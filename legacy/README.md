# legacy/

Superseded one-off scripts kept for reference. Each carried its own copy of the
ELM327 transport and ISO-TP decoders; the maintained versions now live in:

- `../elm327.py`        — BLE + USB transport
- `../leaf_decoders.py` — all LBC group decoders (°C / °F)
- `../battery_read.py`  — console reader (replaces battery_cell_read.py, usb_battery_read.py, BatteryLogger.py, usb_power_monitor.py)
- `../web/reader.py`    — dashboard daemon (SQLite store, auto-reconnect)

| Script | Replaced by |
|---|---|
| `battery_cell_read.py` (BLE) | `battery_read.py --adapter ble --cells` |
| `usb_battery_read.py` | `battery_read.py --adapter usb --cells` |
| `BatteryLogger.py` (JSONL logger) | `battery_read.py --loop 30 --store` |
| `usb_power_monitor.py` | `battery_read.py --loop 2` or `web/app.py --fast` |
| `monitor_can.py` (first attempt, no output) | `door_watch.py` / `probe_hvac_carcan.py` |

The probe/capture one-offs here (`gear_probe.py`, `gear_capture.py`,
`turn_signal_demo.py`, `usb_energy_probe.py`, …) are all superseded by the
interactive walker `../calibrate_input.py` and the live watchers
`../door_watch.py` / `../gear_hvac_live.py`.
