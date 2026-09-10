<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: CC-BY-SA-4.0
-->

# Native CAN transport — a CANable on the OBD port

**Status (2026-09-09): designed and tested against a virtual bus; not yet run
on the adapter or on a car.** Every claim below about the board, its firmware
and its LEDs comes from documentation, not from this bench. What the tests
prove is listed at the end; what only the car can prove is listed next to it.

## What it is

`--adapter can` reads the car through a native USB-CAN controller instead of
an ELM327. The board is a **CANable 2.0 class** adapter: an STM32G431 with a
USB-C port, a 3-pin CAN H / L / GND terminal and a 120 Ω termination jumper,
sold under many brand names. It ships with one of two firmwares — **slcan**
(a CDC serial port speaking the Lawicel text protocol) or **candleLight**
(a vendor-class USB device, `gs_usb`) — and this transport drives either
through [python-can](https://python-can.readthedocs.io).

Why bother, when the ELM327 works: a native controller **receives every
frame on the bus all the time**. A passive item (gear, doors, TPMS, …) stops
costing an `ATCRA` + `ATMA` dwell of 0.2–0.8 s and costs a dictionary lookup;
the cycle collapses to the UDS requests, which at 500 kbit/s are milliseconds.
And the board can be put on the **EV-CAN** pins (13/12), which no ELM327 in
this project has reached — in the controller's listen-only mode, where it
cannot transmit a single bit.

Nothing above the transport changes. `web/reader.py`, the vehicle profiles
and the decoders keep speaking ELM327; `cantransport.py` is an ELM-speaking
façade over a frame source (`docs/ARCHITECTURE.md` "Transport").

## The 10-minute arrival checklist (no car)

Do this once, at the desk, before the board ever sees the OBD port.

1. **Termination jumper OFF.** Both of the Leaf's buses are already
   terminated inside the car. Find the jumper (or switch) next to the
   terminal block and remove it. See "Wiring" for why this is not optional.
2. **Plug it in with a USB-C *data* cable** (not a charge-only one) and run
   `system_profiler SPUSBDataType` (macOS) or `lsusb` (Linux). The
   Vendor:Product ID says which firmware it runs:

   | USB ID | Firmware | What the transport does |
   |---|---|---|
   | `16d0:117e`, product "CANable2 …" | **slcan** (stock canable2-fw) | opens the serial port; pyserial only |
   | `1d50:606f` "Geschwister Schneider CAN adapter" | **candleLight** / `gs_usb` | needs `pip install "python-can[gs-usb]"` and libusb (`brew install libusb`) |
   | `0483:df11` "STM Device in DFU Mode" | **bootloader** — the board is stuck in boot mode | refuses to open; unplug for 5 s and replug |

   Anything else: write it down and look it up before going further.
3. **Serial port present or not.** `ls /dev/cu.usbmodem*` (macOS) /
   `ls /dev/ttyACM*` (Linux). slcan firmware enumerates a port; candleLight
   shows none. That is the second half of the identification.
4. **LEDs at idle.** Note which LEDs light and how; the clones do not all wire
   the LEDs the way the stock firmware drives them, so an LED that never
   lights is cosmetic until proven otherwise. Do not trust an LED as a
   "bus is fine" signal until it has been seen blinking with real traffic.
5. **`python -c "import can; print(can.__version__)"`** in the venv — 4.6.1.
   For candleLight, also `python -c "import usb.core; print(list(usb.core.find(find_all=True)))"`.
6. Put `can_bus` in `config.local.json` (below) **before** the first run. The
   transport refuses to start without it.

Firmware changes are pending the owner's bench check.

## Wiring

The ZE0 (2011–2012 Leaf) OBD-II port, from the pin table in
`docs/SIGNALS.md` and the sources cited there:

| Pin | Signal |
|---|---|
| 4 / 5 | chassis / signal ground |
| **6** / **14** | **Car-CAN** H / L — what the ELM327 uses today |
| **13** / **12** | **EV-CAN** H / L — battery, inverter, charger network |
| 11 / 3 | AV-CAN (infotainment; nothing this project reads) |
| 16 | permanent +12 V |

Both buses run 500 kbit/s, 11-bit ids.

Two attachments, one board:

- **A. Car-CAN, the CANable replaces the ELM327.** Pin 6 → CANH, 14 → CANL,
  4 or 5 → GND. Normal mode: the same UDS `0x21` read requests the ELM327
  sends today, and the passive ids without a dwell.
- **B. EV-CAN through a breakout, the ELM327 stays on Car-CAN.** An OBD-II
  pass-through breakout that exposes all 16 pins; the ELM327 in its female
  socket as before, the CANable's terminals to **13 → CANH, 12 → CANL,
  4 → GND**. `can_bus: "ev"` — listen-only, always.

Keep the H/L pair short (under ~30 cm) and twisted, and connect ground: a CAN
bus does not work without it. Never connect anything to a 5 V output, on the
boards that have one. The board is not galvanically isolated; USB ground and
car ground meet at the laptop, which is the same situation as the USB ELM327
and has been fine.

**Termination — the safety paragraph.** A working CAN bus has exactly two
120 Ω terminators, one at each end, and reads about 60 Ω across H/L. The
car's buses already have theirs. With the board's jumper on, a third resistor
puts 40 Ω across the bus: the dominant level sags, the transceivers are
overloaded, a reflection point appears — and that is not "my adapter does not
work", it is **every other controller on that bus seeing a degraded signal**,
on EV-CAN the battery controller and the inverter among them. Jumper off,
verified with an ohmmeter across the terminals (open, with the board
unplugged from the car) before the first connection. The car's port reads
~60 Ω between 6/14 and between 13/12 with the car off.

## Listen-only on EV-CAN

A CAN controller in listen-only (bus-monitoring) mode does not drive the bus
at all — not data, not error flags, not even the acknowledgement bit every
other node contributes. A bus with one silent listener is electrically
identical to the bus without it, whatever the host software does. That is the
strongest reading of `SECURITY.md`'s read-only rule, and it is the **only**
mode this transport ever opens the EV bus in:

- `can_bus: "ev"` forces `listen_only`; `can_listen_only: false` is ignored
  there, and there is no override.
- On a listen-only bus every non-`AT` command answers `NO DATA` and is logged
  once; `send()` on the underlying bus is never called. UDS items in the
  profile simply read as silent.
- **candleLight (`gs_usb`)**: python-can opens the device in normal mode with
  no way to ask for another, so the transport re-opens it silent through the
  `gs_usb` package and **reads the mode back** from the device. A firmware
  that does not advertise listen-only drops the bit silently — in which case
  the transport refuses to open the bus at all, rather than open it normal.
- **slcan (stock firmware)**: silent mode is `M1` sent before `O`. The stock
  firmware neither implements python-can's `L` command nor acknowledges `M1`,
  so this cannot be verified from the host; the log says so. The software
  guarantee (no `send()` ever) still holds; the electrical one is the
  firmware's word. Prefer candleLight for EV-CAN once the bench check is done.
- **socketcan (Linux)**: the kernel enforces it. The transport checks
  `ip -details link show` for `LISTEN-ONLY` and refuses otherwise.

## Configuration

`config.local.json` (gitignored). `HAKAKE_CAN_*` environment variables win
over the file, the way the BLE keys do.

```json
{
  "can_bus": "car",
  "can_interface": "auto",
  "can_channel": "",
  "can_bitrate": 500000,
  "can_listen_only": false
}
```

| Key | Meaning | Default |
|---|---|---|
| `can_bus` | `"car"` or `"ev"` — which OBD pins the board is wired to. **Required; never guessed.** The board cannot tell, and normal mode on EV-CAN would be a transmit-capable node on the battery bus. | — |
| `can_interface` | `auto` (identify the firmware from the USB id), `slcan`, `gs_usb`, `socketcan`, `virtual` (tests); `sim` is recognised and refused — the simulated bus is `--adapter sim --sim-can`, below | `auto` |
| `can_channel` | slcan: the serial port (found by USB id or `/dev/cu.usbmodem*` when empty); gs_usb: device index; socketcan: `can0` | `""` |
| `can_bitrate` | bit/s | `500000` |
| `can_listen_only` | open silent even on Car-CAN (then no UDS, only the broadcast ids) | `false`; forced `true` on `ev` |
| `can_isotp_stmin` | force the ISO-TP separation time we ask an ECU for, overriding `ATFCSD`; hex or int | unset (honour `ATFCSD`) |
| `can_response_timeout` | seconds to wait for the *first* frame of a UDS answer before `NO DATA` | `1.0` |
| `can_tx_padding` | pad requests and flow-control frames to 8 bytes with this byte, as the ELM327 does (`ATV0`); `null` for unpadded | `0` |
| `can_libusb_path` | where pyusb finds `libusb-1.0.dylib` on macOS | `/opt/homebrew/lib` |

Then:

```bash
python web/app.py --adapter can                # or: python web/reader.py --adapter can
```

`--adapter can` is never auto-detected — like `replay` and `sim` it must be
asked for, for the reason `can_bus` is required.

## How the façade maps ELM327 commands

`cantransport.CanFacade.send()` keeps exactly the adapter state a real ELM327
keeps between commands and answers everything else from its tables or the
frame source:

| Command | Does | Answers |
|---|---|---|
| `ATZ`, `ATI`, `AT@1` | `ATZ` resets header / filter / flow-control state; no bus action | the adapter name (contains a digit, for the liveness probe); `[]` when the source is offline |
| `ATE0 ATL1 ATH1 ATS1 ATSP6 ATFCSM1 …` | nothing — the bus was opened at 500 k / 11-bit | `[]` |
| `ATSH <id>` / `ATCRA <id>` / `ATAR` / `ATFCSH <id>` | remembers request header, response filter, flow-control header | `[]` |
| `ATCAF0` / `ATCAF1` | bookkeeping only — there is no `DATA ERROR` here | `[]` |
| `ATFCSD 30 <BS> <STmin>` | parses the block size and separation time we send in flow control | `[]` |
| `ATMA` | frames of the filtered id (all ids without a filter) received within the last `timeout` seconds, oldest first, then forgotten — the dwell's semantics with no dwell | `["421 08 00 00", …]` |
| `""` (the poke that ends `ATMA`) | nothing | `[]` |
| `2101`, `2110`, `0100`, `03`, `07` | one ISO-TP request to `ATSH`, answer from `ATCRA`, captured as the **raw response frames** in bus order — `parse_isotp()`'s input, byte-identical to an ELM capture | `["7BB 10 29 61 01 …", "7BB 21 …", …]` or `["NO DATA"]` |
| a request whose service byte is not `0x21` / `0x01` / `0x03` / `0x07` | refused before it reaches any bus (`SECURITY.md`); logged once | `["NO DATA"]` |
| anything on a listen-only bus | refused; logged once | `["NO DATA"]` |
| anything unparseable | logged once | `["?"]` |

Two transport-class attributes matter to the scheduler: `SPEED = 0.05`
(cost of a UDS poll relative to BLE — modelled, not measured) and
`PASSIVE_INSTANT = True`, which makes `Reader.estimate()` drop the passive
items' `secs` because there is no dwell. `STMIN = "00"`: a native controller
absorbs consecutive frames at bus speed, so the 32 ms gap the BLE clone needs
(and the 5 ms the USB clone needs) is not asked for. Whether the LBC honours
STmin 0 is a bench question; `can_isotp_stmin` puts it back if not.

Every record carries `can_bus` and `listen_only`, so the header can say
"EV-CAN · listen-only" and a stored row remembers where it came from.

## What is verified, and what is not

**Verified by `tests/test_cantransport.py` (no hardware, python-can `virtual`):**
the fixture round trip — a fake ECU puts the recorded `7BB` frames on the
bus honouring our flow control, and `send("2101")` / `2102` / `2104` return
the fixture lines byte for byte, so `parse_isotp()` and `decode_reading()`
give the same record as the ELM path; the flow-control frame carries the
`ATFCSD` bytes padded to 8; a silent ECU costs `can_response_timeout`, not
the item's 10 s; `ATMA` window, drain, filter and DLC; the ELM state machine;
`STMIN`/`SPEED`/`PASSIVE_INSTANT` and the scheduler tweak; listen-only on the
EV bus refusing every request with a spy bus receiving nothing; the `gs_usb`
listen-only verification against a stub device; slcan `M1` then `O`; firmware
classification from USB ids and the DFU refusal; `detect_adapter("can")`
refusing to run without `can_bus`; auto-detect never picking it; a dead
source failing the liveness probe; one reader `poll_once()` end to end.

**Not verified — needs the board, then the car (memo phases a–b):** that the
stock firmware is what ships and enumerates as documented; that `M1` puts it
silent; that python-can's slcan reader keeps up with ~1,700 frames/s on
Car-CAN; the real `SPEED`; that the LBC honours STmin 0 and 0x00-padded
requests from this controller as it does from the ELM; the 1 % RC-oscillator
margin at 500 kbit/s; the BOOT0 drop-out (below); EV-CAN in listen-only at
all. The cell log, the HVAC amp and the Lancer's mode 01/03/07 over this
transport are untested beyond the fixture round trip.

**BOOT0.** On this chip the CAN RX pin doubles as BOOT0, and on some clones a
short interruption of USB power can leave the board in the bootloader
(`0483:df11`, only the power LED lit) until it is unplugged for ~5 s. The
transport recognises that state and says so instead of reporting "no
adapter". Firmware changes are pending the owner's bench check.

## The simulated bus — the same façade at the real frame rate, no board

```bash
python web/app.py --adapter sim --sim-can          # Car-CAN channel, ≈1,700 frames/s
python web/app.py --adapter sim --sim-can ev       # plus the EV-CAN channel, listen-only
python tools/bench_canrate.py                      # cycle time, intake, CPU, hit rate
```

`simulator/canbus.py` puts the model's ECUs on an in-process python-can
`virtual` channel — every id the profile decodes at its surveyed period,
filler ids to the surveyed volume, ISO-TP answers for the LBC and HVAC amp
honouring BS/STmin, `7F xx 11` for any service but `0x21` — and this façade
opens the same channel through `SimCanSource` / `SimCanFacade` (the block at
the end of `cantransport.py`): `adapter_type = "sim"`, `simulated = True`,
the marker carries scenario, seed and `sim_bus_load`, the rows go to the sim
database. `can_interface: "sim"` through `--adapter can` is refused with that
advice, because the database is chosen by `--adapter`. Full description,
the pull scenario and the comparison tool: `docs/SIMULATOR.md`, "The
simulated bus".

What the bench measured (`tools/bench_canrate.py`, 10 s per load, the
reader's `poll_once()` with every tile on, paced at `--interval 0.5`; Darwin
arm64, Python 3.12, 2026-09-09) — **MEASURED ON THE LAPTOP, VIRTUAL BUS: no
wire, no bit errors, no LBC pacing, no USB, no slcan parser**:

| bus load | expected fps | broadcast fps | intake fps (incl. UDS) | sched. cycle med / p90 (ms) | full cycle med / p90 (ms) | CPU total / ECU / reader (%) | UDS misses |
|---|---|---|---|---|---|---|---|
| 0.1 | 525 | 526 | 572 | 3.9 / 4.6 | 13.5 / 14.1 | 17.3 / 9.7 / 7.7 | 0 |
| 0.5 | 1,044 | 1,045 | 1,091 | 3.4 / 4.6 | 13.8 / 13.8 | 21.7 / 11.8 / 9.8 | 0 |
| 1.0 | 1,693 | 1,693 | 1,739 | 3.9 / 4.3 | 15.3 / 18.2 | 30.6 / 15.7 / 14.9 | 0 |

The reader's own cost at the full surveyed rate is ~15 % of one core (the
ECU share is the simulated car); a scheduled cycle is ~4 ms and a cycle
polling every item ~15 ms, against the ~50–150 ms modelled in the memo for
the real bus (the difference is the LBC's own pacing, which this rig does
not have); every passive item is hit at its own `secs` window at every
load. What this cannot say: whether python-can's slcan byte reader keeps up
on a real USB link, the board's error counters, the LBC's real turnaround.
Those are still the arrival checklist's questions.

## Where things live

| | |
|---|---|
| `cantransport.py` | `CanFacade`, `FrameSource`, `LocalSource`, firmware detection, `can_config()`, `open_can()` |
| `elm327.py` `detect_adapter(prefer="can")` | the one branch that builds it |
| `web/reader.py` | `--adapter can`; `passive_instant` in `estimate()`; the `marker()` stamp |
| `mqttsource.py`, `docs/MQTT.md` | the second frame source: the same façade over a broker |
| `tests/test_cantransport.py` | everything in "Verified" above |
| `simulator/canbus.py`, `tests/test_sim_canbus.py` | the simulated bus: ECUs, periods, EV-CAN layouts (ASSERTED), the sim-can transport |
| `tools/bench_canrate.py`, `tools/compare_sessions.py` | the bench above; the expected-vs-observed pull comparison for arrival day |
| `research/canable_adapter_recommendation_20260908.md` | the design memo (local, gitignored) |
