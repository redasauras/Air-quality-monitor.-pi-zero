# Raspberry Pi Zero W Air Quality Tracker

A local web dashboard for indoor air quality readings from a BME280 environmental sensor and a PMSA003I particulate sensor. The project code was AI-generated; the project planning, component sourcing, and physical design were done by the project creator.

## Features

- Live temperature, humidity, pressure, and particulate readings
- SQLite history, graphs, and CSV downloads
- Rolling daily and weekly reports
- Heuristic trend alerts and indoor condition guidance
- Maintenance log and configurable sensor correction offsets

## Hardware

- Raspberry Pi Zero W and power supply with Raspberry Pi OS (you can also use pi os lite) (32-bit recommended for compatibility)
- BME280 breakout [https://www.adafruit.com/product/2652](url)
- PMSA003I particulate sensor breakout [https://www.adafruit.com/product/4632](url)
- steam qt breakout for pi zeros [https://www.adafruit.com/product/6365](url) (optional but make wireing solderles and simpler)
- stema qt cables i used 1x 100m and 1x50mm [https://www.adafruit.com/product/4210](url) (optional but make wireing solderles and simpler)
- Jumper wires or the stema qt parts above shown above
- 3d printed enclosure found in the folder named (3d files) the folder includes both f3d for making you own chages and 3mf for printing (printed in petg)
- double sided tape for keeping the lid on the enclosure [https://www.amazon.com/dp/B0FMNHWPR3/ref=sspa_dk_detail_0?pd_rd_i=B0FMNHWPR3&pd_rd_w=PkzSo&content-id=amzn1.sym.17345c9b-ef8c-4a79-bcd9-8894b1e1e0ea&pf_rd_p=17345c9b-ef8c-4a79-bcd9-8894b1e1e0ea&pf_rd_r=WQ52FTRPFAKVJBG5EG4M&pd_rd_wg=m5ORJ&pd_rd_r=c9344147-aac9-44c2-a19a-ce61da58d97a&sp_csd=d2lkZ2V0TmFtZT1zcF9kZXRhaWw&th=1](url) (anything simmlar will work)
- command strips for mounting to it the wall (optional)
- a few short m3 hex screws for mounting the components in the enclosure  

The Raspberry Pi Zero 2 W may also work, but check physical fit with the enclosure. Check each breakout board's voltage requirements. Connect both sensors to 3.3V-compatible power, ground, SDA (GPIO2, physical pin 3), and SCL (GPIO3, physical pin 5). On the Pi, physical pin 1 is 3.3V and pin 6 is ground. Confirm sensor-specific wiring and pin labels against their manufacturer documentation before powering the circuit. Both devices use I2C; verify their addresses do not conflict.

## Install on Raspberry Pi OS

1. Enable I2C with `sudo raspi-config` → **Interface Options** → **I2C** → **Enable**, then reboot if prompted.
2. Install system packages and grant your account access to I2C:

   ```bash
   sudo apt update
   sudo apt install -y python3-venv python3-pip i2c-tools
   sudo usermod -aG i2c "$USER"
   ```

   Log out and back in, then check for devices with `i2cdetect -y 1`. Common BME280 addresses are `0x76` or `0x77`; PMSA003I commonly uses `0x12`.

3. Put the project folder at `~/airquality`, then install and start it:

   ```bash
   cd ~/airquality
   python3 -m venv --system-site-packages .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   python app.py
   ```

   `--system-site-packages` lets the virtual environment use the Raspberry Pi OS board support installed by Adafruit Blinka. The app creates `airquality.sqlite3` on first start. Optional environment settings are `AIRQUALITY_DB` (database file path), `POLL_SECONDS` (sensor read interval, default 5), and `SAVE_SECONDS` (database save interval, default 30). No Wi-Fi password or API key is required by the app.

4. Find the Pi's current LAN address with `hostname -I`. On another device on the same trusted home network, open `http://<PI-LAN-IP>:5000` (replace the placeholder with the Pi's address). Stop a manually started server with Ctrl+C.

The dashboard loads Chart.js from a CDN, so charts need internet access. Sensor readings and saved history are served locally. The server listens on all network interfaces at port 5000 and has no login; use it only on a trusted LAN and do not expose the port directly to the internet.

## Start automatically at boot (optional)

1. Copy `airquality.service.example` to `/etc/systemd/system/airquality.service`.
2. Edit the copied file and replace `<PI_USER>` with your Raspberry Pi Linux username and `<PROJECT_DIR>` with the full project folder path (for example, `/home/alex/airquality`). Remove the angle brackets. `User`, `WorkingDirectory`, and both paths in `ExecStart` must match your installation.
3. Enable and start the service:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now airquality
   sudo systemctl status airquality
   ```

   View logs with `sudo journalctl -u airquality -f`.

## Data and privacy

Readings, calibration settings, trend alerts, and maintenance entries are stored in `airquality.sqlite3` next to the app by default. The database is local and may contain your household's sensor history or maintenance notes. It is excluded from Git by `.gitignore`; do not publish it. The app has no account, telemetry, or Wi-Fi credential configuration. It does bind to the LAN, and the dashboard fetches Chart.js from a public CDN.

## Temperature accuracy and calibration

The compact enclosure can let heat from the Pi affect the BME280 temperature reading. Treat temperature as an estimate. The maintenance page supports correction offsets applied to new readings only; offsets do not rewrite past history and are not certified calibration. Set an offset only after comparison with a suitable reference instrument.

The particulate AQI is an approximate PM2.5-based estimate, not a regulatory measurement or medical alert. Trend thresholds and indoor suggestions are code-based heuristics. The sensors do not measure outdoor conditions, CO2, or VOCs.

## Troubleshooting

- If sensor initialization fails, check I2C is enabled, reconnect the I2C group session, inspect wiring and voltage, and run `i2cdetect -y 1`.
- If the dashboard opens but readings are unavailable, check `sudo journalctl -u airquality -f` for service installs or the terminal output for a manual run.
- If port 5000 is unavailable, check that the app is running and use the Pi's current LAN IP address.
- If charts do not render, confirm the viewing device has internet access for the Chart.js CDN.

## License

The source code is provided under the MIT License; see [LICENSE](LICENSE). The MIT License does not automatically cover separate 3D model files. Add the model and its chosen license separately if you distribute it in this repository.
