#!/usr/bin/env python3
"""Raspberry Pi Zero W indoor air-quality monitor (PMSA003I + BME280)."""
import csv
import io
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template, request, Response

import board
import adafruit_bme280.basic as adafruit_bme280
import adafruit_pm25.i2c

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AIRQUALITY_DB", os.path.join(APP_DIR, "airquality.sqlite3"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))
SAVE_SECONDS = int(os.environ.get("SAVE_SECONDS", "30"))
F_PER_C = 9 / 5
HPA_PER_INHG = 33.8638866667
INHG_PER_HPA = 1 / HPA_PER_INHG

app = Flask(__name__)
lock = threading.Lock()
latest = {"ok": False, "errors": ["Sensors have not been read yet"], "timestamp": None}
last_saved = 0
i2c = None
bme = None
pm = None


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat(timespec="seconds")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pm1 REAL, pm25 REAL, pm10 REAL,
                pm25_env REAL, pm10_env REAL,
                temperature REAL, humidity REAL, pressure REAL,
                aqi INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calibration_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                temperature_f_offset REAL NOT NULL DEFAULT 0,
                humidity_offset REAL NOT NULL DEFAULT 0,
                pressure_inhg_offset REAL NOT NULL DEFAULT 0,
                pm25_offset REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trend_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_active ON trend_alerts(resolved_at, last_seen)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                category TEXT NOT NULL,
                notes TEXT NOT NULL,
                details TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO calibration_settings (id, updated_at)
            VALUES (1, ?)
        """, (iso_now(),))


def get_calibration():
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM calibration_settings WHERE id=1").fetchone()
    return dict(row) if row else {
        "temperature_f_offset": 0.0, "humidity_offset": 0.0,
        "pressure_inhg_offset": 0.0, "pm25_offset": 0.0, "updated_at": None
    }


def pm25_aqi(value):
    """Approximate US EPA PM2.5 AQI; concentration is in µg/m³."""
    if value is None or value < 0:
        return None
    c = int(value * 10) / 10.0
    bands = [(0.0, 9.0, 0, 50), (9.1, 35.4, 51, 100),
             (35.5, 55.4, 101, 150), (55.5, 125.4, 151, 200),
             (125.5, 225.4, 201, 300), (225.5, 325.4, 301, 500)]
    for clo, chi, ilo, ihi in bands:
        if clo <= c <= chi:
            return round((ihi - ilo) / (chi - clo) * (c - clo) + ilo)
    return 500 if c > 325.4 else 0


def aqi_label(aqi):
    if aqi is None: return "Unavailable"
    if aqi <= 50: return "Good"
    if aqi <= 100: return "Moderate"
    if aqi <= 150: return "Unhealthy for sensitive groups"
    if aqi <= 200: return "Unhealthy"
    if aqi <= 300: return "Very unhealthy"
    return "Hazardous"


def init_sensors():
    global i2c, bme, pm
    i2c = board.I2C()
    try:
        bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=0x77)
    except Exception:
        bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=0x76)
    pm = adafruit_pm25.i2c.PM25_I2C(i2c, None)


def calibrated_reading(raw, calibration):
    """Apply user-entered display/reference offsets before data is stored."""
    result = dict(raw)
    temp_offset_f = float(calibration.get("temperature_f_offset") or 0)
    humidity_offset = float(calibration.get("humidity_offset") or 0)
    pressure_offset_inhg = float(calibration.get("pressure_inhg_offset") or 0)
    pm25_offset = float(calibration.get("pm25_offset") or 0)
    if result.get("temperature") is not None:
        result["temperature"] += temp_offset_f / F_PER_C
    if result.get("humidity") is not None:
        result["humidity"] = min(100.0, max(0.0, result["humidity"] + humidity_offset))
    if result.get("pressure") is not None:
        result["pressure"] += pressure_offset_inhg * HPA_PER_INHG
    if result.get("pm25") is not None:
        result["pm25"] = max(0.0, result["pm25"] + pm25_offset)
    return result


def update_trend_alerts():
    """Compare the most recent hour with the preceding 24-hour baseline."""
    now = utc_now()
    recent_start = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    baseline_start = (now - timedelta(hours=25)).isoformat(timespec="seconds")
    recent_end = now.isoformat(timespec="seconds")
    with connect_db() as conn:
        recent = conn.execute("""
            SELECT COUNT(*) n, AVG(pm25) pm25, AVG(temperature) temp_c,
                   AVG(humidity) humidity
            FROM readings WHERE timestamp >= ? AND timestamp <= ?
        """, (recent_start, recent_end)).fetchone()
        baseline = conn.execute("""
            SELECT COUNT(*) n, AVG(pm25) pm25, AVG(temperature) temp_c,
                   AVG(humidity) humidity
            FROM readings WHERE timestamp >= ? AND timestamp < ?
        """, (baseline_start, recent_start)).fetchone()

        conditions = {}
        evaluated = set()
        if recent["n"] >= 4:
            pm_recent = recent["pm25"]
            pm_base = baseline["pm25"] if baseline["n"] >= 10 else None
            evaluated.add("pm25_rising")
            if pm_recent is not None and (
                pm_recent > 35.4 or
                (pm_base is not None and pm_recent >= max(12.0, pm_base * 1.5)
                 and pm_recent - pm_base >= 5.0)
            ):
                base_text = f" The previous 24-hour baseline was {pm_base:.1f} µg/m³." if pm_base is not None else ""
                conditions["pm25_rising"] = (
                    "alert", "PM2.5 trend is elevated",
                    f"The last-hour PM2.5 average is {pm_recent:.1f} µg/m³.{base_text} Check cooking, smoke, candles, dust, and outdoor air before ventilating."
                )

            humidity_recent = recent["humidity"]
            humidity_base = baseline["humidity"] if baseline["n"] >= 10 else None
            evaluated.add("humidity_shift")
            if humidity_recent is not None and (
                humidity_recent > 60 or humidity_recent < 30 or
                (humidity_base is not None and abs(humidity_recent - humidity_base) >= 15)
            ):
                baseline_note = f" The prior baseline was {humidity_base:.0f}%." if humidity_base is not None else ""
                conditions["humidity_shift"] = (
                    "warning", "Humidity moved outside its usual range",
                    f"The last-hour average is {humidity_recent:.0f}%. Typical indoor guidance is around 30–50%.{baseline_note} Check moisture sources, condensation, and ventilation."
                )

            temp_recent = recent["temp_c"]
            temp_base = baseline["temp_c"] if baseline["n"] >= 10 else None
            evaluated.add("temperature_shift")
            if temp_recent is not None and temp_base is not None:
                change_f = (temp_recent - temp_base) * F_PER_C
                if abs(change_f) >= 5:
                    direction = "increased" if change_f > 0 else "decreased"
                    conditions["temperature_shift"] = (
                        "warning", "Indoor temperature changed noticeably",
                        f"The last-hour average {direction} by {abs(change_f):.1f}°F compared with the preceding 24-hour baseline. Check heating, cooling, and open windows."
                    )

        now_text = now.isoformat(timespec="seconds")
        for code, (severity, title, message) in conditions.items():
            active = conn.execute(
                "SELECT id FROM trend_alerts WHERE code=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                (code,)
            ).fetchone()
            if active:
                conn.execute("UPDATE trend_alerts SET severity=?, title=?, message=?, last_seen=? WHERE id=?",
                             (severity, title, message, now_text, active["id"]))
            else:
                conn.execute("""INSERT INTO trend_alerts
                    (code, severity, title, message, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (code, severity, title, message, now_text, now_text))
        for code in evaluated - set(conditions):
            conn.execute("UPDATE trend_alerts SET resolved_at=? WHERE code=? AND resolved_at IS NULL",
                         (now_text, code))


def read_sensors():
    global latest, last_saved
    while True:
        now = iso_now()
        result = {"ok": False, "timestamp": now, "errors": []}
        try:
            p = pm.read()
            result.update(
                pm1=float(p.get("pm10 standard", 0)),
                pm25=float(p.get("pm25 standard", 0)),
                pm10=float(p.get("pm100 standard", 0)),
                pm25_env=float(p.get("pm25 env", 0)),
                pm10_env=float(p.get("pm100 env", 0)),
            )
        except Exception as exc:
            result["errors"].append("PMSA003I: " + str(exc))
        try:
            result.update(
                temperature=round(float(bme.temperature), 2),
                humidity=round(float(bme.relative_humidity), 2),
                pressure=round(float(bme.pressure), 2),
            )
        except Exception as exc:
            result["errors"].append("BME280: " + str(exc))

        try:
            result = calibrated_reading(result, get_calibration())
        except Exception as exc:
            result["errors"].append("Calibration settings: " + str(exc))
        result["aqi"] = pm25_aqi(result.get("pm25"))
        result["aqi_label"] = aqi_label(result["aqi"])
        result["ok"] = not result["errors"] and "pm25" in result and "temperature" in result

        with lock:
            latest = result.copy()

        if ("pm25" in result or "temperature" in result) and time.time() - last_saved >= SAVE_SECONDS:
            try:
                with connect_db() as conn:
                    conn.execute("""
                        INSERT INTO readings
                        (timestamp, pm1, pm25, pm10, pm25_env, pm10_env,
                         temperature, humidity, pressure, aqi)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (now, result.get("pm1"), result.get("pm25"), result.get("pm10"),
                          result.get("pm25_env"), result.get("pm10_env"),
                          result.get("temperature"), result.get("humidity"),
                          result.get("pressure"), result.get("aqi")))
                last_saved = time.time()
                update_trend_alerts()
            except Exception as exc:
                print("Database write / trend check failed:", exc, flush=True)
        time.sleep(POLL_SECONDS)


def period_seconds(period):
    return {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}.get(period, 86400)


def requested_history_window():
    """Return an inclusive UTC window for preset or user-entered local ranges."""
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    if bool(start_raw) != bool(end_raw):
        raise ValueError("Choose both a start and end time.")
    if start_raw and end_raw:
        def parse_client_datetime(value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        start = parse_client_datetime(start_raw)
        end = parse_client_datetime(end_raw)
        if end <= start:
            raise ValueError("The end time must be later than the start time.")
        if end - start > timedelta(days=366):
            raise ValueError("Custom ranges are limited to 366 days.")
        return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), "custom"

    period = request.args.get("period", "24h")
    if period not in {"1h", "6h", "24h", "7d", "30d"}:
        raise ValueError("Unsupported time range.")
    end = utc_now()
    start = end - timedelta(seconds=period_seconds(period))
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), period


def report_window(seconds):
    end = utc_now()
    start = end - timedelta(seconds=seconds)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def aggregate_window(start, end):
    with connect_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS samples,
                   MIN(pm25) AS pm25_min, AVG(pm25) AS pm25_avg, MAX(pm25) AS pm25_max,
                   MIN(aqi) AS aqi_min, AVG(aqi) AS aqi_avg, MAX(aqi) AS aqi_max,
                   MIN(temperature) AS temp_min, AVG(temperature) AS temp_avg, MAX(temperature) AS temp_max,
                   MIN(humidity) AS humidity_min, AVG(humidity) AS humidity_avg, MAX(humidity) AS humidity_max,
                   MIN(pressure) AS pressure_min, AVG(pressure) AS pressure_avg, MAX(pressure) AS pressure_max,
                   MIN(timestamp) AS first_sample, MAX(timestamp) AS last_sample
            FROM readings WHERE timestamp >= ? AND timestamp < ?
        """, (start, end)).fetchone()
    d = dict(row)
    for key in ("temp_min", "temp_avg", "temp_max"):
        if d[key] is not None: d[key] = round(d[key] * F_PER_C + 32, 1)
    for key in ("pressure_min", "pressure_avg", "pressure_max"):
        if d[key] is not None: d[key] = round(d[key] * INHG_PER_HPA, 2)
    for key in d:
        if key.endswith("_avg") and d[key] is not None and key not in ("temp_avg", "pressure_avg"):
            d[key] = round(d[key], 1)
    return d


def build_report(seconds):
    end = utc_now()
    start = end - timedelta(seconds=seconds)
    previous_end = start
    previous_start = start - timedelta(seconds=seconds)
    current = aggregate_window(start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))
    previous = aggregate_window(previous_start.isoformat(timespec="seconds"), previous_end.isoformat(timespec="seconds"))
    comparisons = {}
    for key in ("pm25_avg", "temp_avg", "humidity_avg", "pressure_avg"):
        a, b = current.get(key), previous.get(key)
        comparisons[key] = None if a is None or b is None else round(a - b, 1)
    return {
        "generated_at": end.isoformat(timespec="seconds"),
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": end.isoformat(timespec="seconds"),
        "hours": int(seconds / 3600),
        "current": current,
        "previous": previous,
        "change_vs_previous": comparisons,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/reports")
def reports_page():
    return render_template("reports.html")


@app.route("/maintenance")
def maintenance_page():
    return render_template("maintenance.html")


@app.route("/api/current")
def api_current():
    with lock:
        data = latest.copy()
    if data.get("temperature") is not None:
        data["temperature_f"] = round(data["temperature"] * F_PER_C + 32, 1)
    if data.get("pressure") is not None:
        data["pressure_inhg"] = round(data["pressure"] * INHG_PER_HPA, 2)
    return jsonify(data)


@app.route("/api/history")
def api_history():
    try:
        start, end, period = requested_history_window()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT timestamp, pm1, pm25, pm10, temperature, humidity, pressure, aqi
            FROM readings WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC
        """, (start, end)).fetchall()
    values = [dict(r) for r in rows]
    raw_count = len(values)
    # Keep the chart responsive for very long ranges by selecting actual stored
    # samples at regular intervals. No stored readings are changed or averaged.
    max_points = 2400
    if len(values) > max_points:
        step = len(values) / max_points
        values = [values[int(i * step)] for i in range(max_points)]
    for row in values:
        if row.get("temperature") is not None: row["temperature"] = round(row["temperature"] * F_PER_C + 32, 1)
        if row.get("pressure") is not None: row["pressure"] = round(row["pressure"] * INHG_PER_HPA, 2)
    return jsonify({"period": period, "count": raw_count, "plotted_count": len(values),
                    "start": start, "end": end, "readings": values})


@app.route("/api/summary")
def api_summary():
    try:
        start, end, _period = requested_history_window()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    fields = ["pm25", "temperature", "humidity", "pressure"]
    summary = {}
    with connect_db() as conn:
        for field in fields:
            row = conn.execute(
                f"SELECT MIN({field}) lo, AVG({field}) avg, MAX({field}) hi FROM readings WHERE timestamp >= ? AND timestamp <= ?",
                (start, end)).fetchone()
            summary[field] = dict(min=row["lo"], avg=row["avg"], max=row["hi"])
    for field in ("temperature", "pressure"):
        for key in ("min", "avg", "max"):
            value = summary[field][key]
            if value is not None:
                summary[field][key] = round(value * F_PER_C + 32, 1) if field == "temperature" else round(value * INHG_PER_HPA, 2)
    return jsonify(summary)


@app.route("/api/reports")
def api_reports():
    return jsonify({"daily": build_report(86400), "weekly": build_report(604800)})


@app.route("/reports/<period>.txt")
def download_report(period):
    if period not in ("daily", "weekly"):
        return "Unknown report period", 404
    seconds = 86400 if period == "daily" else 604800
    report = build_report(seconds)
    d = report["current"]
    label = "Rolling 24-hour" if period == "daily" else "Rolling 7-day"
    lines = [
        f"AIRQUALITY {label.upper()} REPORT",
        f"Generated: {report['generated_at']}",
        f"Window: {report['window_start']} to {report['window_end']} (UTC)",
        f"Saved samples: {d['samples']}",
        "",
        "PM2.5 (µg/m³): min {0} | avg {1} | max {2}".format(d['pm25_min'], d['pm25_avg'], d['pm25_max']),
        "PM2.5 AQI estimate: min {0} | avg {1} | max {2}".format(d['aqi_min'], d['aqi_avg'], d['aqi_max']),
        "Temperature (°F): min {0} | avg {1} | max {2}".format(d['temp_min'], d['temp_avg'], d['temp_max']),
        "Humidity (%): min {0} | avg {1} | max {2}".format(d['humidity_min'], d['humidity_avg'], d['humidity_max']),
        "Pressure (inHg): min {0} | avg {1} | max {2}".format(d['pressure_min'], d['pressure_avg'], d['pressure_max']),
        "",
        "Compared with the previous matching period:",
    ]
    for key, label_text, unit in [("pm25_avg", "PM2.5 average", "µg/m³"), ("temp_avg", "Temperature average", "°F"),
                                  ("humidity_avg", "Humidity average", "%"), ("pressure_avg", "Pressure average", "inHg")]:
        change = report["change_vs_previous"][key]
        lines.append(f"{label_text}: {change:+.1f} {unit}" if change is not None else f"{label_text}: not enough comparison data")
    lines += ["", "AQI is an approximate PM2.5-based estimate, not a certified regulatory measurement."]
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=airquality-{period}-report.txt"})


@app.route("/api/alerts")
def api_alerts():
    with connect_db() as conn:
        active = conn.execute("SELECT * FROM trend_alerts WHERE resolved_at IS NULL ORDER BY last_seen DESC").fetchall()
        recent = conn.execute("SELECT * FROM trend_alerts ORDER BY first_seen DESC LIMIT 20").fetchall()
    return jsonify({"active": [dict(r) for r in active], "recent": [dict(r) for r in recent]})


@app.route("/api/maintenance", methods=["GET"])
def api_maintenance():
    with connect_db() as conn:
        settings = conn.execute("SELECT * FROM calibration_settings WHERE id=1").fetchone()
        logs = conn.execute("SELECT * FROM maintenance_log ORDER BY timestamp DESC LIMIT 30").fetchall()
    return jsonify({"calibration": dict(settings) if settings else {}, "log": [dict(r) for r in logs]})


@app.route("/api/calibration", methods=["POST"])
def api_calibration():
    payload = request.get_json(silent=True) or request.form
    keys = {
        "temperature_f_offset": (-15.0, 15.0),
        "humidity_offset": (-25.0, 25.0),
        "pressure_inhg_offset": (-1.0, 1.0),
        "pm25_offset": (-50.0, 50.0),
    }
    values = {}
    try:
        for key, (low, high) in keys.items():
            value = float(payload.get(key, 0))
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            values[key] = value
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    now = iso_now()
    with connect_db() as conn:
        conn.execute("""UPDATE calibration_settings SET temperature_f_offset=?, humidity_offset=?,
            pressure_inhg_offset=?, pm25_offset=?, updated_at=? WHERE id=1""",
            (values["temperature_f_offset"], values["humidity_offset"],
             values["pressure_inhg_offset"], values["pm25_offset"], now))
        details = json.dumps(values, sort_keys=True)
        conn.execute("INSERT INTO maintenance_log (timestamp, category, notes, details) VALUES (?, ?, ?, ?)",
                     (now, "calibration", "Calibration offsets updated", details))
    return jsonify({"ok": True, "calibration": values, "updated_at": now})


@app.route("/api/maintenance/log", methods=["POST"])
def add_maintenance_log():
    payload = request.get_json(silent=True) or request.form
    category = str(payload.get("category", "other"))[:30]
    notes = str(payload.get("notes", "")).strip()[:500]
    allowed = {"inspection", "cleaning", "fan_check", "other"}
    if category not in allowed or not notes:
        return jsonify({"ok": False, "error": "Choose a category and enter a note."}), 400
    now = iso_now()
    with connect_db() as conn:
        conn.execute("INSERT INTO maintenance_log (timestamp, category, notes) VALUES (?, ?, ?)",
                     (now, category, notes))
    return jsonify({"ok": True, "timestamp": now})


@app.route("/download.csv")
def download_csv():
    try:
        start, end, period = requested_history_window()
    except ValueError as exc:
        return str(exc), 400
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM readings WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC", (start, end)).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["timestamp_utc", "pm1_standard_ug_m3", "pm25_standard_ug_m3", "pm10_standard_ug_m3",
                     "pm25_environmental_ug_m3", "pm10_environmental_ug_m3", "temperature_f",
                     "humidity_percent", "pressure_inhg", "approx_pm25_aqi"])
    for row in rows:
        temp_f = None if row["temperature"] is None else round(row["temperature"] * F_PER_C + 32, 1)
        pressure_inhg = None if row["pressure"] is None else round(row["pressure"] * INHG_PER_HPA, 2)
        writer.writerow([row["timestamp"], row["pm1"], row["pm25"], row["pm10"], row["pm25_env"],
                         row["pm10_env"], temp_f, row["humidity"], pressure_inhg, row["aqi"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=airquality-{period}.csv"})


if __name__ == "__main__":
    init_db()
    try:
        init_sensors()
        threading.Thread(target=read_sensors, daemon=True).start()
    except Exception as exc:
        latest.update(ok=False, errors=["Sensor initialization failed: " + str(exc)])
        print("Sensor initialization failed:", exc, flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
