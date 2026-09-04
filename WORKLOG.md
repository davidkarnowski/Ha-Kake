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
