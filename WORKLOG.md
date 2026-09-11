# Nissan Leaf 2012 — CAN Bus Logging Work Log

## Project Overview
- **Vehicle**: 2012 Nissan Leaf EV
- **Platform**: macOS, Python 3.9.6

### Adapters
| Adapter | Type | Connection | Firmware | Notes |
|---------|------|------------|----------|-------|
| LELink "OBDBLE" | BLE | the adapter address / GATT `0xFFE0`→`0xFFE1` | ELM327 v1.5 | Requires `response=True` on writes |
| obdiisoft.com USB | USB Serial | `/dev/tty.usbserial-10` @ 38400 baud | ELM327 v1.5 | CH340 chip, HS/MS switch (use HS for Car-CAN) |

Both adapters use the same ELM327 AT command set. Transport is abstracted in `elm327.py`.

---

## Key Findings

### Adapter Quirk: Write-With-Response Required
This adapter **only triggers BLE notifications when writes use `response=True`**.
`response=False` (write-without-response) silently succeeds but produces zero notification callbacks.
This was confirmed via `diag_notify.py` Test B.

### Protocol
- **ATSP6**: ISO 15765-4 CAN, 11-bit ID, 500 kbaud
- Standard OBD PIDs (`0100`) return `NO DATA` — the Leaf does not implement standard OBD-II service modes
- All useful data uses **Nissan-proprietary CAN message IDs**

### CAN Bus Architecture (OBD-II Port)
| Bus | OBD Pins | Notes |
|-----|----------|-------|
| **Car-CAN** | Pin 6/14 | Standard ELM327 connects here. General vehicle operation |
| **EV-CAN** | Pin 13/12 | Battery/drive system. Accessible via bridging (0x79B→0x7BB) |
| **AV-CAN** | Pin 11/3 | Infotainment |

The 2012 Leaf has **no CAN gateway** — raw traffic is directly available on the OBD port.

### CAN IDs Observed on Car-CAN (2026-02-15)

| CAN ID | Data Sample | Likely Function | Notes |
|--------|-------------|-----------------|-------|
| `002` | `7D FF 00 07 3E` | Unknown | DATA ERROR (may be 29-bit) |
| `130` | `00 32 63` | Counter/status | Changes between captures |
| `174` | `00 00 00 AA 03 00 00 00` | **Gear position** | Byte 3: `AA`=P/N, `99`=R, `BB`=D/Eco. Byte 4 is rolling counter |
| `176` | `00 00 00 00 00 00 03` | Rolling counter | Last byte increments |
| `180` | `00 00 00 00 00 00 23 00` | Steering/chassis | |
| `1D5` | `00 00 00 03 D9` | Torque/motor speed | |
| `1F9` | `00 00 00 00 00 00 00 00` | Idle frame | All zeros at rest |
| `245` | `7F E8 02 18 3A 00 7F E2` | Unknown | DATA ERROR |
| `260` | (truncated) | Unknown | |
| `284` | `00 00 00 00 00 00 43 C9` | Speed/odometer | |
| `285` | `00 00 00 00 00 00 43 CA` | Speed/odometer | |
| `292` | `80 08 28 80 30 00 00 02` | Unknown | DATA ERROR |
| `300` | `03` | Status byte | |
| `354` | `00 00 00 00 00 10 00 00` | Rolling counter | Byte 5 is 2-bit counter (00→08→10→18), not gear data |
| `358` | `00 0A XX 00 00 00 00 00` | **Turn signals / body** | ~10 Hz. Byte 3: `80`=off, `82`=left, `84`=right |
| `6F6` | `81 00 00` | Unknown | DATA ERROR |

### Buffer Overflow
Unfiltered ATMA hits `BUFFER FULL` quickly — the ELM327 clone cannot keep up with full bus traffic. **Must use ATCRA filters** for sustained monitoring.

---

## Session Log

### Session 1 — 2026-02-15 (Phase 1: Connection & Enumeration)
1. Created Python venv, installed bleak
2. `scan_ble.py` — found adapter as "OBDBLE" at -52 dBm
3. `enumerate_gatt.py` — mapped GATT services:
   - `0x180A` Device Information (generic placeholders)
   - `0xFFE0` Vendor Specific: `0xFFE1` (notify/write/read), `0xFFEE` (write/read config)
4. `probe_adapter.py` — confirmed ELM327 v1.5, "OBDII to RS232 Interpreter"

### Session 1 — 2026-02-15 (Phase 2: CAN Bus Observation)
5. `monitor_can.py` v1 — zero output (write-without-response bug)
6. `diag_notify.py` — diagnosed root cause: `response=True` required for notifications
7. `query_and_monitor.py` — **successful CAN capture**:
   - 0x358 (turn signals): 30 frames in 3s
   - 0x5B3 (SOH/GIDs): 6 frames with DATA ERROR (protocol mismatch)
   - Unfiltered ATMA: 24 frames across 16+ CAN IDs before BUFFER FULL
8. **Live stream viewer** — `live_stream.py` created for real-time filtered CAN monitoring
9. **Turn signal capture** — 60s stream on 0x358:
   - 605 frames, 0 errors, ~10 frames/sec
   - Byte 3 encodes turn signal state: `0x80`=off, `0x82`=left, `0x84`=right
   - Transitions clearly visible in data (confirmed by toggling signals)

### Session 2 — 2026-02-15 (Phase 3: Gear Position Decoding)
10. **Gear probe scan** — `gear_probe.py` scanned 10 candidate CAN IDs with ATCRA filters
    - 0x354 byte 5: initially looked like gear data, turned out to be a 2-bit rolling counter (00→08→10→18)
    - 0x174 byte 3: confirmed as gear position signal
11. **Per-gear capture** — `gear_capture.py` captured 0x174 and 0x354 in each gear:
    - 0x174 byte 3: `AA`=Park, `99`=Reverse, `AA`=Neutral, `BB`=Drive, `BB`=Eco
    - 0x174 byte 4: rolling counter (varies independently of gear)
    - 0x354 byte 5: rolling counter cycling 00→08→10→18 continuously
12. **Drive vs Eco diff** — `drive_eco_diff.py` compared 12 CAN IDs between Drive and Eco:
    - No stable differentiator found on Car-CAN bus
    - All observed byte changes were rolling counters or odometer values
    - D/Eco distinction may require EV-CAN bus access (pins 12/13)
13. **Gear demo** — `gear_demo.py` displays 3 gear states in real time:
    - `P/N` (Park/Neutral), `R` (Reverse), `D/E` (Drive/Eco)
    - Uses 5-frame debounce for clean output

### Session 2 — 2026-02-15 (Phase 4: BMS Battery Data via UDS)
14. **BMS diagnostic access** — confirmed VCM bridges Car-CAN ↔ EV-CAN for UDS requests
    - ELM327 clone supports flow control: `ATFCSH`, `ATFCSD`, `ATFCSM` all accepted
    - Requires `ATCAF1` (auto-formatting ON) — `ATCAF0` returns NO DATA
    - Header: `ATSH 79B`, response filter: `ATCRA 7BB`
    - Flow control: `ATFCSH 79B` / `ATFCSD 30 00 20` / `ATFCSM1`
15. **Cell pair voltages** — `2102` request returns 29 ISO-TP frames, all 96 cells:
    - 16-bit big-endian millivolts per cell pair
    - Sample reading: 4003–4029 mV range, 26 mV spread, 385.8V pack sum
    - Cell 53 lowest (4003 mV), Cells 30/33/44/47 highest (4029 mV)
16. **Battery temperatures** — `2104` request returns temperature data (decoding TBD)
17. **Battery state** — `2101` request returns SOC, current, etc. (decoding TBD)

### Decoded CAN Signals

| Signal | CAN ID | Byte | Values | Notes |
|--------|--------|------|--------|-------|
| Turn signals | `0x358` | byte 2 | `80`=off, `82`=left, `84`=right | ~10 Hz |
| Gear position | `0x174` | byte 3 | `AA`=P/N, `99`=R, `BB`=D/Eco | Cannot distinguish P/N or D/Eco on Car-CAN |

### BMS UDS Diagnostic Commands

| Command | Target | Response | Content |
|---------|--------|----------|---------|
| `2101` | `0x79B→0x7BB` | 6 frames | Battery state (SOC, current, etc.) |
| `2102` | `0x79B→0x7BB` | 29 frames | **96 cell pair voltages (mV)** |
| `2104` | `0x79B→0x7BB` | 3 frames | Battery temperatures |

---

## Scripts

| Script | Purpose | Status |
|--------|---------|--------|
| `scan_ble.py` | Passive BLE device scan | Working |
| `enumerate_gatt.py` | GATT service enumeration (read-only) | Working |
| `probe_adapter.py` | ELM327 adapter identity commands | Working |
| `diag_notify.py` | BLE notification diagnostic | Working |
| `monitor_can.py` | Basic CAN monitor (first attempt) | Superseded |
| `query_and_monitor.py` | Multi-phase query + monitor | Working |
| `live_stream.py` | Real-time filtered CAN stream viewer | **Working** |
| `turn_signal_demo.py` | Human-readable turn signal display | **Working** |
| `gear_probe.py` | Gear position candidate scanner | Working |
| `gear_capture.py` | Per-gear data capture (interactive) | Working |
| `drive_eco_diff.py` | Drive vs Eco CAN diff tool | Working |
| `gear_demo.py` | Human-readable gear position display | **Working** |
| `battery_diag.py` | BMS read diagnostic (tests various approaches) | Working |
| `battery_cell_read.py` | **96 cell pair voltage reader with stats** | **Working** |

### Session 3 — 2026-02-19 (USB Serial Adapter Testing)
18. **USB adapter probe** — `usb_probe_adapter.py` created for serial ELM327 testing
    - CH340 USB-to-serial chip, detected at `/dev/tty.usbserial-10`
    - Auto-detected baud rate: 38400 (also tried 115200, 9600)
    - Firmware: ELM327 v1.5, "OBDII to RS232 Interpreter"
    - Adapter has HS/MS CAN switch
19. **MS vs HS switch testing**
    - MS position: `CAN ERROR` on all commands — likely routes to different OBD pins (Ford MS-CAN)
    - HS position: works correctly on Car-CAN (pins 6/14), same as BLE adapter
20. **USB battery read** — `usb_battery_read.py` confirmed full BMS read over USB:
    - All 96 cell pairs, SOC, SOH, capacity, temps — identical data to BLE adapter
    - Sample: SOC 68.18%, capacity 24.90 Ah (37.7% SOH), 29 mV spread, 17-19°C
21. **Dual-adapter architecture** — created `elm327.py` transport abstraction
    - `BleELM` class: async BLE transport via bleak
    - `SerialELM` class: sync serial transport via pyserial (wrapped async)
    - `detect_adapter()`: auto-detects USB first, then BLE fallback
    - `configure_leaf_bms()`: shared AT command config sequence
    - `web/reader.py` updated to use `elm327.py` with `--adapter` flag (auto/usb/ble)
    - Dashboard updated to display adapter type and port in header

### USB Adapter Notes
- **HS/MS switch**: HS = High Speed CAN (500k, pins 6/14 = Car-CAN). MS = Medium Speed (unknown pins, possibly Ford-specific)
- **Baud rate**: 38400 confirmed. No auto-baud needed at this point
- **pyserial**: Added as dependency (`pip install pyserial`)
- **No BLE quirks**: USB serial doesn't need the `response=True` workaround

## Scripts

Updated scripts table:

| Script | Purpose | Status |
|--------|---------|--------|
| `scan_ble.py` | Passive BLE device scan | Working |
| `enumerate_gatt.py` | GATT service enumeration (read-only) | Working |
| `probe_adapter.py` | ELM327 adapter identity commands (BLE) | Working |
| `diag_notify.py` | BLE notification diagnostic | Working |
| `monitor_can.py` | Basic CAN monitor (first attempt) | Superseded |
| `query_and_monitor.py` | Multi-phase query + monitor | Working |
| `live_stream.py` | Real-time filtered CAN stream viewer | **Working** |
| `turn_signal_demo.py` | Human-readable turn signal display | **Working** |
| `gear_probe.py` | Gear position candidate scanner | Working |
| `gear_capture.py` | Per-gear data capture (interactive) | Working |
| `drive_eco_diff.py` | Drive vs Eco CAN diff tool | Working |
| `gear_demo.py` | Human-readable gear position display | **Working** |
| `battery_diag.py` | BMS read diagnostic (tests various approaches) | Working |
| `battery_cell_read.py` | 96 cell pair voltage reader with stats (BLE) | **Working** |
| `usb_probe_adapter.py` | ELM327 adapter identity commands (USB) | **Working** |
| `usb_battery_read.py` | 96 cell pair voltage reader with stats (USB) | **Working** |
| `usb_can_test.py` | USB CAN bus connectivity diagnostic | Working |
| `usb_ms_can_test.py` | MS switch / EV-CAN connectivity test | Working (MS=no EV-CAN) |
| `usb_energy_probe.py` | Energy signal probe (passive + UDS) | **Working** |
| `usb_energy_decode.py` | LBC/HVAC/VCM/BCM group decoder | **Working** |
| `usb_power_monitor.py` | Real-time power monitor (console) | **Working** |
| `elm327.py` | **Shared transport abstraction (BLE + USB)** | **Working** |
| `web/reader.py` | Background BMS reader daemon (dual-adapter, power) | **Working** |
| `web/app.py` | Flask API + integrated reader | **Working** |

### Session 4 — 2026-02-19 (Energy & Power Monitoring)
22. **MS-CAN switch test** — `usb_ms_can_test.py` tested all 4 CAN protocols on MS switch
    - No traffic on any protocol (500k/250k, 11-bit/29-bit)
    - MS switch does not route to EV-CAN (pins 12/13) on this adapter
    - Likely routes to Ford MS-CAN pins or AV-CAN (pins 11/3)
23. **Energy signal probe** — `usb_energy_probe.py` two-phase test on Car-CAN (HS)
    - Phase 1 (passive): Found 0x260 (available power, 53kW drive / 5kW regen) and 0x1D5 (torque) on Car-CAN
    - EV-CAN signals (0x1DB, 0x1DA, 0x55B, 0x5BC) NOT bridged to Car-CAN passively
    - Phase 2 (UDS): Probed 8 ECUs — LBC, HVAC, ABS, BCM, EPS respond; VCM, inverter, steering do not
24. **New LBC groups decoded** — groups 03, 05, 06 via UDS
    - Group 05 (74 bytes): **pack current** at bytes 22-23 (signed ×0.001A), discharge flag at bytes 20-21
    - Confirmed by heater ON/OFF test: 3.2 kW draw matches dash display of 3-4 kW
    - Cell group voltages at bytes 46-65 (10 segments), voltage sag visible under load
    - Group 06 (25 bytes): cell balancing nibble flags
25. **VCM diagnostic session** — session 0x81 accepted but all groups still return NRC 0x80
    - VCM likely requires CONSULT-III proprietary protocol or security access (service 0x27)
26. **HVAC ECU** (0x744) — returns static 11-byte payload, identical with heater on/off
    - Not useful for climate power detection
27. **Power monitoring** — `usb_power_monitor.py` console demo confirmed:
    - Real-time current and power from LBC group 05
    - 3-4 kW heater draw, ~200W idle "other systems" load
    - 0xFFFF padding in cell groups/segment deltas filtered
28. **Dashboard power integration** — added to web dashboard:
    - Power display with kW value, current (amps), draw/regen/idle state badge
    - Power bar (0-10 kW scale) with color coding
    - Power history sparkline with EMA smoothing (alpha=0.4)
    - LBC group 05 polled alongside groups 01, 02, 04
    - Power and current included in history JSON for trending

### Decoded LBC Group 05 (Extended Battery Data)

| Bytes | Field | Scale | Notes |
|-------|-------|-------|-------|
| 0-1 | Unknown (static) | — | 0x02CF (719) |
| 4-5 | Unknown (static) | — | 0x0199 (409) |
| 6-7 | Cell max voltage | 1 mV | Changes with load |
| 8-9 | Cell min voltage | 1 mV | Changes with load |
| 10-17 | Temperature raws | varies | Match group 04 pattern |
| 18-19 | Unknown | — | Changes with load |
| 20-21 | Discharge flag | — | 0xFFFF=discharging, 0x0000=idle |
| 22-23 | Pack current | ×0.001 A (signed) | Negative=discharge, inverted with flag |
| 26-45 | Segment deltas | 16-bit × 10 | Load distribution, 0xFFFF=padding |
| 46-65 | Cell group voltages | mV × 10 | Per-segment summary, 0xFFFF=padding |

### Session 5 — 2026-08-24 (Reconnect / Context Refresh)
29. **BLE adapter re-verified** — OBDBLE still at the adapter address, ELM327 v1.5, ATRV 13.0V. No USB adapter present.
30. **Full BMS read over BLE** via `elm327.py` + `web/reader.py` decoders — all groups OK:
    - Group 01: SOC 78.76%, capacity **23.16 Ah (SOH 35.1%)**, HX 17.96, 12V 12.70 V, insulation 885 kΩ
    - Group 05: current 1.31 A discharge, ~0.50 kW idle draw, cell max/min 3974/3962 mV
    - Group 04: temps 34/35/35/36 °C (summer)
    - Group 02: 96 cells, min 3990 / max 4029 mV, **spread 39 mV**, pack 384.9 V
    - vs 2026-02-19: capacity down from 24.90 → 23.16 Ah (SOH 37.7% → 35.1%), spread up from ~30 → 39 mV
31. **Group 01 extended decode** (BLE probe, 3 samples):
    - Bytes 0-3 and 6-9: HV current sensors 1 & 2, signed 32-bit ÷1024 A (−1.52 / −1.51 A vs group 05 +1.21 A — same magnitude, opposite sign convention)
    - Bytes 18-19: **HV pack voltage ÷100** = 384.26 V (96-cell sum 384.3 V) — group 01 alone now yields power
    - Bytes 4-5 (0x0287) and 24-25 (0x00F2) static, unknown
32. Weakest cell pair today: **#55** (3986 mV); Feb it was #53. Highest #6 (4016 mV). Temps 34/35/34/36 °C = 93/95/93/97 °F.
33. Wrote `docs/ROADMAP.md` — codebase assessment (11 issues) + phased plan (foundation → dashboard → novel features → housekeeping).
34. **Convention adopted:** all temperatures displayed as °C / °F from now on (dashboard already does; console tools to be updated).

### Session 5 (cont.) — 2026-08-24 (Sprint A3 → A4 → A1 → A2 → B1)
35. **`leaf_decoders.py`** — single decoder module for groups 01–06; `decode_reading()` returns one flat record
    - Current sign convention standardized to the BMS's: **negative = discharge**, positive = charge/regen; `power_kw` signed the same way
    - Group 05 current scale changed from ×0.001 to **÷1024** (matches group-01 sensors within 0.05 A; 2.4 % difference from the old scale)
    - Group 04 returns °C **and °F**; `fmt_temp()` gives `34 °C / 93 °F`
    - Group 06: 24 data bytes → 96 × 2-bit balancing flags (tentative; 21 pairs flagged at rest today)
    - Group 03: bytes 10-13 = cell max/min mV (tentative)
36. **Tests** — `tests/` with 31 offline tests against `tests/fixtures/lbc_raw_20260824.json` (raw frames for all six groups). `./venv/bin/python -m pytest -q`
37. **SQLite store** — `web/store.py`, `web/leaf_battery.db`. Never prunes. Migrated 783 legacy rows (Feb 15 JSONL, Feb 19 JSON history, Feb 19 state snapshot). UTC timestamps.
38. **Reader supervisor** — `web/reader.py` rewritten: reconnect with back-off, `asleep` state on NO DATA, last-good reading preserved with `last_ok`, `--fast` group-01-only mode. Tested with a fake adapter.
39. **Dashboard** — power sign now from the BMS (not SOC delta); honest status dot (stale / asleep / reconnecting); 7d / 30d / All ranges served from SQLite; **new Capacity Degradation card** with least-squares fit and projection to 20 Ah. °F retained everywhere.
40. **Housekeeping** — `battery_read.py` unified console tool; five duplicate scripts moved to `legacy/`; `README.md`; `requirements.txt` pinned (pyserial, Flask, pytest); `live_stream.py` 0x174 label fixed.
41. Verified live over BLE: SOC 75.75 %, pack 383.5 V, −0.43 kW draw, spread 31 mV (min pair #55), temps 34/35/34/36 °C = 93/95/93/97 °F.

### Session 6 — 2026-08-24 (evening: probes, Car-CAN passive signals, HVAC amp, dashboard tiles)
42. **Dashboard crash** — combined Flask + BLE-reader process segfaulted (SIGSEGV in a background thread, CoreBluetooth callback) after 38 readings. Fix: `web/app.py` now runs `reader.py` as a supervised **subprocess** with auto-restart; a crash costs seconds, not the dashboard.
43. **`ATCAF0` is the key for passive Car-CAN** — every ID that showed `<DATA ERROR` in Feb (0x5B3, 0x292, 0x002…) decodes fine with auto-formatting OFF. `ATCAF1` is only needed for ISO-TP (UDS) responses. `elm327.passive_capture()` handles it.
44. **New Car-CAN signals** (`probe_hvac_carcan.py`, `tests/fixtures/probe_20260824_185139.json`):
    | ID | Signal | Reading | Status |
    |---|---|---|---|
    | `0x385` b2-5 | TPMS PSI = raw/4 (FL, FR, RR, RL) | 37.25 / 35.75 / 36.5 / 36.5 | ✅ |
    | `0x5C5` b1-3 | Odometer (units per 0x355) | 65,545 mi | ✅ (user to confirm) |
    | `0x5C5` b0 bit2 | Parking brake | set | ✅ |
    | `0x355` b6 bit5 | Units: 1 = miles | miles | ✅ |
    | `0x5B3` b1>>1 | SOH % (dash) | 35 % (LBC says 35.1 %) | ✅ |
    | `0x421` b0 | Gear: 0x08 = P confirmed; 10/18/20/38 = R/N/D/Eco from community DBC | P | ⚠ verify by shifting |
    | `0x60D` b1 bits1-2 | Start state 0 off/1 acc/2 on/3 ready; b0 door/lock/light bits | ready | ⚠ tentative |
    | `0x5A9` b1-2 >>4 | Range word (OVMS: /5 km) | raw 100 → 20 km / 12 mi | ⚠ check vs dash |
    | `0x284` b4-5 | Speed (≈ raw/100 km/h) | 0 | ⚠ scale tbc |
    | `0x180` b5, `0x292` b6 | Throttle %, brake | 0 / 0 | ⚠ |
45. **HVAC amp (0x744→0x764)** answers service 21 groups **01, 10, 11** only (02–0F, 12–20 → NRC 0x12; 22 xxxx → NRC 0x11).
    - Group 10 = 46 B: `49 3D 29 02 49 3D 00 29 02 00 80 85 61 …`. Reading bytes 0-3 as (raw − 40) °C gives ambient 33 °C / 91 °F, **cabin 21 °C / 70 °F**, evaporator ("intake") 1 °C / 34 °F, sunload 2 — coherent with AC running on a warm evening. **Tentative until the differential test.**
    - Group 11 = 11 B `06 40 00 00 00 00 00 00 0A 0A FF`; group 01 = 11 B (Feb's "static" payload).
46. **VCM (0x797→0x79A)**: NRC 0x80 for all 21/22 requests, 0x11 for 1A/09 — parked.
47. **USB adapter MS switch** cannot reach EV-CAN (pins 12/13): the switch selects pins 3/11. Re-pinned OBD extension needed for 0x54F cabin / 0x54C ambient / 0x1DB / 0x5BC etc.
48. Reader cycle now: LBC groups → HVAC 10/11/01 → staggered passive captures (0x421, 0x60D, 0x284 every cycle; 0x5C5, 0x385 every 2nd; 0x5B3, 0x5A9 every 3rd; 0x355 every 20th). New dashboard tiles: **Vehicle** (gear, odometer, range, state, brake, doors), **Tires** (top-down Leaf with four colour-coded PSI), **Climate** (cabin/ambient/evaporator °F/°C, sunload, raw bytes for calibration).
49. **Gear confirmed on 0x421** (`gear_hvac_live.py gear`, 19:03): P=`08`, **R=`10`**, **D=`20`** observed with matching 0x174 byte 3 (AA/99/BB). N=`18`, Eco=`38` expected from the DBC, not yet observed. Combined, 0x421 resolves the P-vs-N ambiguity that 0x174 cannot.
50. **Root cause of both dashboard crashes**: sqlite3 segfault from one shared connection used by concurrent Flask threads — not BLE. Fixed with a thread-local `Store` in `web/app.py`; 15 concurrent `/api/history` requests now pass. Reader stays a supervised subprocess.
51. **Cycle-time optimisation** — dashboard updates were ~25–30 s apart. Causes: 0.3 s post-prompt sleep on every `send()` (~35 cmds/cycle), `ATCAF0`+`ATCRA` per passive ID, 12-command ECU switches, cells every cycle, and `--interval` added *after* the cycle. Fixes: `wait=0` for AT commands, `ATCAF0` once per passive block, 4-command `set_uds_target()`, groups 02/06 every 2nd cycle, staggered passive plan, `--interval` = target period, `cycle_s` in state and on the Readings tile. **Result: 3.7–7.9 s cycles, updates every 8 s.** Over BLE each command round-trip is ~0.15 s; the remaining floor is the 29-frame cell read (~2 s) and the passive capture windows.
52. **All five gear codes confirmed on 0x421 byte 0** (second live capture, 19:12–19:13, P→R→N→D→Eco→P): **P=08, R=10, N=18, D=20, Eco=38**. 0x174 byte 3 showed AA/99/AA/BB/BB/AA in lockstep — proving it cannot separate P/N or D/Eco, which closes the Session 2 open question. Dashboard now has a console-style **shifter tile** (lit R/N/D labels, P button, ECO badge). Reader gained a pause file (`web/reader.pause`) so calibration tools can borrow the adapter while the dashboard stays up.
53. **Tile-driven scheduler** — `web/reader.py` rewritten around *items* (16 sources: 5 LBC groups, 2 HVAC groups, 9 passive IDs) each with a period. Period-0 fast lane (group 01, 0x421 gear, 0x358 turn) runs every cycle; the rest rotate by overdue ratio inside a 1.5 s budget, ordered LBC → HVAC → passive to minimise ECU switches. **Only items needed by enabled tiles are polled** (`web/tiles.json`, re-read on mtime change). Measured over BLE with everything on: **1.9–2.9 s per cycle** (from ~28 s at the start of the evening). SQLite rows every 5 s; state file every cycle; per-item ages published as `item_age`.
54. **Dynamic tiles** — dashboard is now a 12-column grid of `data-tile` cards. "Tiles ▾" menu toggles and orders tiles (▲▼ or drag the card title); persisted via `GET/PUT /api/tiles` so the reader honours it. Each slow-source tile shows "N s ago".
55. **Open-source scaffolding** — README rewritten, `docs/SIGNALS.md` (every ID/offset with verified/tentative status), `docs/ARCHITECTURE.md`, CONTRIBUTING, SECURITY (read-only rule), CODE_OF_CONDUCT, LICENSE (Apache-2.0) + NOTICE, `pyproject.toml`, `.gitignore` (DB, state, logs, `.claude/*` except skills/agents), repo `CLAUDE.md`. Git conventions adopted from Spiralyst/FAPD: `area: subject` + narrative body, main sacred, feature/bug/arch branches, never amend/squash. Local repo initialised (no remote yet).
56. **Tile Studio** — `signals.py` registry (38 signals: unit, range, decimals, source item, default colour scale, history column, °C twin), `/api/signals`, tiles config v2 (`span`, `type`, `opts`, user tiles with `kind: signal`). `web/static/tilestudio.js`: per-tile ⋯ menu (width 2–12, colour scale + invert + min/max, hide; signal/type/title/history-range/remove for user tiles), *Tiles ▾ → add*, drag-to-reorder, and generic renderers — number, ring, arc, dial, bar, thermometer, battery, line/area/bars, text, lamp — colour-encoded through 7 scales. The reader resolves a user tile's item through the registry, so an unused tile of any kind is not polled (verified: all built-ins off + one cabin-temp arc tile → only `hvac10` polled). `docs/ADDING_SIGNALS.md` is the six-step routine for new inputs.
57. **Current-sensor zero noise** (car READY, AC off, ~19:50): group-01 sensor 1 jumped −2.0 / +0.2 / +1.4 A; sensor 2 sat −0.15…+0.36 A; group 05 read −0.96 A (a plausible DC-DC idle draw) then +0.31 A with its discharge flag flipping to "not discharging"; SOC drifted 67.80 → 67.82. Under the AC load earlier sensor 2 and group 05 agreed within 0.05 A. Conclusion: **all three sources wander ±0.5 A around zero**, and the BMS flag follows the noise. Changes: `current_a` is sensor 2 every cycle with a learned EMA offset to group 05 (now polled every 5 s); positive readings while the flag says discharging are clamped; a per-car **zero-offset calibration** (`web/calibration.json`, "zero now" in the Power tile, `PUT /api/calibration {"zero_current": true}` — do it with the car ON but not READY so true current is 0); dashboard treats |I| < 0.6 A / |P| < 250 W as IDLE and paints it grey. Raw values are kept (`current_raw_a`, `g05_current_a`, `s2_offset_a`).
58. **Free layout** — the flow grid is replaced by a layout engine: 12 columns × 40 px rows, tiles carry `x, y, span, h` (server validates/clamps). Grab the title bar to move, the bottom-right corner to resize width and height; collisions push tiles down, gaps compact upward; unplaced tiles auto-flow and measure their own height. Engine functions (`overlap`, `firstFit`, `resolve`) run under node in `tests/layout_engine_test.js`, wired into pytest (skips without node).
59. **gridstack.js replaces the hand-rolled layout engine.** Surveyed the well-known options: gridstack (2D cell grid, drag by handle, corner resize, push-aside + compaction, save/load, one-column mode, MIT, no deps), Muuri (masonry reorder, no cells/resize), react-grid-layout (React only), SortableJS/interact.js (primitives). Vendored gridstack 13.2.0 under `web/static/vendor/` with its MIT licence (credited in NOTICE); `tilestudio.js` wraps each card in a `.grid-stack-item`, drives `makeWidget/update/removeWidget`, and persists `x/y/span/h` from the `change` event. Same data model as before, so `web/tiles.json` and the server did not change. The custom engine and its node harness are gone; tests now check the vendor files and that the studio parses.
60. **Named layouts + per-tile reset** — `web/layouts.json` (gitignored) holds saved arrangements; `GET /api/layouts`, `PUT /api/layouts/<name>` (current or explicit tiles), `POST /api/layouts/<name>/load` (overwrites `tiles.json`, reader follows), `DELETE`. Tiles ▾ panel: select / load / delete / save-as, "reset to default" with confirm. ⋯ menu: **reset tile** (registry default span, type number, opts cleared, title cleared, auto-position, height re-measured). `reloadLayout()` tears widgets out of gridstack and re-places from config so a loaded layout lands exactly as saved.
61. **Fan walk (calibrate_input.py fan, 22:14)** — group 10 **byte 11** follows the fan: `84 85 86 88 89 8B 8B` for speeds 1–7 going up and the same values coming down; bit 7 = blower on, low bits = **blower motor volts** (4, 5, 6, 8, 9, 11, ~12). Speed 7 sampled at 11 V while still ramping — 6 vs 7 tentative. Other movers (bytes 2, 7, 12, 25) drifted with time, not speed: temperatures rising with the A/C off. Byte 10 was `80` earlier with A/C on and `00` with it off → first candidate for the A/C walk. Decoder: `hvac_fan_on`, `hvac_blower_v`, `hvac_fan_speed` (nearest-volts table); Climate tile has a fan rotor spinning at a rate ∝ volts plus a 7-bar level. Walker gained presets for the owner's button set — `hvac`, `ac`, `fresh`, `recirc`, `auto`, `mode` (4-position cycle, two passes), `setpoint` (60→90→60 °F) — chainable in one session (`calibrate_input.py all`).
62. **HVAC walks (on/off, A/C, fresh, recirc)** — byte 11 → `00` with the HVAC OFF button (bit 7 = system/blower on); **byte 10 bit 7 = A/C compressor on** (consistent across five toggles); bytes 21–22 read 1600–2425 with A/C on → compressor rpm (tentative); 23/24 scale with it; 38/39 = 00 / 36 / 64-67 by state. **Fresh/recirc moved nothing** in groups 10/11/01. Swept every service-21 group: only 00, 01, 10, 11, 82, 83 answer (`tests/fixtures/hvac_group_sweep.json`); group 00 (`80 01 80 00`) added to the walker and reader as the last candidate for door/mode/auto flags. AUTO walk reworded (AUTO only switches on; leave it via the fan knob). Walker now writes after every step and `--from` resumes a chain.
63. **Auto / mode / setpoint walks** — **byte 12 tracks the setpoint** (111→173 for 60→90 °F, ≈ 11 per 5 °F, 1–3 counts of lag on the way down → air-mix target; decoded as `hvac_target_f` ±2 °F, verified against all 13 steps). **Byte 36 = heater demand** (0 at 60–65 °F, 3→40 with the PTC working; bytes 29/31 light up alongside — current/kW candidates). **Mode (8 steps), AUTO (5) and intake: nothing moved** anywhere, including group 00, which stayed `80 01 80 00` — pinned by a test as a documented negative. Bytes 38/39 jumped at lower+defrost and stayed: defrost forcing the A/C on, not the mode. Climate tile now shows setpoint, A/C + compressor rpm, heater demand, system on/off.
64. Fan 6 vs 7: both read `8B` (11 V) in every sample of the walk — the amp does not distinguish them; tile shows "6–7" at 11 V. Climate tile raw-hex rows removed (raw stays in the state/API). `hvac10` period 10 s → 3 s so climate changes show within a few seconds. Flask template auto-reload enabled after a stale-tile confusion.
65. **Decision: control is out of scope here.** Confirmed the BLE adapter *can* transmit but cannot realistically change the fan on Car-CAN (amp control frames are EV-CAN; UDS writes need a session + likely security access; the real panel out-transmits any injection; ELM327 clones are poor at periodic TX). Programmatic HVAC/vehicle control moves to a future sibling project (Leaf_Control) gated behind native CAN hardware; this repo stays read-only so it can go public. Recorded in docs/ROADMAP.md.
66. **Tires → wheel profiles; new Body tile.** Tires tile is now four side-profile wheels (tyre ring coloured by pressure, rim, hub, spokes, PSI); sub-5 psi on all four reads as "sensors asleep — drive to wake TPMS" (parked, sensors dormant). The overhead car moves to a new **Body** tile (`p60D`+`p5C5`): 4 doors that swing open + colour orange, a hinged hatch, headlights that glow, and a lock indicator (🔒/🔓). Door bits are still the tentative 0x60D map (front/rear/hatch grouped, not per-corner) — the `doors`/`locks` walk will pin them and the tile splits per corner then.
67. **Doors + locks decoded (walks 11:00/11:05, clean single-bit results).** `0x60D` byte 0 = **per-corner door open flags**: `08` driver, `10` front passenger, `20` rear-left, `40` rear-right, `80` hatch — each door its own bit, baseline `00`, no overlap up or down. `0x60D` byte 2 = **lock**: `18` locked, `00` unlocked (both presses, both directions). Byte 1 stayed `06` = start-state READY (bits 1-2, restored after I briefly dropped it). `0x5C5` and `0x625` did not move for doors or locks. Decoder now emits door_driver/pass/rl/rr/hatch, door_any, locked (+ legacy front/rear/trunk aliases); Body tile lights the exact corner that's open and the 🔒/🔓 matches. Each door/lock/hatch is a registered signal (own tile-able). Tests build 0x60D lines from both walk fixtures and assert one bit per step.
68. **Body tile "all out".** Rebuilt the overhead car with hinged doors (rotate about the front edge — fixes the floating-door look), per-door padlocks (red closed when locked, grey open when unlocked), and the full exterior-lamp set: headlights, parking/position, fog, front+rear turn signals, **side repeaters**, brake, reverse. Wired from verified data: doors/locks (0x60D), turn+hazards+side repeaters (0x358, amber blink animation), brake (0x292 b6>0, tentative), reverse (gear=R). Added `p292` brake item (period 1) and dropped 0x60D poll to 3 s so the tile is responsive; body tile items = p60D/p358/p421/p292. Headlights still the tentative 0x60D bit; **parking and fog are undecoded** — added a `lights` walk preset (off→parking→low→high→low→fog→off, passive over 60D/625/358/5C5) to find them. No horn on CAN (as expected — not pursued).
69. Body tile fixes: door swing signs flipped so doors open OUTWARD (left doors +52°, right -52° about the front-edge hinge — screenshot showed them pivoting into the cabin); turn-signal blink hardened (CSS animation owns opacity, 0.48 s on/off, no inline-style conflict). Lights walk rebuilt to the real ZE0 stalk sequence: OFF → AUTO (1st click) → PARKING (2nd) → HEADLIGHTS (3rd) → HIGH BEAM (push) → low → OFF → FOG on/off (separate switch). AUTO in daylight may leave lamps off — the walk reads switch state, not lamp output.
70. **Lights walk decoded.** 0x60D b0: `0x04` parking/position, `0x02` low beam. 0x60D b1: `0x08` high beam, `0x01` fog (bits 1-2 remain start-state; verified the extra bits don't disturb READY). 0x625 b1 mirrors it as a clean bitfield (0x40/0x20/0x10/0x08 = park/low/high/fog). AUTO in daylight reads `00` (lamps off) — switch mode not distinguishable from OFF, as expected. My earlier tentative `0x60D` b0 0x02 headlight guess is now confirmed. Body tile: high beam whitens + widens the headlight glow, legend shows headlights/high beam/parking/fog. New signals parking_lights, high_beam, fog_lights.
71. Body-lamp visuals fixed: `setLamp` now takes glow radius + opacity. Low beam = soft white (small glow), **high beam = bright blue, large glow** (was indistinguishable). Fog lamps get a strong yellow glow so the toggle shows. **Rear lamps now behave as tail+brake**: dim red when parking/headlights are on, bright red (bigger glow) when braking — previously the rear only lit on brake and parking left it dark.
72. **Housekeeping for the public push.** Merged feature/tile-studio to main (26 commits, ff). Verified the whole stack on **Python 3.12.13**: 71 tests pass, bleak + CoreBluetooth + pyobjc 12 import, and the dashboard read the live car over BLE. Rebuilt `./venv` on 3.12 (was Xcode CLT 3.9; pyobjc 12 actually needs ≥3.10). Bumped `requires-python` to >=3.10; updated README/CLAUDE.md/requirements. Added `.gitattributes` (fixtures linguist-generated, vendor linguist-vendored). Removed the aborted duplicate lights fixture. Refreshed README status/test-count (71). Confirmed all runtime files (tiles/layouts/calibration/state/db/reader.pause/config.local/research/logs) are gitignored.
73. **Sleep/lid-close recovery.** On lid-close the process suspends and CoreBluetooth drops the BLE link; on wake bleak often still reported the client "connected", so `send()` timed out silently and the reader couldn't tell a dropped link from a sleeping car — it fell into the 60 s asleep heartbeat and never reconnected (stale dashboard). Fix, two layers: `BleELM` now registers a `disconnected_callback`, bounds `connect()` with a 20 s timeout, and `send()` raises `ConnectionError` if the link is down or a write fails instead of hanging; the reader adds `probe_alive()` — an `ATI` to the adapter (powered from OBD pin 16, so it answers when the car is merely asleep but times out when BLE is dead), and on no-CAN-data it probes and **reconnects if the adapter is silent**, only heartbeating "asleep" when the adapter itself answers. New test `test_dropped_link_reconnects`; 72 tests.
74. **Sleep recovery, the actual fix.** Logs from a real lid-close showed the drop WAS detected (write TimeoutError → reconnect) but every reconnect then failed with bleak "Device with address … was not found". Root cause: macOS/CoreBluetooth invalidates a peripheral's session UUID across sleep, so a direct connect-by-address never succeeds again — you must re-discover (scan) the peripheral first. `BleELM.connect()` now always scans (`find_device_by_address`, else `find_device_by_name`) and connects to the found BLEDevice. Reconnect logging is timestamped and step-by-step (detecting → scanning → found → connected → configured, and "RECONNECTED after N failed attempts"); backoff cap lowered 30 s → 15 s. Verified the connect trace live.
75. **Sleep recovery, root cause and real fix.** Verbose logs from a second lid-close proved the reconnect was scanning but finding *nothing* for 6+ minutes — not a timing issue. macOS leaves the process's CoreBluetooth central manager stalled across sleep; no scan in that process ever sees the adapter again (a new BleakScanner instance doesn't help — it's process-level). The only cure is a fresh process. So the reader now **exits after the first failed reconnect** (`MAX_DETECT_ATTEMPTS = 1`) and `web/app.py`'s subprocess supervisor relaunches it — a brand-new process gets a clean CoreBluetooth and scans/connects normally. If the adapter is actually present (an awake blip), the first attempt's scan finds it and reconnects in-process with no restart; only a scan-finds-nothing failure (the stall, or a truly-gone dongle) triggers the process restart. Recovery ≈ 20 s after wake instead of never. Scan timeouts trimmed to 8 s / 6 s.
76. **charge_report.py** — reconciles our DC pack energy against a metered AC charge bill (Blink). Per session (explicit `--session START,END,kWh` or auto-detected from a `--date` as runs of positive power) it integrates pack energy, uses the gap-immune SOC-based pack gain as primary, reads A/C duty + compressor rpm + cabin/ambient/pack temps from the store's `extra` JSON, and breaks the wall energy into: into-pack / A/C compressor / Leaf 12 V / accessory DC-DC (e.g. Victron `--victron-a`) / charger loss, with `--price` cost attribution and an A/C $/hour figure. 2026-08-25 Long Beach report (2 Blink sessions, A/C on 95 %, Victron charging a 100 Ah LiFePO4) saved to `research/` (gitignored — has location/receipts). Confirmed granularity: HVAC on/rpm are sampled ~5 s but live in `extra` JSON, not columns.

---

## Data-logging sprint — detailed progress (`feature/logging-granularity`)

### Phase 1 — promote high-value signals to columns  ✅ 2026-08-25
**What & why.** Aggregate questions like "how long was the A/C compressor on, at
what RPM" required parsing the `extra` JSON blob on every row (what
`charge_report.analyse` does). Promoted 11 high-value signals to first-class,
indexed columns so those become plain SQL: `hvac_ac_on`,
`hvac_compressor_rpm`, `hvac_on`, `hvac_fan_on`, `hvac_fan_speed`,
`hvac_heater_level`, `cabin_temp_c`, `hvac_ambient_c`, `hvac_evap_c`, `gear`,
`speed_mph`.

**How.** `web/store.py`: a `PROMOTED` map (col → sql-type, rec-key, kind) drives
everything. The `readings` DDL gains the columns for fresh DBs; `_migrate_columns()`
(run in `__init__` after the schema) `ALTER TABLE ADD COLUMN`s any missing on an
existing personal DB, creates a partial index `idx_readings_ac … WHERE
hvac_ac_on=1`, then `_backfill_promoted()` one-shot-populates the new columns
from each row's `extra` via `json_extract` (Python fallback if a SQLite lacks
json1), guarded by a `meta` row so it never re-runs. `insert_reading` now writes
the promoted keys into the row (booleans as 0/1) and — because they're in the
row dict — they're automatically excluded from `extra`, so no duplication.

**Design line held.** Only signals present on nearly every row *and* worth
filtering/aggregating became columns. Raw diagnostic bytes (`hvac_g10_raw`,
segment deltas), rare passive one-offs (odometer, range), and per-tile display
values stay in `extra`. State *transitions* (doors, locks, charge start/stop)
are Phase 2's events table, not columns — a mostly-constant flag column still
costs a full scan to find its edges.

**Verified.** 75 tests (3 new: columns written + not duplicated in extra; a
columnar A/C count/avg query; a migrate+back-fill from a synthetic old-schema DB,
idempotent on re-open). Ran the migration on a *copy* of the live 4,735-row DB:
all columns added, 2,902 A/C-on rows and 3,079 rpm rows back-filled (avg 2,102,
max 3,508), and "today's A/C-on time" is now one indexed query (~4.0 h) instead
of a JSON scan. The live DB migrates on the next app restart.

### Phase 2 — events table for state transitions  ✅ 2026-08-25
**What & why.** Column aggregates answer "how long/what value" only to the store's
5 s sample resolution, and can miss a state that flips and flips back between
rows. An `events` table records each *transition* exactly when it happens, so
on-time is precise and independent of sampling.

**How.** `web/store.py`: `events(id, ts, ts_epoch, name, value, prev)` +
`idx_events_name`; `ev_norm()` normalises a value to stable text (`True→'1'`,
`False→'0'`, gear `'D'`, …). Methods: `insert_event(name, value, prev, ts)`,
`events(name, t0, t1)`, and `on_time(name, t0, t1, on='1')` which walks the
paired transitions — seeding the state at `t0` from the last event at/before it,
and clamping any still-open interval to `t1`. `web/reader.py`: a `WATCH` set of
clean discrete signals (A/C on, HVAC on, gear, locked, door_any, handbrake, high
beam, fog) and `emit_events()` called every poll cycle (not just store cycles),
which diffs each watched signal against `prev_watch` and writes an event on
change — a baseline event on first sight so `on_time` has a starting state even
if the signal never changes during a window.

**Deliberately excluded.** `charging` was left out of the watch set for now: near
the charge/idle boundary the net current wanders and a naive threshold would
flap; charge-session detection stays with `charge_report`'s power-run logic until
a hysteresis state is added (future).

**Verified.** 78 tests (3 new: `on_time` returns 120 s for an on/off pair with no
readings between and honours a window that starts mid-state; events query returns
values/prev in order; the reader emits a baseline then a transition, no duplicate
when unchanged, and tracks gear changes).

### Phase 4 — charge report from the web app  ✅ 2026-08-25
**What & why.** The charge-energy reconciliation existed only as a CLI that
parsed `extra` JSON. Turned it into a web feature over a user-defined range,
and made it use the Phase-1 columns and Phase-2 events instead of JSON scans.

**How.** `charge_report.py` refactored into an importable module: a
`ReportParams` dataclass replaces the argparse-object reads; `analyse(store,
t0, t1)` reads the promoted columns (`hvac_compressor_rpm`, `cabin_temp_c`,
`capacity_ah`, …) directly and gets A/C duty from `store.on_time("hvac_ac_on")`
(exact) — falling back to the `hvac_ac_on` column sample fraction for windows
logged before events existed, with the source labelled honestly ("from events"
vs "from samples"); `reconcile`, `build_report`, `render_markdown` are pure and
composable; the CLI is a thin `main()`. The dead `_extra_first` and its silent
`23.16` fallback are gone (capacity now the mean of the `capacity_ah` column).
`web/app.py`: `GET/POST /api/charge-report` — POST takes multiple sessions,
GET a single from/to or a `date` to auto-detect; read-only via the request-thread
store, a 3-day range guard, and it renders on demand and **never persists**
personal data server-side. Dashboard: a **⚡ Report** header button opens a modal
with from/to (`datetime-local`, converted to UTC), metered kWh, price, and an
Advanced section (charger efficiency, accessory DC-DC amps); "Run" POSTs and
shows the markdown; "Download .md" is a client-side Blob (never touches disk).

**Verified.** 83 tests (5 new: reconcile sums to the billed kWh; A/C duty comes
from events not sample spacing; build+render smoke; empty window; and a Flask
test-client POST that returns markdown/totals and enforces the range guard).
Live against the migrated 4,857-row DB, the endpoint reproduces the CLI numbers
exactly (13.22 kWh billed, 30% reached the pack, A/C ≈ $0.98/h day average).

### Phase 4 removed  2026-08-25
The owner decided against the charge-report feature and did not want it
deployed. Removed `charge_report.py`, the `/api/charge-report` endpoint, the
**⚡ Report** dashboard button + modal, and `tests/test_charge_report.py`. The
Phase 1 promoted columns and Phase 2 events table are **kept** — they are the
data-logging granularity improvement in their own right, not the report. The
one-off `research/charge_report_2026-08-25.md` analysis stays local (gitignored).
Phase 4's commits remain in history (never rewritten, per the repo rules); the
working tree no longer carries the feature.

### Pre-release doc-truth audit, part 1  2026-08-26
Fanned out audits of the docs vs the code ahead of open-sourcing. Part 1
(README / ARCHITECTURE / ADDING_SIGNALS / legacy/README) found 17 drifts —
6 blockers — all fixed: test count 71→78 (badge, quick start, status); the
**Body tile was missing entirely** from README's tile table and ARCHITECTURE's
tile list (added); Tires described as "top-down car" from before the redesign
(now wheel profiles); Climate refresh said 10 s (is 3 s); "38 registered
signals" (is 57); Vehicle refresh range corrected to 2 s–5 min; Power notes
group-05's 5 s half; console tools added to the repo-layout table;
ARCHITECTURE's data model now covers the promoted columns + events table and
the calibration config/API; ADDING_SIGNALS documents the `kind`/`alt_unit`
registry fields; legacy/README no longer points a superseded script at another
superseded script and names the walker; app.py's stale docstring (4 of 10
routes, wrong default interval) fixed; the Tiles panel no longer hardcodes
"of 17 sources" (ITEMS is 18 and moving — now just counts). Verified-true
coverage from the audit: API table exact, all quick-start commands/flags real,
no charge-report leftovers, Python/gridstack version claims right, gitignore
claims all true. Part 2 (SIGNALS.md byte tables, CLAUDE/CONTRIBUTING/SECURITY/
NOTICE) in flight.

### Pre-release doc-truth audit, part 2  2026-08-26
Part 2 covered SIGNALS.md byte tables (decoded through the real decoders and
fixtures), the policy docs and .gitignore. Two blockers: SECURITY.md and
README claimed *every* command is a service-0x21 read — true for the reader,
but the probe tools send read-identification services 0x22/0x1A/mode 09 and a
legacy script sends 0x10 session control; both docs now say exactly that
(still nothing writes, actuates or requests security access — grep-verified).
And 0x358's `86` hazards value wore a "verified" label though the only capture
holds only off/left/right — relabelled tentative (community value). Honesty
pass on the rest: a new **Static** confidence tier (externally checked, never
seen changing) now covers TPMS/odometer/handbrake/units/dash-SOH instead of
"verified"; 0x625 demoted to observed-not-decoded; 0x174/0x180 marked
decoded-but-not-polled; the "ECUs that do not answer" table (which contained
answering ECUs) is now "Other ECU probes"; the EV-CAN transport row no longer
contradicts the re-pinned-cable section; payload sizes state declared vs
padded lengths; the 0.6 A idle band correctly attributed to the dashboard;
the orphan group-10 table row merged. ROADMAP: stale/duplicated "Next up"
line rewritten (setpoint calibration, N/Eco, drag-resize all done), the
2026-08-24 codebase snapshot explicitly marked historical, Phase 2 watch set
corrected, charge-report references qualified as removed, A1's history API
described as shipped. SECURITY's in-scope list names all three writing APIs.
.gitignore gains leaf_battery.db-journal (WAL-fallback) and the promised
retention.json. Verified-true haul was large: every LBC group offset/scale,
the full transport sequence, the HVAC sweep results, every walk-verified
Car-CAN row, all CLAUDE/CONTRIBUTING conventions, NOTICE/LICENSE consistency.
78 tests, sweep clean. Both audit parts done — release-doc truth established.

### The vehicle-profile seam, and a second car  2026-08-28
The Lancer came off the back burner: BLE dongle moved to the 2009 Lancer ES,
and — unlike the Leaf — it answers standard mode-01 PIDs (39 supported).
First console read in minutes (`Lancer_Testing/lancer_read.py`, reusing our
elm327 transport): coolant 203 °F, 13.84 V charging, 719 rpm idle. Then a
20-minute idle logger (`lancer_log.py`, JSONL + first-cycle raw fixture).

While it logged, cut the seam this month's market-direction note called the
load-bearing prerequisite: a `vehicles/` package. A profile bundles ITEMS +
TARGETS (UDS headers or passive), KIND_ORDER, TILES/DEFAULT_TILES, the
SIGNALS registry entries, WATCH, configure(), decode(responses)→(rec,alive),
and an optional apply_policy (the Leaf's current fusion moved there, its
EMA state in a profile-owned dict). `reader.set_vehicle()` binds the profile
to the reader's module globals — deliberately, so the 22 scheduler tests and
app.py keep reading `reader.ITEMS` unchanged — and rebinds `signals.SIGNALS`.
`--vehicle` on app.py/reader.py or `"vehicle"` in config.local.json selects;
scheduler, store, supervisor, API, Tile Studio needed no vehicle knowledge
beyond what fell out: estimate() reads per-item `est`, switch() reads
TARGETS, default layouts may be signal tiles (built-in tiles stay Leaf SVGs).
`vehicles/lancer_2009.py` is 15 items of plain SAE J1979 — the whole profile
is a page, which was the point. One regression during surgery (the span
replace ate TILE_FIELDS; every reader test failed the same way) — restored.
87 tests: the 78 plus profile contracts, °F-with-°C rule enforced per
profile, Lancer decode against the morning's real idle capture, and
set_vehicle round-trip. Idle log runs in the background meanwhile; analysis
next. Also noted: the Lancer's "ambient" PID reads 61 °C at idle — engine-bay
heat soak, not weather.

### DTC readout — the code tile, and what the Lancer confessed  2026-08-28
Read the Lancer's trouble codes (modes 0101/03/07, read-only — never mode
04/clear): MIL ON with 12 stored engine codes + CVT P0868. The first
functional-address read only got single-frame answers — a 12-code mode-03
response is multi-frame and functional 7DF can't do ISO-TP flow control;
re-read physically addressed (7E0/7E8, 7E1/7E9, FC set) and captured the
raw exchange as tests/fixtures/lancer_dtc_raw_20260828.json. Per the
owner's suggestion, Ha-Kake grew a code readout: the Lancer profile now
polls MIL/count (60 s) and stored/pending/trans codes (300 s), decodes via
the shared parse_isotp (whose stripped-echo behaviour made "no codes" and
"no answer" ambiguous — the decoder now detects the 0x43/0x47 response byte
in raw frames first), and ships default lamp + text tiles (u_mil, u_dtc).
Zero UI changes needed — lamp and text renderers already existed; a second
UDS target kind (pid_t → 7E1/7E9) exercised the seam's TARGETS design.
90 tests. The codes themselves: a dead upstream O2 story (P0131/32/34,
P2195, P0171 lean), an electronic-throttle plausibility cluster
(P0122/P0223 + Mitsubishi P1233/34/35 torque-monitor trio), P1590 CVT↔ECM
torque-request comms, P0868 CVT secondary pressure. Pending = only the O2
pair, so that's the live fault; the throttle/CVT set reads like a
low-voltage or connector event snapshot.
Correction to the entry above: the suite is 89 tests, not 90 (87 + the two
DTC tests). The commit message for f85bad5 repeats the miscount; the code
and fixtures are as described.

### The simulator grows up: a load model, a cockpit, and a seam it exposed  2026-09-03
Context for a log that skipped a week: at the start of September the project
gained a way to run with no car at all — a replay transport that plays a
recorded session fixture through the whole stack ("test: replay the car from
a fixture"), then a mock Leaf with knobs and a control API ("sim: a mock Leaf
with knobs"), a history generator that writes months of realistic rows in
seconds ("sim: generate months of history in seconds"), and a first control
panel ("sim: a control panel that looks like the car, and two bugs it
exposed" — the two being `--speed` multiplying a scenario's `clock_scale`
into 14400× and forward Euler blowing up an 80 °C pack; both fixed, both
tested).

The owner drove that panel and sent a review. His framing settled what the
simulator is *for*: a development fixture — a stand-in car for working on
the app's tiles, charts and reports — not a signal verifier. Against that
bar the v1 model failed in a way no test had noticed: `current()` was a
three-way selector (charging, else `load_kw`, else the raw `current_a`
knob), so the A/C, the 5 kW heater, the blower and the headlights changed
the pack current by **exactly zero watts**, and the −1.5 A idle default
(~576 W) was neither measured nor gated on READY — a powered-off car drained
35 kWh a day. Five knobs were °C-only against the house rule. The panel was
a listing with raw knob names. Launching was confusing: `--pty` never started
the dashboard, the command that did defaulted its control port off, and
neither page linked to the other.

Four commits on `feature/simulator`, three of them written concurrently by
sub-agents with disjoint files, one after:

- **"dashboard: extract the four styled tiles and make Tile Studio a
  library."** `tilestudio.js` now waits for `TileStudio.init(opts)` instead
  of booting itself; the vehicle, tires, body and climate tiles moved
  verbatim into `web/templates/tiles/*.html` partials with their update code
  in `web/static/tiles.js`. Behaviour-preserving by construction: the
  rendered page before and after differs only in the link tags, the script
  tag and the `init()` call, and the CSS rules match as multisets.
- **"sim: a real load model, °F everywhere, the dashboard's vocabulary, and
  lamps."** `current()` is a sum over a `LOADS_W` table with a provenance
  comment on every row — READY base 150 W and the beam deltas MEASURED
  (kelvin shunt at the cell interconnects, the mynissanleaf Lab Test
  thread), A/C 1.5–3 kW and PTC 5 kW from the owner's own reports, blower
  and the small lamps ASSERTED and saying so; HVAC and motor only in READY;
  motor and regen a road-load shape labelled ASSERTED for the driving capture
  to calibrate. `current_a` kept its name and became *extra* current on top
  of the model, default 0 (renaming it would have broken the contract,
  sixteen test sites and five scenarios, which were re-authored to drive
  pedals and speed). Every temperature gained its `_f` twin. `record()`
  emits the decoder vocabulary the tiles read, held equal to
  encode → decode on sixty keys. `lamps()` drives twenty-one cluster
  indicators and lists the eleven the model cannot drive as unmodelled
  rather than faking them. READY idle now reads 150 W and −0.39 A.
- **"sim: one command starts the car, the dashboard and the panel."**
  `web/app.py --adapter sim` defaults `--sim-control` to 8099 (0 = free
  port, `--no-sim-control` opts out) and prints all three URLs; `--pty`
  prints the exact app command and gained `--launch-dashboard`. Defaulting
  the port on exposed a crash loop — a stale rig holding 8099 killed the
  reader child with `OSError` forever — now a logged retry on a free port.
  The API grew `GET /sim/record`, `POST /sim/step {sim_seconds}` and
  scenario clearing. And a seam bug: the Lancer core has no `record()`, and
  the first cut answered **500** — a broken-looking API for a core that was
  merely honest. It is a **501** now, looked up per request, and that 501 is
  exactly how the cockpit hides the Leaf tiles for a profile with no profile
  code.
- **"sim: the cockpit — drive the mock car from a page that looks like the
  car."** `GET /sim` on the dashboard, built on the dashboard's own tile engine and the four extracted
  tiles made interactive — drag a tyre's slider, click a door or a lamp,
  click the shifter — plus the emulated cluster with a full ZE0 indicator
  strip lit from `lamps` (dim, dashed glyphs for the unmodelled ones), the
  head unit with its documented-negative buttons still inert, a Simulated
  time card with skip-ahead, and one card per knob category generated from
  the schema with labels as titles. Layout persists in `web/sim_tiles.json`.
  The page always renders; with nothing behind it, it shows the two launch
  commands.

Mid-flight, a session rate limit killed all three concurrent agents.
Recovery worked only because the working tree happened to be inspected
before anything else; that is now a rule — CLAUDE.md §3b: every delegated
task keeps an append-only progress log under `research/agent-logs/` with the
brief, file ownership, done/pending and a current `NEXT:` line, written
before any code ("docs: sub-agent tasks keep a progress log so they can be
resumed").

Docs caught up in this entry's commit: `docs/SIMULATOR.md` now leads with the
one launch command and describes the cockpit, the load table with its
provenance, the °F rule and the full control API; the contract
(`docs/SIMULATOR_CONTRACT.md`) stays as the interface stubs and sub-agents
build against; ARCHITECTURE's Testing section finally describes replay and
the simulator. 648 tests (460 at the v1 panel, 611 after the three parallel
commits), sweep clean, live database untouched.

Still open, and it needs the car: a logged drive (`docs/ROADMAP.md`, "Open:
capture a real drive") to turn the motor and regen rows from ASSERTED into
measured, settle the group-05 current scale above 32 A, and calibrate the
pack's temperature rise under load.

### Docs catch up with two commits, and a rule about why they didn't  2026-09-03

Two commits landed on `feature/simulator` after the last documentation pass
(`c7d9944`), and neither carried its docs. `b81018e` portalled the per-tile ⋯
menu out of the card that was clipping it — right-aligned under its button,
flipped when there is no room below, clamped eight pixels inside the viewport,
280 px wide, followed through scrolling and gridstack's own move animation by a
requestAnimationFrame tracker; ⋯ toggles it, Escape closes it, and a *Done*
button sits in the foot — and made the tire art a centred 2×2 block sized by
the card through container units rather than by the window. `e7b1161` collapsed
the car's power into one identity summed in one place — wall → charger → loads
→ pack — so the A/C draws while the car charges, audited twenty couplings in
`model.py` with a verdict and a reason each, drove compressor rpm from A/C
demand instead of fan speed, left `lv_volts` integrated rather than forced to a
14 V DC-DC bus because the owner's own car reads 12.64–12.74 V across 102 READY
samples, and gave the model the 2012 manual's push-button start.

So this was a third truth audit, in the shape of the first two. Every claim in
`README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/SIGNALS.md`,
`docs/ADDING_SIGNALS.md`, `docs/ROADMAP.md`, `docs/SIMULATOR.md`,
`docs/SIMULATOR_CONTRACT.md`, `docs/REPLAY.md`, the eight reverse-engineering
chapters, `CONTRIBUTING.md`, `SECURITY.md`, `NOTICE` and the issue and pull
request templates was checked against the code — flags against `--help`, routes
against the source and against a running rig, counts against the objects
themselves. The docs came out of it well; most of what was wrong was small and
old. What was found:

- **The test badge said 648 in three places in the README and once in
  ARCHITECTURE.** It is 714.
- **`docs/ROADMAP.md`'s status line stopped at 2026-08-28**, with no mention of
  replay or the simulator. **`CONTRIBUTING.md` and the new-vehicle issue
  template still told contributors the no-hardware replay harness was "planned,
  not built"** and that validating a profile "still needs the actual car" —
  which has not been true since `--adapter replay` shipped, and was the most
  misleading sentence in the repository.
- **`SECURITY.md` opened with "This software writes to your car's diagnostic
  bus"**, one line above the read-only claim. It meant "puts frames on the
  wire"; it now says so. Its list of write surfaces had also missed
  `PUT /api/sim/tiles` and the simulator's unauthenticated control API.
- **Chapter 08's `HISTORY_COLS` example wrote `"hist_f": True`.** `hist_f` is
  the *name* the °F twin takes, and `validate_profile()` rejects it without a
  `hist`; anyone copying the snippet would have got a silently useless key.
- **`SIMULATOR.md`'s road-load figures were stale** — `cruise_kw()` gives 11 kW
  at 55 mph and 19 at 70, not 9.5 and 15 — and its generator sample output was
  from before the power model changed: 180 days at seed 1 now writes 49,553
  rows and 3,882 events in half a minute, not 50,967 and 3,667 in thirteen
  seconds. The `--seed` row promised "byte for byte" without saying that the
  generated window ends at *now*, so the same command on a different day starts
  on a different weekday and lands somewhere else.
- Smaller: `CLAUDE.md` and ARCHITECTURE both claimed "the cockpit and both
  panels generate every control from `/sim/schema`" — the cockpit does; the
  control API's landing page is a curl-and-endpoints fallback that generates
  nothing (it does keep the no-knob-list rule, and tests enforce that on both).
  ARCHITECTURE still called `index.html` the home of all eleven built-in tiles
  after four moved into `templates/tiles/` partials, and summarised `WATCH` as
  five things when it is eight. Chapter 06 promised "seven wrong hypotheses"
  over six plus a case it explicitly says is not a mistake. The contract listed
  `ambient_c` as a rig knob (it is climate), omitted `clamped` from
  `time_scale_info()`, and both simulator docs printed an illustrative timeline
  under the name and seed of the real `drive` scenario.

Nothing in `docs/SIGNALS.md` needed touching: no decoder, profile or fixture
changed in either commit, and the byte offsets, CAN IDs, scale factors and
confidence tiers were left exactly as they were, ÷1024 notes included.

Three things are **reported, not fixed**, because they are code rather than
docs. `.githooks/pre-push` still says in a comment that "CI is planned and not
yet built"; `.github/workflows/ci.yml` has run both gates for a while.
`util.fmt_temp_f` and its `simulator.units` re-export have no callers anywhere
— the sim-side °F formatter the docs describe is real but unused, and the doc
now says so rather than claiming everything sim-side goes through it. And
`press_power(hold=True)` applies the emergency shut-off whenever the car is
moving, without first checking it is READY; unreachable through the cockpit,
since nothing but READY drives the motor, but `speed_mph` is independently
settable.

The interesting finding is not any of those. It is *why* they accumulated: the
work was split across sub-agents with narrow file ownership, and no lane owned
`README.md` or `docs/`. The rule that came out of the last session covered
losing an agent; this one covers losing the documentation. `CLAUDE.md` §3b now
requires a brief to name the living docs its change invalidates and put them in
that lane's ownership — or the phase ends with an explicit doc-sync step named
in the plan. §2 does not bend: docs land in the same commit as the change.

714 tests, unchanged (this was a documentation pass; not a line of code, test
or fixture was touched). Privacy sweep clean. `web/leaf_battery.db` never
opened.

### A real drive, and the group-05 current field finally gives itself up  2026-09-03

Thirteen minutes, three and a half miles, 21:45 to 21:59 UTC. Nothing exotic —
neighbourhood roads, peak 41.4 mph, one stretch held near 40 for the better part
of a minute. 171 logged rows, pack current from group-01 sensor 2 reaching
−66.5 A and regen coming back up to +20 A. The point of it was routine R1 in
`research/driving_capture_plan.md`: get group 05 sampled while the car is
actually pulling more than 32 A, because since 2026-09-02 the note in
`docs/SIGNALS.md` has said, in as many words, that we did not know what that
field does above the rail.

Now we do. **It wraps.** The capture plan named four possible shapes — linear
past 32 A, wraps, clamps, or a wrong slope — and the answer is the least
interesting and most reassuring of them: bytes 22–23 really are a signed 16-bit
count divided by 1024, they really are the same count group 01 reports, and
there is simply nothing wider behind them. Push the current past ±32 A and the
number folds back by 64 A, sign and all.

Getting there honestly took some care, because `lbc01` is period 0 and `lbc05`
is period 5: the two reads sit half a second to a second apart, and a car being
driven changes 20 A in that time. So every hypothesis was given the same
latitude — it could pick whichever of the three neighbouring sensor-2 reads
flattered it most — and then judged on the median error over the 21 fresh
group-05 samples taken with a neighbouring current above 32 A. Linear: 8.49 A.
Clamps at ±32: 8.49 A. Half scale: 11.88 A. Wraps: **2.50 A**. And 2.50 A is not
a residual, it is the floor of the measurement: in-band samples taken under the
same violent transients disagree by a median 1.80 A purely from the timing skew.

Clamping is the one that had to be killed properly rather than out-voted, and it
died cleanly. Of the samples whose neighbourhood exceeded 40 A, *none* sat at the
rail. Only 4 of 169 samples land in the 30–32 A bin at all, and the distribution
of magnitudes is smooth right up to it — there is no pile-up, which is the whole
signature of a clamp. The largest reading in the entire drive was 31.919 A. Best
of all, the field changes sign as it crosses over: with sensor 2 at −34.11 A,
group 05 read **+31.71 A**. A clamp gives −32. Linear gives −34. A half scale
gives −17. Only a wrap gives +30. Two more, caught while the current was moving
slowly: −47.50 A came back as +15.57 (folded: +16.50), and −41.13 A as +23.58
(folded: +22.87).

Below the rail the published scale is simply right, which is what the old note
suspected but could only demonstrate under 9 A. Restricted to the 25 samples
where sensor 2 moved less than 3 A across a three-row window, group 05 and
sensor 2 agree to a median 0.27 A, worst case 1.43 A.

That leaves the question the whole thing rests on: is `÷1024` right in absolute
terms, or do both fields share one wrong scale? Coulomb counting settles it.
Integrating sensor 2 over the full 964 seconds gives −2.586 Ah; the BMS's own
SOC, 69.39 % down to 57.74 % against 23.14 Ah of capacity, says −2.695 Ah. That
is 96 %, and the 4 % shortfall is in the direction and of the size you get from
trapezoid-integrating a spiky trace sampled every five seconds. It does not
leave room for a factor of 1.37 or 2. Group-01 sensor 2 at `÷1024` is now
checked to −66 A, not to −9.

Two things came out of the same data that are recorded and **not** fixed here.

The first is that the reader is not as unaffected as the old note claimed. It
was right that `apply_policy()` treats sensor 2 as canonical — that is the
correct choice and stands. But it also learns `s2_offset = group05 − sensor2`
every time a fresh group-05 sample arrives, and above the rail that difference
*is* the wrap error, carried forward until the next sample replaces it. Across
this drive the fused `current_a` strays from sensor 2 by a median 5.9 A, p90
23.6 A, worst 41.1 A while moving, and the `discharging and cur > 0 → 0.0`
clamp then flattens a good many driving rows to zero outright. The offset should
only be learned while group 05 is comfortably inside the band and the two reads
already agree. `web/reader.py` and `vehicles/leaf_ze0.py` were not this lane's
to touch; the note in SIGNALS says so plainly.

The second is a new question where there used to be a shrug. Group-01 sensor 1
reads a median **1.358×** sensor 2 under load — p10 1.294, p90 1.429, across 48
samples above 15 A — and it is sensor 2 that coulomb-counts correctly. Sensor 1
is not merely "coarse", as we have been saying since August; something about its
scale or its meaning is wrong. Its row in SIGNALS was left exactly as it was,
because guessing a replacement is how bad numbers get canonised. It is written
down as open.

---

The same drive was supposed to calibrate the simulator's motor and regen
constants, which have carried an ASSERTED label and a promissory note since they
were written. It calibrated one of them, and the more useful result is the list
of things it cannot calibrate and why.

**Eco coast regen, 4.0 kW: corroborated.** Rows in Eco with the brake released
and power flowing back gave a median +3.60 kW in the 10–20 mph band over nine
samples, peaking at +7.68 kW. This is the one term road grade cannot fake:
lift-off regen is a commanded torque, so a hill changes how fast the car slows,
not how many watts come back at a given speed. It stays 4.0 and it is no longer
a guess. It is *not* labelled MEASURED, because the accelerator position is not
on Car-CAN and never gets logged, so "brake released" is not proof the pedal was
fully lifted, and each row averages five or six seconds of a decelerating car.

**Road load: still ASSERTED, deliberately.** This is the important one. There is
no grade signal anywhere in the capture — no GPS, no altitude, no inclinometer,
and `0x1D5` torque and `0x260` power limits are not in `ITEMS` so they were never
polled. Fit `cruise_kw(v)` against (speed, power) pairs from this drive and you
do not learn the car's rolling resistance and drag; you learn the route's
topography, wearing their name. The contamination is not marginal. Across rows
where speed held within 5 mph over three samples, the 35–40 mph bin spans −15.07
to −3.72 kW at n = 12, and one perfectly "steady" 32 mph row was *regenerating*
at +0.72 kW — that is a hill, not a road-load curve. Averaging it out is not
available either: the speed profile's mirror correlation is −0.05, so this was a
loop, not an out-and-back over the same tarmac, and the grade contributions do
not cancel. Even the per-bin minimum draw, the honest lower bound on level-ground
load, comes out non-monotonic — −3.72 kW at 35–40 mph but −2.01 kW at 40–45 —
which is exactly what a descent does to a lower bound. All that can be said is
that the asserted curve is not contradicted: it sits inside the observed
envelope, toward its low-draw edge. A `cruise_kw` stamped MEASURED that really
meant "measured on one hilly three-mile loop" would be worse than the honest
assertion it replaced.

**The pedal term, the 80 kW peak, D coast regen and the brake term: all still
ASSERTED**, for plainer reasons. The accelerator is not logged, so the pedal term
has no input side to fit against at all; the largest traction draw the drive ever
saw was −23.8 kW at 26.6 mph, which bounds the peak from below and says nothing
else. The car spent almost the whole drive in Eco and yielded exactly one usable
D coast sample (+4.10 kW at 8.4 mph), and one sample calibrates nothing. The
brake pedal never went past 8.6 %, so nine-tenths of the range behind
`brake_pct/100 · 30 kW` is untouched; in the sliver that was exercised the model
over-predicts, but every one of those rows is a deceleration through the speed
fade, so they do not cleanly indict the constant either.

And nothing above 41.4 mph is calibrated by any of this. No highway road-load
curve gets extrapolated from a 41 mph drive.

What would settle the rest is now written into `simulator/model.py` and
`docs/SIMULATOR.md` next to the constants themselves: R1's steady-speed ladder
driven out-and-back over the same stretch so grade cancels, or R7's deliberate
hill climb and descent. One more thing was recorded and not folded in — stationary
in READY with the lights and blower off, this car drew a median 0.50 kW across 57
rows, more than three times the 150 W `base_ready` from the Lab Test thread. But
the DC-DC was recharging the 12 V battery throughout (13.6–14.0 V) and the draw
decayed from about 0.55 kW early to 0.37 kW late, so an unknown part of that is
the recharge rather than standing load. Splitting the two needs a long stationary
READY soak, not a drive. The load table was left alone and the observation sits
in a comment above it.

731 tests (one added: the Eco coast regen figure is now pinned to the band the
drive observed). Privacy sweep clean. `web/leaf_battery.db` was opened
read-only throughout, with `?immutable=1`, and never by a `Store`.

### Audible alerts — a beep when a value crosses the line  2026-09-07

Branch `feature/tile-alerts`. The dashboard could show a hundred values and
never say a word when one went wrong; the first ask was plain — "beep when the
Leaf's SOC drops below N %". Roadmap C4 imagined a server-side rule engine with
macOS notifications and phone push; this is its client-side half, and it is
built into the ⋯ menu every tile already has rather than a new panel.

**What a rule is.** One per value a tile shows: `{signal, min, max, when, tone,
repeat, enabled}`, stored in the tile's `opts.alerts`. That placement was the
load-bearing decision — `opts` is the only free-form field `_clean_tile` passes
through, so rules survive `PUT /api/tiles`, `web/tiles.json`, saved layouts and
the cockpit's `/api/sim/tiles` with no schema change, and a hidden tile (which
is not polled) simply has no rules evaluated. The server now runs
`_clean_alerts()` over them on every write: unknown signals, unknown keys and
rules with no threshold are dropped, numbers coerced.

**Which values a tile offers.** A signal tile has one. A built-in tile had no
notion of "what I display" — only `items` (what to poll), and `lbc01` alone
feeds SOC, pack voltage, current, power, SOH, capacity, 12 V and insulation.
So each `TILES` entry in `vehicles/leaf_ze0.py` now declares `signals`, the
registry keys it actually paints (verified against the element ids in the
template and the partials), and `validate_profile` checks every one exists.
A profile that declares none gets every non-text signal its items produce.
The lists are served as `tile_signals` on `/api/signals` — not decorated onto
`/api/tiles` GET, because the cockpit loads its layout from a different store
and would have been left alert-less.

**The sound.** No file, no library: `web/static/alerts.js` drives a Web Audio
`OscillatorNode` through a gain node with an 8 ms attack and 20 ms release so
it does not click — four patterns, `low` (two notes down, the default for
"below"), `high`, `chirp`, `triple`. Every browser boots the `AudioContext`
suspended until a user gesture; the module resumes it on the page's first
pointerdown/keydown, the menu says "click anywhere to enable sound" until it
has, and ▶ on a row doubles as that gesture. Safari's `webkitAudioContext`
costs one `||`.

**The engine** is pure and lives beside the tone: `evaluate(rules, record,
now, ctx)`. Fire on the transition into breach; nag on the rule's repeat
(once, 10 s … 5 min) — but only while the value is actually past the line,
which a first draft got wrong (it nagged from inside the re-arm band, so a
value that had climbed back to 20.5 with a floor of 20 still beeped); re-arm
only after the value comes back inside by 1 % of the signal's registry range
(1 % SOC, 0.05 V on the 12 V, 0.3 psi), clamped so two close thresholds keep
a gap; freeze — no fire, no re-arm — while `status` is not `ok`, the item's
`item_age` is missing or past `max(90 s, 3 × period)`, or the value is null.
Freezing rather than clearing matters: a breach cleared on "reconnecting"
would re-fire the moment data returned. The cockpit's record carries neither
`status` nor `item_age`, so their absence means "evaluate". Breach state is
memory only; a reload sounds a still-breached rule once. Tests drive all of
this from node the way `tiles.js` is tested.

**The menu.** An *Alerts* section under the colour controls: per value, a
tick, *below* / *above* (or *when on* / *off* for a lamp), tone, repeat, ▶.
Thresholds commit on `change`, not `input` — the existing range fields save
per keystroke, which for a threshold means typing "50" fires at "5". Setting
a threshold arms the row; clearing both drops the rule. Editing a rule resets
its engine state; hiding, removing or resetting a tile clears all. A breached
card gets a red outline; 🔔 in the header is a global mute kept in
localStorage so the cockpit shares it (the cockpit evaluates the same rules
through `TileStudio.update()` but has no bell).

Docs in the same commit: README (Tile Studio paragraph, file and API tables,
badge), ARCHITECTURE (menu, persistence, engine), ADDING_A_VEHICLE (`signals`
on `TILES`), the profile contract docstring, ROADMAP (status line; B6 and C4
annotated with what is now done and what is still open — the store-backed
multi-read rules, `/api/alerts`, push that reaches a phone), SIMULATOR
(shared files). Known limits, stated: demo mode does not persist rules, each
open tab beeps on its own, sound needs one click per page load.

751 tests (twelve added: the engine's crossing / repeat / hysteresis /
freeze / bool / dotted-key sequences from node, the source seams, the
`tile_signals` lists, and the rules' round trip through `/api/tiles`, saved
layouts and `/api/sim/tiles`). Not yet heard in a browser — the assistant
cannot open one; the owner's check is `--adapter sim`, an SOC rule at 40
lowered from the cockpit: one beep, red outline, the 30 s nag, silence at
40.5, clear at 41. Privacy sweep clean.

**Same day, after the owner's first browser test** (baseline works, the red
highlight and the row controls approved): two requirement changes. The
repeat is now a slider, every 1 s to every 60 s (default 10), in place of the
once / 10 s / 30 s / 60 s / 5 min list — `Alerts.REPEAT` in `alerts.js`,
`ALERT_REPEAT_*` in `reader.py` clamping what is stored, the engine unchanged
(a hand-written `repeat: 0` still means once). The slider's label follows it
live on `input`; the value commits on `change` like every other threshold
control. And the card now flashes on the tone's beat: every fire — the first
and each repeat — restarts a 0.7 s `.alert-flash` keyframe on the card, the
steady `.alerting` outline holding between flashes; mute silences the tone
and leaves the flash. Tests updated for the clamp, the default and the
seams; 751 tests, privacy sweep clean. Browser check again the owner's.

**Owner's browser check passed** (same day): the slider, the beat and the
flash behave as described; merged to `main` and pushed.

### The pack in three dimensions, and the dashboard replaying itself  2026-09-08

Branch `feature/pack3d-playback`, five commits. Three things the owner asked
for in one breath, because together they answer one question: which cell
pairs go weak *under load*, and where in the pack are they.

**The 3D pack tile.** The cell grid says which pair is low; the new tile
says where it sits — the 24-module block on edge under the rear seat, the
flat stacks either side of the floor — every measured pair its own body,
coloured by voltage, in a viewport you orbit, zoom and pan; hover reads a
pair out, click pins it. No CAD file: the geometry is data in the profile
(`PACK_MODULE`, `PACK_CASE`, `PACK_LAYOUT`, `PACK_SENSORS`), turned into 96
boxes by a pure `pack_layout.js` (node-tested) and drawn by `pack3d.js` with
three.js — one instanced mesh, one colour per instance, module outlines,
terminal studs, a translucent case, DOM labels tracked in 3D. three.js ships
as ES modules only, so it is vendored under `web/static/vendor/three/` (MIT,
credited in NOTICE) and the tile is the page's first module script, reached
through an import map and talking to the classic scripts via `window.Pack3D`.
Three colour scales from the ⋯ menu: deviation from the pack mean (the
default — under load every pair sags, this shows who sags more), the grid's
own absolute scale (`cellColor` moved into `tiles.js` so both agree), and
drop from the pair's own rest voltage. Tile Studio grew the three hooks a
self-drawing tile needs (`opts`, `enabled`, `menuExtra`) and a `tiles:applied`
event. The spike that preceded it had one bug worth recording: a select
sharing an id with the label layer, so the label renderer resized the select.
Label layers are classes now, and a test says so.

**Where each pair is — partly assumed.** The three sections and their
counts are published; a 2013 teardown gives the floor as "2-high packs of 4
and 4-high packs of 8" per side; one forum post with a 2013+ diagram gives
the series order rear → driver → passenger. Nobody has published the order
inside a section, which floor stacks are 2-high front-to-back, or confirmed
any of it on a 2011–12 car. Every `PACK_LAYOUT` row carries a `verify` note,
the tile marks such pairs in its readout, `docs/SIGNALS.md` ("Cell order in
the pack") and `docs/PACK3D.md` say exactly which rows are which, and
`validate_profile` checks the table covers 0–95 once. The service manual's
EVB-67 figure settles it; that is a one-figure lookup for the owner.

**Playback.** A Playback button in the header stops the live poll, raises a
PLAYBACK badge and opens a timeline above the tiles: sessions (gaps of ten
minutes in the data — the `sessions` table has no epoch and an open row for
every crash), a strip of SOC and pack current with a tick where cells were
read, a playhead, ⟨ frame / ▶ / frame ⟩, ±10 s and ±1 min, ½× to 60×, space
and the arrows; click seeks, drag zooms into a stretch and re-fetches it at
full resolution. The page keeps one paint path: live `poll()` fans two
fetches out to five sinks, playback's `renderFrame(k)` feeds the same five
from `records[k]` and `hist[0..k]`, so no tile has a playback branch — the
3D pack simply resets its rest voltages at the first frame of a window.
`Store.frames()` rebuilds the `/api/status` shape from rows (extra bag,
columns, temperature lists and °F twins from the °C columns, cells joined
in one query), thins to the last *real* row per bucket — never an average,
a frame is a state — and marks each `playback: true`; the status dot says
"Recorded", the adapter badge "recorded", and alerts stay silent unless the
timeline's box is ticked, the engine forgetting its hysteresis on every mode
switch. The clock (`playback.js`) lives in the browser on purpose: the reader
may be live on the car while two browsers scrub two different afternoons.
`?playback=1&from=&to=` links a moment. Lost on the way back and named as
such: `adapter_port`, the readings counter, the raw balancing list.

**The cell log.** Cells were stored once per 20 s; an acceleration lasts
three to eight seconds. `opts.celllog` on the cell grid or the 3D pack is
the first tile option that changes *how often* rather than *whether*:
`period_overrides()` puts `lbc02` in the fast lane, `Reader.period()`
consults it before the profile, and the main loop stores a row for every
fresh read — a `cells_seq` counter marks freshness, which also stopped the
sticky cache being written as four identical cell sets between real reads.
Same read-only request, more often; a CELL LOG badge because it costs
~1.3 s a cycle on BLE and 96 rows a cycle.

**Also:** the privacy sweep's VIN rule tripped on four 17-digit float
literals inside three.js (0.15915494309189535 is 1/2π). A real VIN carries
letters, so the rule now excludes all-digit runs rather than exempting the
vendor directory; vendored code stays scanned.

Docs in the same commits: README (status, tiles table, API table, repository
layout, quick start, licence), ARCHITECTURE (tiles, page modes, data model,
scheduler, vendoring), SIGNALS, ROADMAP, CLAUDE §5/§6, NOTICE, REPLAY (replay
is not playback), and two new pages, `docs/PACK3D.md` and `docs/PLAYBACK.md`.

790 tests (thirty-nine added: the vendored files and their licence, the
profile's coverage of 0–95, the geometry and scales from node, the page
wiring, frames' round-trip fidelity against every key the tiles read,
last-of-bucket thinning with cells, gap-derived sessions, the routes and
the demo path list, the transport from node, the timeline outside the grid,
the alerts gate, the period override on a scripted clock, and every fresh
cell read stored under the log with unchanged cells stored once without it).
Privacy sweep clean. Not yet seen in a browser — the assistant cannot open
one; the owner's checks are `--adapter sim` with `fault.cell_degraded` (one
red pair on the deviation scale), the real database in Playback (pick an
afternoon, scrub, every tile follows), and the cell log on USB in the parked
car (the `lbc02` age staying under a second, rows growing per cycle).

**Same day, after the owner's first browser test** (the pane is there, the
model is not, in live and playback alike): the page declares the layout with
a top-level `const PACK`, which every script sees by name but which is *not*
a `window` property — and the module checked `window.PACK`, found nothing,
and never built. The spike never met this because it was one script. The
page now publishes `window.PACK = PACK` and the module accepts either form;
a test pins both. Also asked for and added: a marker on the strip at the
playhead — an accent line, a triangle on each edge, the frame's clock time
in a pill that flips left near the right edge.

**Owner's second browser round** (same day; the render works): the tile now
defaults to the grid's own colour scale and is lit so a top face shows its
plain colour, so a pair reads the same in both panels side by side; the four
sensor balls are coloured on the pack's own range (hottest red) and labelled
in °F and °C; clicking a pair opens a side pane with both pairs of its module,
larger, with rank and balancing; the pointer help is a `?` rather than text
that looks clickable; `⤢` doubles the tile's height through a real gridstack
resize (`TileStudio.tile()` / `size()` are new); and the lowest pair breathes
toward white (an option, on by default).

**Cell positions, second attempt** (same day, at the owner's request): a
primary source turned up. RegGuheert on mynissanleaf (2013-04-29, "Which
cell loses capacity fastest?") quotes the ZE0 service manual, page EVB-20:
MD1–MD24 in the rear stack with MD1 at the far passenger side and MD24 at
the far driver side; MD25–28 under the rear driver's footwell; MD29–36
under the front driver's seat; MD37–44 under the front passenger's seat;
MD45–48 under the rear passenger's footwell; module n holds cells 2n−1 and
2n. Every section, count, direction and index range the tile had assumed
matches it, so the section-level layout is now marked verified in the
profile, SIGNALS and PACK3D; what stays assumed is only the order of the two
stacks inside a footwell or seat group and bottom → top within a stack, and
the tile's readout now says *(stack order assumed)* rather than *(position
unverified)*. Cells 53 and 55 — the weakest in February and August — are
MD27/MD28, under the rear driver's footwell beside the LBC.

**Owner's third round** (same day): the side pane paints each voltage in
the pair's own colour, the grid's; a pinned module gets a glowing box, a
bobbing pin and a label, since an edge outline was not enough to find it; the
highest pair now breathes toward blue as the lowest does toward white; the
playback timeline floats, locked to the bottom of the window by default (or
the top, or in the page — remembered), so the transport is in reach from any
tile; auto-rotate is a `⟳` button on the pane itself, no longer in the menu
(`TileStudio.setOpt()` is new for that); and the bodies have the real module's
rounded edges — `RoundedBoxGeometry` vendored, and because a rounded box cannot
be scaled per instance without distorting its corners, the 96 bodies are now one
instanced mesh per body size with a slot table from pair to (mesh, instance).

**Numbering** (same day, the owner's call): the dashboard now counts cell
pairs 1–96 on screen, as the service manual and every other tool do — the
cell grid's boxes and tooltips, the 3D tile's labels, readout, pin label and
side pane. The record's `cells` list, `cell_min_idx` and the layout table's
`first` stay 0-based array indices underneath. Entries above this one use the
old 0-based count: the "cell 53 / 55" of February and August are pairs 54 and
56 today, MD27 and MD28.

**Same day:** the 3D tile's ⋯ menu gained *Flash all below / above* — two
voltages; every pair under the first breathes white, every pair over the
second breathes blue, the counts shown on the readout line. The flash
machinery is now one set of (pair, base colour, target) rather than two fixed
slots, and the lowest / highest flashes are just its first two entries.

**Fourth round** (same day): the temperature sensors are selectable — hover
reads one out, click pins it with the same marker a module gets and opens a
pane with its reading large and in its colour, °C, the difference from the
pack mean, its rank among the four, where it sits, and all four listed. The
module pane gained a section under the two pairs: the module's own spread and
average, large, the average ranked among the 48 and the spread ranked
widest-first. Cleanup found on the way: the tile drew the sensors from the
temperature item without declaring it, so with the temperature tile off the
balls would have gone blank — `lbc04` is now in its items and the sensor
signals in its alert list; the ARCHITECTURE hook list and the CLAUDE.md §6
row were behind and are current.

**Timeline, fifth round** (same day): the strip's legend sat under the
playhead's time pill at frame 0 — it lives bottom-right now; the playhead
is grabbable (press within a few pixels of it and drag to scrub, anywhere
else drags the zoom brush, the cursor says which). And **flags**: `⚑ Flag`
in the header (or `f`) drops a bookmark on *now* while live — a passenger
marking "pull from the light" as it happens — or on the playhead in
playback; yellow triangles on the strip, a `⚑ jump to…` list, `✕` to remove;
`web/bookmarks.json` (gitignored, stamped with the vehicle) behind
`GET/PUT/DELETE /api/bookmarks`. **Auto-detected pulls** (`/api/bookmarks/auto`,
runs of pack current below −40 A, merged within 8 s) show as hollow orange
triangles. The owner's last twenty minutes, read from the database: 102 rows
at ~4.2 s, every one with cells (the cell log was armed), six pulls, each a
single row — peaks of −271, −267, −264 A (−89 kW at 54 mph). The peak lands
in the data with that row's cells; the rise and recovery do not, at this
cadence — USB for drive days.

**The pack abstracted** (same day): the 3D tile no longer knows it is
drawing a Leaf. A stack says how its modules map to measured values —
`split` values per module (the Leaf's 2), or `group` modules per value (a
Prius NiMH's 2) — and `PACK_MODES` says which record list colours the
bodies, in what unit, with which scales, `invert` for temperatures; bodies
and values are separate (`body.v`), so a grouped pack colours several bodies
alike and labels the value once. The validator checks coverage through
`split`/`group` and the modes' shape. `docs/PACK3D_GUIDE.md` is the contract,
the method that produced the Leaf's table with an agent in an afternoon —
collect the published shape, find the numbering in the service manual, write
the table with `verify` notes, spike, wire, verify on the car — the mapping
modes, a Prius sketch and a checklist. Node tests cover a 28-module grouped
row and a four-slice stack on the same geometry code.

**Chimes off the beat** (same evening, the owner's report: a 1 s repeat
plays for a few seconds, then skips one and drifts). Two causes, both on the
page. Alert rules were evaluated only when a poll landed, and a 1 s repeat
judged on a poll that arrives every 1.0–1.3 s skips a beat whenever the
interval falls short; Tile Studio now re-runs the pure engine on the last
record four times a second, so a repeat lands within a quarter second of its
time. And the 3D tile drew sixty frames a second whether or not anything
moved — it renders on demand now (a new record, a hover, a resize, the
camera moving, auto-rotate), the flashes and the pinned marker tick at
20 fps, and nothing is drawn while the tile is scrolled out of view or the
tab is hidden, which hands the main thread back to the page's timers.

**Smooth again** (same evening): the 20 fps tick made the breathing lowest
pair twitch. While a value breathes or a marker is pinned the tile now
animates at the display's full rate, and the saving comes from the right
place: the DOM labels — the expensive part of a frame — are redrawn only
when the camera or the data moved. Idle, scrolled away or in a hidden tab,
nothing is drawn.

**Studs on the short end** (same evening, the owner's eye): the gen1 module
is one design throughout — both studs and the sense tap on one 223 mm end —
and the rear block had them drawn along its long top edge. Standing on edge
a module is 223 mm tall, so its short end faces forward; the studs now point
down the bus-bar channel toward the floor stacks. The flat stacks already
had theirs on a short end, inboard. Docs caught up: README's status carries
the flags, ARCHITECTURE's page-modes paragraph the docked timeline and the
pulls, PACK3D the stud placement, and ROADMAP a section distilling the
extended-CAN research (throttle, brake and 12 V, regen torque, motor power,
steering, climate power — all on Car-CAN, all walkable parked).

**Owner's browser check passed** (same evening — the pack, the sensors, the
module pane, playback, flags, the docked timeline): `feature/pack3d-playback`
fast-forwarded to `main` and pushed, nineteen commits, 800 tests, privacy
sweep clean. Still to see in the car: the cell log's cadence over USB, and
flags dropped during a real pull.

### The cycle on USB, and the flow-control pace  2026-09-09

Branch `main` (a transport tuning, verified on the car before it landed).

**What the cycle was.** USB adapter, cell log armed, eighteen items: a
median 3.0 s cycle (p10 2.3, p90 3.6). The last cycle's timing said where
it went — the cell read (group 02) 1.18 s, five passive captures at
0.22–0.24 s each 1.16 s, group 01 0.35 s, HVAC group 10 0.30 s. Neither big
share was the adapter: a USB round-trip is 5–10 ms.

**The cell read was paced by our own flow control.** `ATFCSD 30 00 20`
asks the ECU to leave 32 ms between the frames of a multi-frame answer;
29 frames at 32 ms is 0.93 s of the 1.18. A probe with the reader paused
and the pack charging (`research/probe_stmin.py`, read-only: 0x21 reads,
five each, at 38400 and again at 115200):

| STmin | group 02 (29 frames) | group 01 (6 frames) | errors |
|---|---|---|---|
| 32 ms | 1.18 s | 0.26 s | 0 / 5 |
| 16 ms | 0.65 s | 0.18 s | 0 / 5 |
| 5 ms | 0.37 s | 0.14 s | 0 / 5 |
| 0 ms | 0.37 s | 0.14 s | 0 / 5 |

Every read returned the same 29 frames and 464 hex characters. The faster
wire changed nothing worth having (0.36 → 0.35 s at 0 ms), so the remaining
floor is the ECU's own pacing, not ours. The separation time is now a
per-transport attribute: `STMIN` 05 on the serial link, 20 on BLE — whose
20-byte notification chunks are why 32 ms was chosen in February — and on
the replay and simulator transports, whose fixtures pin it. Tests cover the
attribute and the command sent.

**The passive captures wait their full dwell.** Each `ATMA` runs for its
whole 0.2 s even when the frame it wants arrived in the first 20 ms; five
of them a cycle is 1.16 s for perhaps 0.1 s of useful listening. Returning
on the first clean frame is the next change, with its own probe.

### Native CAN, MQTT ingestion, and a public stream  2026-09-09

Branch `feature/can-transport` (on top of `feature/usb-stmin`, which carries
the STmin commit recovered from the cut-off session). Two sub-agent lanes,
each with a progress log in `research/agent-logs/`, integrated and reviewed
by the orchestrator. **Nothing in this entry has run on a board, a Pi, a
broker or the car**: the CANable arrived today and is still in its bag, and
every doc written today says so on its first screen.

**Recovery first.** The 09-09 session had been cut off with the STmin tuning
uncommitted on `main` and its research sub-agent killed after writing the
CANable memo but before logging it. The tuning moved to `feature/usb-stmin`
and was committed with its doc sync (AGENTS had said 730 tests since 09-03;
802 was the truth). The rule that every sub-agent keeps a log is now also in
the owner's global instructions, and CLAUDE.md §3b cites this second case.

**The design that shaped both lanes.** A native CAN controller speaks
frames, not ELM327 text, so `cantransport.py` is an *ELM-speaking façade*
(`CanFacade`) over a *frame source*: it keeps exactly the adapter state an
ELM327 keeps (`ATSH`/`ATCRA`/`ATCAF`/`ATFCSH`/`ATFCSD`), answers `ATMA` from
a table of recently received frames with the caller's window, and turns a
hex request into one ISO-TP exchange while capturing the raw response frames
off the stream — so `parse_isotp()` sees the same lines an ELM capture has,
and the reader, profiles, decoders and fixtures are untouched. MQTT is the
same façade over a second source; the two sprints share one core.

**Lane can-facade** (48 tests): `LocalSource` on python-can — slcan (the
stock firmware's silent switch is `M1` before `O`; python-can's `L` is not
implemented by it), gs_usb (listen-only re-applied through the `gs_usb`
package and *read back*; a firmware that drops the bit means the bus is not
opened at all), socketcan (`ip -details link` checked for LISTEN-ONLY),
virtual for tests; firmware identified from USB VID:PID, the DFU bootloader
state recognised and refused. Read-only enforced at the transport: a service
byte outside {0x21, 0x01, 0x03, 0x07} never reaches a bus. **EV-CAN is
listen-only always, with no override** — the owner's call to confirm; it
means the LBC's reads keep going through Car-CAN and the VCM bridge. A
silent ECU costs 1 s, not the item's 10 s. `STMIN = "00"`, `SPEED = 0.05`
(modelled), `PASSIVE_INSTANT = True` and `Reader.estimate()` drops the
passive items' dwell. Requests and flow control padded to 8 bytes with 00,
as the ELM327 does by default (`ATV0`). Fixture round trip verified: a fake
ECU on the virtual bus answers 2101/2102/2104/2110 from the recorded frames
honouring our flow control, and `decode_reading()` is equal on both paths.
`docs/CAN_TRANSPORT.md` carries the arrival checklist and the wiring; the
firmware-flashing guidance is held ("pending the owner's bench check") at
the owner's request.

**Lane mqtt-source** (87 tests): the owner chose **JSON payloads** and asked
that the protocol be documented well enough for a third party to build a
simple gauge. `docs/MQTT.md` is that spec — topic tree, a JSON Schema per
payload (frame, batch, uds request, uds ack, status, state, signal), field
semantics, the read-only rule on both ends, versioning, `mosquitto_sub`
examples for every topic, a Python gauge and a browser gauge. Frames go to
`<prefix>/<bus>/rx/<ID>` as `{"t","id","d"}` — `id` + space + `d` *is* the
ELM line; requests to `tx/uds` with the bridge doing ISO-TP flow control
locally (a WAN round trip must never be inside the FC timing) and the
response frames arriving on the ordinary `rx/` topic; a retained `status`
with a Last Will. Because a gauge wants a number, not a frame, **any reader
with `mqtt` configured publishes its decoded record to `<prefix>/state` and
every scalar to `<prefix>/signal/<key>`, retained**; one call in
`Reader.publish()`, a no-op without a broker, never raises. The Pi bridge
(`bridge/hakake_bridge.py`) reads SocketCAN, applies `ids` as a kernel
filter, hand-builds the frame JSON (3.5× `json.dumps` on the laptop), batches
on request, drops-and-counts on a bounded queue, and refuses every non-read
itself — it is the process that can transmit, so it trusts nobody. Sized for
a Pi Zero 2 W as *expectations*: per-id filtered ≈ 200–600 fps comfortable,
whole-bus needs batch mode. `record_session.py --from-mqtt` turns a saved
stream into a replay fixture — the first capture path with no laptop in the
car. Orchestrator fixes on review: the bridge padded requests like the façade
and the ELM (it had sent them unpadded), and the spec now says `state`
carries the adapter's identifiers, one more reason the broker stays on the LAN.

**Written into the plan and the roadmap today, not built:** a timing
architecture (acquisition time per item stored, both clocks kept with the
offset published, peak-preserving min/max per row for the few signals where
a peak matters, high-rate samples on a flag, `ts_source` on every row);
capture as a pillar (one core, several front ends, none needing the laptop);
and several adapters at once with provenance — items carry a `bus`, one
transport per bus polled concurrently, every source writes its own key, the
registry names the canonical key and its sources, a resolver picks (user pin,
then verified over tentative, then freshness, then rate) and stamps
`<key>_src`, and two fresh sources disagreeing beyond a tolerance is an
event, not a silent choice. Python is enough for all of it at these rates;
if a measurement ever pins the bridge on a small board, a Rust bridge
speaking the same topics changes nothing on the laptop.

937 tests, privacy sweep clean. Not merged, not pushed.

### Stored values are the reported values  2026-09-09

Branch `bug/raw-current-stored` (on `feature/can-transport`). The owner
noticed the power tile's "5-sample EMA" label, could not change it, and
asked the real question: is anything smoothed before it is stored? The
rule, in the owner's words: smoothing for readability is cosmetic, on the
dashboard only; the database keeps the most raw and accurate values the
car reported, always.

**The audit.** The read path was clean — `/api/history` averages per time
bucket on read for the charts, playback keeps the last real row per bucket
and never averages, the page draws what the API returns, and the tile's
EMA lives entirely in the page. One stored column was not raw: the Leaf's
`apply_policy` rewrote `current_a` before the row was written (the
group-05 / sensor-2 fusion with a learned offset, the zero calibration, the
positive-while-discharging clamp) and derived `power_kw` from it; the raw
readings survived only in the row's `extra` JSON.

**The change.** `apply_policy` now derives instead of overwriting:
`current_a` and `power_kw` stay exactly what `decode()` produced (sensor 2
when group 01 was read, else group 05) and that is what the `readings`
columns hold; the fusion, calibration and clamp land in `current_adj_a` /
`power_adj_kw`, with `current_adj_src` naming the steps applied
(`s2+g05_offset+zero_cal+clamp`). The power tile shows the adjusted value,
so idle still reads −0.9 A rather than the sensor's dead zone and "zero
now" still visibly works; its EMA is now a setting — off / 3 / 5 / 10
samples — in the tile's ⋯ menu and on the label itself, persisted in the
tile's opts, and labelled display-only. The two derived keys are registry
signals (61 now) and alertable from the tile. Old rows keep their derived
`current_a`; the page falls back to it when no adjusted key is present.

**The rule is enforced, not just written.** `tests/test_policy_raw.py`
runs the session fixture through every profile that has a policy, three
cycles with a calibration offset set, and fails if any key `decode()`
produced changed; the contract docstring, ADDING_A_VEHICLE, ARCHITECTURE
and SIGNALS say the same. Still to do in the provenance lane: the raw
source currents as first-class columns.

Not checked in a browser: the ⋯ menu select and the clickable label. The
tests cover the policy and the API; the page needs the owner's eye.

**Owner's browser check passed** (same evening — the power tile's smoothing
is clickable, the 3 / 5 / 10-sample settings show): `feature/usb-stmin`,
`feature/can-transport` and `bug/raw-current-stored` fast-forwarded to `main`
and pushed, four commits, 940 tests, privacy sweep clean. Still to see in the
car: a recorded session with raw current stored; the CANable's arrival
checklist and phase (a) on the board; the MQTT bridge on a Pi.

### MQTT: plain, on the LAN; Tailscale or a VPN beyond it  2026-09-09

Branch `bug/mqtt-no-auth`. The owner's call: the MQTT configuration had
gone too far. The broker runs plain MQTT — no username, no password, no
TLS — and lives on the car's LAN; to use it from anywhere else the Pi and
the laptop join a Tailscale tailnet or a private VPN, which authenticates
and encrypts the whole path with nothing to configure in Ha-Kake. The
`username` / `password` / `tls` keys are gone from the reader's `mqtt`
block, the bridge's config and both example files; the clients are built
plain; the Mosquitto snippet in `bridge/README.md` is `allow_anonymous
true` on a LAN listener; SECURITY.md, `docs/MQTT.md` and the gauge examples
say the same. A test pins it: auth keys in config are ignored, never
applied. The read-only whitelist is unchanged — it never was the thing
protecting the broker; the private network is.

### Timing, several adapters, provenance  2026-09-09

Branch `arch/timing-multibus`, two commits, plan §8 and §10. First the
timing architecture: every item is stamped with when its value was actually
acquired (`item_ts` / `item_ts_epoch`, a UDS answer as it returned, a passive
item by its newest frame's arrival), the row keeps the epoch and playback
rebuilds the ISO, and the per-tile "read at" badge measures from the frame's
own moment in playback. The native CAN façade keeps the source's timestamp
beside the laptop's receive time and the reader publishes the median offset
(`clock_offset_s`, stored on the session, never applied); `ts_source`
(laptop / driver / bridge) is an additive column on every row. Signals a
profile marks `peak` keep their min / max / time-of-each between stored
rows in the row's `extra`, so a 5 s row holds the true peak of a pull and
the timeline's auto-flags find it. `tools/bench_transport.py --timing` runs
the reader's own cycle over any adapter and reports jitter, per-item timing
and clock drift; `docs/TIMING.md` is the authority.

Then the buses: items carry a `bus`, a profile may declare `BUSES`, and an
`adapters` list in `config.local.json` (or `HAKAKE_ADAPTERS`) opens one
transport per bus — `--adapter X` stays the shorthand and, deliberately,
carries no overrides, so the file's `can_bus` still names the bus. The
cycle's items are grouped by bus and polled under `asyncio.gather`, each
bus with its own target state, timing and tri-state liveness; only the
primary bus's silence means asleep; a secondary that raises is dropped and
reconnected by its own task while the rest keep storing. The record lists
every adapter (the old `adapter_*` keys stay, from the primary) and the
header shows a chip per further bus. Provenance: a SIGNALS entry declares
`sources` and a `tolerance`, the reader's generic resolver picks — pin,
verified over tentative, fresher, faster — and stamps `<key>_src`; it never
writes a key a decoder has produced (then `<key>_resolved`), and two fresh
sources apart beyond tolerance set `<key>_disagree` and log a
`source_disagree` event on the way in and out. The Leaf's pack current from
groups 01 and 05 is the worked example — expect disagreements above ±32 A,
where group 05 wraps, which is the point. 972 tests, fake transports only;
nothing has run with two adapters or on a native board yet.
### A simulated CAN bus, and an acceleration to compare against  2026-09-09

Branch `feature/sim-can-rig`. The CANable had not arrived, and the question
the owner wanted answered before it did was whether the app can take the
data rate a native adapter delivers. So the simulator grew a bus.

**The rig.** `simulator/canbus.py` runs simulated ECUs on python-can's
`virtual` channels, driven by the same model the cockpit drives: every
Car-CAN id the Leaf profile reads, encoded through the existing encoder at
the periods the frame-rate survey gives, plus filler ids so the bus carries
its true load (a `bus_load` knob scales it, and it is in the schema like
every other knob). The battery and climate controllers answer the reader's
`0x21` reads over ISO-TP honouring the requester's flow control; anything
that is not a read gets a negative response, never an exception. A second
channel carries EV-CAN — `1DB`, `1DA`, `1D4`, `55B`, `5BC`, `11A`, `1DC` —
whose byte layouts come from the published DBC and OVMS sources, are cited
in the code and are labelled ASSERTED, because no car here has confirmed
them. `--adapter sim --sim-can` runs the whole stack behind the CAN façade.

**What it measured** (`tools/bench_canrate.py`, on the laptop, virtual bus —
no wire, no bit errors, no real ECU pacing, no USB):

| Bus load | Broadcast fps | Scheduler cycle med / p90 | CPU total / ECU / reader | Missed reads |
|---|---|---|---|---|
| 0.1 | 525 | 3.9 / 4.6 ms | 17.3 / 9.7 / 7.7 % | 0 |
| 0.5 | 1,044 | 3.4 / 4.6 ms | 21.7 / 11.8 / 9.8 % | 0 |
| 1.0 | 1,693 | 3.9 / 4.3 ms | 30.6 / 15.7 / 14.9 % | 0 |

So the answer is yes, with room: at a full Car-CAN load the reader's own
share is about 15 % of one core, and the passive items are answered from
the frame table with no dwell at all. Every passive item's own capture
window caught its id at every load; a 0.2 s window misses the 500 ms ids
only by phase, which is why they ask for 0.8 s.

**The pull.** `simulator/scenarios/pull.json` is a standing start to about
50 mph in eight seconds reaching −250 to −270 A, a hold, a regen coast and
a brake to stop — shaped to the owner's own reading of −271 A and −89 kW at
54 mph on 2026-09-08. `hakake_sim.py --pull` leaves three artefacts: a
session in the throwaway database with the cell log armed, playable on the
timeline with the pull auto-detected; the raw frame stream of both channels
as JSON lines; and a thinned replay fixture,
`tests/fixtures/session_leaf_ze0_pull_sim.json`, marked synthetic and
carrying a note that the EV-CAN bytes are assumed. That fixture is the
*expected* half of a comparison: `tools/compare_sessions.py` prints peak
current and when it arrived, time to peak, the lowest pair, pack sag, SOC
drop and the per-id periods actually observed, so when the board is on the
car the difference between what we predicted and what the Leaf does is one
command.

A simulator checks consistency, not truth. Nothing here is evidence about a
car, and the EV-CAN half is not even evidence about the bus — it is a
statement of what we expect to find, written down early so it can be proved
wrong.

### A colour scale that holds still  2026-09-10

Branch `feature/fixed-colour-scale`. The owner, watching a session
play back, saw the cell colours re-scale as the pack sagged and said he had
expected a static map from voltage to colour.

He was right about the cause. Both cell tiles colour through `Tiles.cellColor`,
which normalises a value between a low and a high the caller supplies, and both
callers supply the *frame's own* lowest and highest pair. So red has always
meant "the lowest pair in this frame", not a voltage, and in playback both ends
of the scale slide down together under load. The two other scales the 3D tile
offers avoid this in their span but not their reference: deviation is from the
frame's mean, drop is from each pair's own first reading.

So there is now a fourth scale, `fixed`, in the pure layout module beside the
other three: it maps a value into bounds that never move and clamps outside
them. The bounds come from the profile — `PACK_MODES` gains
`fixed: [3000, 4200]`, the same numbers the signal registry already declares
for the lowest and highest pair — and either tile can override them with two
numbers in its ⋯ menu, blank meaning the profile's. The validator refuses a
mode that offers the scale without a sane pair, because a reversed range would
paint every pair one colour. The cell grid gained the same two menu rows and
resolves its range through the same function the 3D tile uses, so the two
panels can be put on one scale; each tile keeps its own setting. Both legends
now name the active range instead of saying Low and High.

**The default did not change**, at the owner's request: the frame-relative
scale is still what a tile gets. The reason to keep it is the reason the fixed
scale costs something — on fixed bounds a healthy resting pack occupies a
narrow slice near the top of the ramp and every pair looks alike, while the
frame-relative scale always spends the whole ramp on the spread that exists.
One is for reading a voltage, the other for finding a weak pair.

Display only, and it says so in the menu: no decoder, no record and no stored
row changes, which is the same rule the power tile's smoothing follows.
Node-tested for the clamp, the override, the fallback and the legend, plus the
proof that a value keeps its colour across frames where the frame-relative
scale moves. Not yet seen in the owner's browser.

### The docked timeline matches the page's column  2026-09-10

Branch `bug/timeline-width`. The owner, on a wide display: the playback
timeline spans the whole browser window while every card beneath it stops at
the page's column, so the two do not line up.

It floats — `position: fixed` inset 12 px from the left and right edges — and
nothing capped it, while `.dash` caps at 1400 px and pads by 20 px. On a
1440-pixel window they are nearly the same and the mismatch hides; on anything
wider the timeline runs away from the page. The page's column is now one
custom property in `hakake.css` (`--app-max`, `--app-pad`, and `--app-col` for
the card width that follows from them); `.dash` reads it instead of carrying
its own literal, and the docked timeline takes `max-width: var(--app-col)`
with auto margins, which centres it between the same insets. Narrow windows
are unchanged, because there the insets still win.

The single property is also the seam the wide-display mode will move, when it
is built: that is still only a roadmap entry, never implemented.

Not yet seen in the owner's browser.

### A truth audit: the docs against the code  2026-09-10

Branch `docs/truth-sync`. After the two lanes merged, the owner asked whether
the documentation still describes the code. Four reviewers went over it in
parallel — the reader and store, the transports and security, the simulator
and the page, and the front-door documents — with the rule that a claim is
checked by running something, not by reading around it. Their logs are in
`research/agent-logs/audit-*-20260910.md`.

**The answer is mostly yes, with two findings that mattered.**

*The docs promised a speed the code deliberately declines.* `ARCHITECTURE.md`
and the README said the USB transport negotiates up to 115200. It does not:
`serial_target_baud()` returns 38400 unless `HAKAKE_SERIAL_BAUD` asks
otherwise, and its own docstring says why — `ATBRD` leaves persistent state on
the adapter, so a killed run leaves the chip fast while the next one opens
slow and hears silence, which cost a session on 2026-09-03. The large win came
from the blocking read, which touches nothing. Both documents now say the rate
is opt-in and mark the round-trip figures as the faster wire's.

*A documented invariant was not enforced.* `ADDING_A_VEHICLE.md` lists the
columns a profile may not redefine and includes `ts_source`; the validator's
reserved tuple never did, so a profile declaring that column would have had it
emitted twice and failed at `CREATE TABLE`. Fixed in `validate_profile`, with
a test that now walks every reserved name.

**Everything else was drift, corrected in place.** A row's `ts` is the cycle's
*start* and can precede every value in it — `TIMING.md` had that backwards.
The scheduler paragraph knew two transports and no `PASSIVE_INSTANT`
exception. The cycle model counted eleven passive captures where the profile
has ten. `ADDING_SIGNALS.md` still said EV-CAN was unreachable, months after
items gained a `bus`. The current policy publishes six more keys than
`SIGNALS.md` named, and the HVAC group captured raw is 00, not 01. The MQTT
spec omitted two rules its own filter applies and overstated its environment
variables; the `req` pattern is a producer rule the bridge does not fully
enforce. The pack guide's temperature example was missing a required key and
would have failed validation, and it did not warn that an omitted mode key
inherits the Leaf's millivolt numbers. The playback document described a demo
frame set that does not exist, credited the `f` key with working live when it
only works in playback, and said auto-pulls are flagged at their true peak
when the endpoint computes it and the page still draws the run's first row.
Two things the code had wrong in user-visible text: the pack pane said "all
four sensors" whatever the pack held, now counted; and the legend bar does not
honour a mode's `invert`, which is now stated rather than implied.

Counts checked rather than assumed: 1023 collected, 1022 passing, the skip
being the policy test on a profile with no policy. Two claims that said
"tests" now say "passing". The suite's stated runtime moved from "~2 min" to
2.5-4 min, which is also why a push feels slow: the hook runs the sweep and
the whole suite before it uploads.

Three findings were left as code questions rather than doc edits, and are on
the roadmap: the legend not inverting, the `f` key not working live, and the
auto-pull flag not using the peak time the store already returns.

### Five pulls from a real drive, for the 3D pack  2026-09-10

Branch `feature/pulls-scenario`. The owner wanted the voltage-drop
visualisation to have something to show: a simulation of an acceleration
repeated several times, built from real drive data rather than invented.

**The drive.** 2026-09-09, 14:05–14:08 PDT, 207 rows, every one of them
carrying a cell set because the cell log was armed. Four pulls in three
minutes, peaking at −96, −228, −157 and −56 A, with the car dropping from
51.5 % to 46.3 % SOC across the stretch. What that data gives, measured:

- at rest, 377.9 V drawing 5.9 A, lowest pair 3918 mV, spread 35 mV;
- a regression of pack voltage on current over all 207 rows: open-circuit
  376.9 V, series resistance 0.186 Ω (single rows scatter from 0.09 to 0.38,
  which is the acquisition lag between group 01 and group 02 showing up
  exactly where yesterday's follow-up said it would);
- and the thing that matters most for the tile: **the spread tracks the load**,
  `spread_mV = 32 + 1.06 × |I|`, from 35 mV parked to 274 mV at −228 A.

Under the deepest sag the mean pair fell 581 mV; the worst fell 681 mV
(pairs 66, 70, 72, 76 — the modules under the front seats) and the best 501 mV
(pairs 1, 48, 53–56, 92). Worth noting against February and August, when pairs
54 and 56 were the *weakest* on the bench: under load here they are among the
strongest, which is a different question from resting voltage and worth
watching.

**The scenario.** `simulator/scenarios/pulls.json`: five pulls, twelve seconds
of rest between them, about two minutes end to end. Each pull's pedal
percentage was solved by bisection so the model lands on one of the currents
the car actually reached, and each timeline step also sets `cell_spread_mv`
from the fitted line. The result tracks the drive: −96/−228/−157/−266/−56 A
against the car's −96/−228/−157/−265/−56, with cell floors of
3666/3334/3508/3230/3747 mV against the car's 3683/3345/3552/–/3765.

**What it does not do, and the doc says so.** The widening spread is stepped by
the timeline, not produced by physics, because the model gives every pair one
resistance and a fixed spread. So the magnitude of the sag is the car's but the
*pattern* is the seed's — the reddest pairs are not the pack's real weak
modules. Giving each pair its own resistance is now a roadmap item, and the
drive above is enough to derive a measured pattern for this pack when it is.

`tests/test_pulls_scenario.py` pins the calibration: each pull's peak against
the car's, the cell floor within 60 mV, the spread against the fitted line, and
a tight pack when parked. 519 simulator tests still pass. Nothing here has run
on a car; it is the simulator reproducing numbers a car produced.

### A timeline that slides, and a cadence that keeps up  2026-09-11

The owner drove the `pulls` scenario and said the current curve was very
sharp — and diagnosed half of it himself: *"part of that is our cycle
resolution being so low near 1 Hz."* Both halves turned out to be fixable in
an afternoon, and only one of them needed code.

**The cadence half needed none.** Over BLE a cycle is 1.5–3 s of adapter round
trips, so a six-second pull gets three or four samples whatever `--interval`
says. Behind the simulated bus a cycle is milliseconds, so `--interval` stops
being a floor and becomes the only governor. Measured on the laptop on
2026-09-10, against the virtual bus and therefore a statement about the
software and never about a car: `python web/app.py --adapter sim --sim-can
--scenario pulls --interval 0.1` produced **8.6 rows a second with a cell set
on every row** — 197 rows over 23 s, median gap 0.10 s — against roughly one
row a second at the default interval over a slow transport. With the cell log
armed, `lbc02` joins the fast lane and every fresh cell read gets its own row,
which is why the cells keep up with the rows. That recipe is now written down
beside the pulls material in `docs/SIMULATOR.md`, where someone looking at the
3D tile will find it.

**The scenario half needed a flag.** A timeline is a step function by design,
and `pulls.json` moves `speed_mph` every half second, so the current climbs in
stairs even at 10 Hz. A timeline entry can now carry `"ramp": true`: its
*numeric* knobs interpolate linearly from the value they held at the previous
entry to this entry's value, across the interval between the two, landing
exactly on the target at the entry's own `t`. Non-numeric knobs snap at that
`t` and always will — half of `D` is not a gear and 0.5 of `handbrake` is not
a brake.

Three things about the implementation are worth keeping. The interpolation is
a function of **absolute simulated time**, not of an increment, so it is right
for any `dt`, for the model's bounded sub-steps and under `--speed`: ten
`step(1)`s, one `step(10)` and 3600× all put a ramp in the same place, and a
step that jumps clean over the entry still lands on the target rather than
somewhere short of it. A ramp **arms at the previous entry's time, after every
already-armed ramp has been advanced to that instant**, so it starts from the
value the timeline actually left behind; getting that ordering wrong was the
one real bug on the way — consecutive half-second entries captured each
other's un-advanced values and the speed sat still for four seconds and then
jumped. And an entry without the flag is the step function it always was,
which `tests/test_scenario_ramp.py` pins as a compatibility guarantee for
every scenario ever written, this project's and anyone else's.

It is per entry, not per knob, because per-knob control already exists without
widening the format: two entries sharing a timestamp, one ramped and one not.
Only `pulls` ships ramped. `drive` and `commute` are deliberately step
*shapes*, the fault scenarios inject at instants, and `pull.json` feeds the
expected half of `tools/compare_sessions.py` and should not change shape for
cosmetic reasons; a test pins that `pulls` is the only ramped scenario, so
adding another is a conscious act.

**What it bought, in numbers.** Sampling the whole 133 s scenario every 0.1 s,
the biggest jump in current between two consecutive samples fell from **217 A
to 20 A**, and the jumps over 10 A from 37 to 3 — the three survivors being
the pedal presses that start a pull, which still snap, because that is what a
foot does. The hardest pull's peak moved by 0.3 A, from −266.0 to −265.7, so
the calibration against the owner's 2026-09-09 drive is untouched and
`tests/test_pulls_scenario.py` passes **unchanged**: that was the condition on
the whole change, and re-solving the pedal figures turned out not to be
needed, precisely because a ramp lands exactly on its target at the entry's
own time.

And the scenario says what it is: the ramp is a *presentation* choice. A real
car's current does rise over a fraction of a second rather than instantly, so
sliding is at least as honest as stepping — but nothing was measured between
two timeline points, and `pulls.json`'s description now says so in the same
breath as it says the widening spread is stepped rather than physical.
---

## 2026-09-11 — the trouble-code dictionary: the format ships, the data does not

Trouble codes have been readable on the Lancer since August — `dtc_stored`,
`dtc_pending` and `dtc_trans`, twelve engine codes and a CVT code at the last
capture — and they have been displayed as bare codes ever since, because a
`P0868` on a tile tells you nothing you did not already know. This session
added the missing half, and the interesting part is what it deliberately does
*not* add.

**The licensing is genuinely hostile, and pretending otherwise would be the
easy mistake.** SAE J2012, the standard that defines the generic descriptions,
is a paid copyrighted document with no open grant. The Leaf-specific text in
the tools people use carries Nissan ESM page references beside each row —
`EVC-279`, `WT-26` — which is about as clear a statement of derivation as one
could ask for, and the ESM is a paid subscription. And every "open" DTC dataset
the research memo examined turned out to be the same table in a different
wrapper, with no stated chain of title anywhere. An MIT header on a file does
not cure the licence of content the uploader did not own.

So the owner's call: **ship the format, not the data.** The application defines
the schema and reads a dictionary from a machine-local file; `docs/DTC_DICTIONARY.md`
carries a recipe a user hands to an AI agent to build one for the car they
actually own; the result lives in gitignored `dtc/`, exactly like
`config.local.json`. An absent dictionary is the *normal* case — with no file
the dashboard shows bare codes, which is what it did yesterday — and that is
the property the tests defend hardest.

**`dtc.py`** does discovery (a generic file under a profile file, a profile's
own `DTC_FILES`, or paths named in `config.local.json`), validation, and cheap
cached lookups, and it never raises: a missing file is silent and a malformed
one logs once and behaves as absent. A dashboard that stopped showing codes
because a hand-edited JSON file lost a comma would be a worse failure than one
that shows no descriptions.

**Five required fields, and four that were deliberately left out.** Required:
`code`, `desc`, `scope`, `evidence`, `source`. Omitted: `ecu` (it belongs to
the sighting, not the code), `severity` (every schema surveyed has the field
and not one fills it — guessing severity on a safety-adjacent EV fault is worse
than showing nothing), `system` (derivable from the letter; storing it invites
it to disagree with the code) and `possible_fixes` (this is a telemetry
dashboard, not a repair manual, and that is exactly where manual text would end
up). The cautionary example is a public dictionary whose entries carry fourteen
fields of which three are filled — `"severity": "unknown"`, `"title": "No
Title"`, `"sources": []`. A schema with fourteen fields and three real ones is
worse than one with five that are all real, because it teaches the reader to
ignore the fields. Hence a validator that **rejects placeholder descriptions**
rather than accepting them politely, which matters doubly for a file an agent
will populate: an agent asked for 200 descriptions will produce 200.

**The marking rule is code, not convention.** Every description carries an
evidence tier — `service-manual`, `standard`, `observed`, `community`,
`unverified` — and the two weak tiers come back with the tier inside the string
the server produces. So `P1234 — … (unverified)` reaches every renderer already
marked, and a renderer that knows nothing about trouble codes still cannot show
a guess as though it were a manual quote. An unverified description displayed
like a verified one turns a known unknown into a confident wrong answer.

**Wiring.** The Lancer's three code signals are flagged `"dtc": True` in the
profile registry — the only thing outside `vehicles/` that knows which signals
carry codes — and the description is added on the way *out* of `/api/status`,
never on the way in, so the store keeps the raw code that was read off the car
(`dtc_raw` carries the original, `dtc_desc` the structured rows). The text tile
lays a described list out instead of shouting it in caps.

**And "we won't commit it" is now a property of the repository**, not a habit:
`dtc/` is gitignored, and the privacy sweep fails on a tracked dictionary by
path *and* by content, so renaming one out of `dtc/` does not get it past. The
only dictionary in the tree is `tests/fixtures/dtc_sample.json`, three entries,
every code and description invented, every row `unverified`.

**Not done, on purpose:** reading codes from the Leaf. The memo verified the
recipe — `19 02 0E` to `0x79B`, no session control needed, and use `0x0E` not
`0xFF` or a parked pack answers with ~150 rows of "self-test not run this
cycle" that look like catastrophe — but that is a car-side sprint. No UDS
service was added, the read-only whitelist is untouched, and `leaf_ze0` still
declares no code signals. The dictionary is ready for that day without assuming
it.

**1094 passed, 2 skipped** — 57 of them new in `tests/test_dtc.py` and five more in
`tests/test_privacy_sweep.py`, including an end-to-end sweep failure on a throwaway
repository with a dictionary force-added to it. No car was involved in any of this.
---

## 2026-09-11 — a raw output console, and the first framework tile

The owner asked for "a pane that displays direct CAN frames in a scrolling,
terminal-like display. A debug view." The plan written on 2026-09-10 said the UI
was the easy half and the frames were the problem: the reader is a separate
process, it discards raw lines the moment the profile has decoded them, and what
each transport can even offer differs sharply. His own decisions on that plan
widened it usefully — it is a console for the *transport's* output, not only for
CAN frames, and it belongs to every vehicle rather than to each profile.

**Built-in tiles used to be a car's.** That was right while every one of them was
Leaf art: the Lancer declares `TILES = []` and gets user signal tiles instead. It
is wrong for a tile that describes the transport, because then a second car's
author has to opt in to a debug view he never wrote. So `vehicles/__init__.py`
grew `FRAMEWORK_TILES` and the three merged views the reader binds — `tiles()`,
`default_span()`, `default_tiles()`. The console is the first member: id
`console`, "Raw output", full width, **disabled by default**. A profile that
declares one of those ids is now rejected, because two definitions of one tile
would silently disagree about its items. Everything downstream reads the merged
views and cannot tell where a tile came from; the only place that deliberately
does not is `sim_ctx()`, which includes partials a *profile* can drive.

**Off means off.** The reader learns the tile is enabled from `web/tiles.json` —
the same mtime path the cell log already used — and only then builds a ring and
points the taps at it. No tile, no object, no tap, no file, and turning it off
drops all three. Arming logs a line and puts its own event in the pane, so the
console is never blank while waiting for a first frame.

**What each transport can honestly give, which is the whole point.** Native CAN,
MQTT and the simulated bus all reach the reader through `CanFacade`, so one
additive `tap` attribute there covers all three and sees every broadcast frame.
An ELM327 has no façade: its frames are the `ATMA` lines `poll_bus` already
holds — only the ids the profile polls, only during their dwell — and the pane
says exactly that, in the pane, whenever the primary transport is one. Frames
captured for a UDS request are deliberately not tapped as frames; the reader
emits one grouped `uds` entry per request instead, `2101 -> 7BB 10 29 61 01 /
…`, which is the pairing that makes a decoded value trustworthy rather than
magic. On top of those: adapter replies and refusals, every `text`-kind signal
as it changes (the Lancer's stored codes, the case that makes this worth having
on a car that is not the Leaf), and reader events — connect, reconnect, bus
down, link dropped, asleep, awake again.

**The rate problem is answered in the reader, not the browser.** Car-CAN carries
about 1,700 frames a second. The ring keeps only the ids the enabled tiles poll,
caps each id at N frames per second, and offers an explicit "everything" mode
that says it is lossy. Every dropped frame is counted per id and published in
the record, because a pane that silently thinned its own data would be worse
than no pane. Only frames are ever decimated: an answer or an event is not a
stream, and dropping one loses the thing the person was watching for. The kind
filter is the other half — frames are the only firehose, and they are one
checkbox.

**Getting it to the page** needed no new IPC: the ring is flushed once a cycle
to `web/console.jsonl` (gitignored, size-capped, cleared on each arming), which
is the state file's own pattern, and `GET /api/console?since=&kind=&ids=&limit=`
serves the tail after an **opaque cursor** — a cursor and not a timestamp,
because two frames can share a millisecond. A `limit` returns the newest
matches, so a pane that fell behind gets the end of the stream rather than the
start of a backlog; that also means the endpoint cannot page forward through
one, which is deliberate and documented.

**The pane** is monospace, newest at the bottom, auto-scrolling only while it is
already at the bottom, with a Pause button that is not a nicety — a scrolling
pane at any real frame rate is unreadable. A byte that changed from that id's
previous frame is highlighted (a first sighting highlights nothing: "everything
changed" says nothing), and clicking a line copies it as `ID B0 B1 …`, the exact
shape the decoders and fixtures use. "Known ids only" is built from the
profile's own items — `/api/signals` now reports each item's `can_id` — so
nothing in the browser knows what a Leaf is.

**What it is not**, said in the doc and in the code: not a capture tool.
`record_session.py` records a session properly and the bridge logs losslessly.
This drops frames on purpose. `web/console.py` has no transport handle in it at
all, and a test asserts the module imports no transport and defines no `send` —
the read-only rule applied to the debug pane.

61 tests in `tests/test_console.py`: the ring's bounds and eviction, the
decimation and its per-id drop counts, the cursor, the endpoint's filters and
limit, the tap staying off while the tile is disabled, each transport's entry
shapes, the ELM327 partial view being labelled, the tile's page strings, and the
pure half of `console.js` under node. 1,090 tests pass. Nothing here has run on
a car, and the pane's own feel — scrolling, pausing, copying a line — still has
to be read in a browser and then in the passenger seat.
