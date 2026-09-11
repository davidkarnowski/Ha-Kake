# Leaf OBD Dashboard — Improvement Plan (2026-08-24)

> **Status — updated 2026-09-10; entries below are dated in place, oldest first.**
>
> **2026-08-24 (evening):** first sprint complete — A1 ✅ A2 ✅ A3 ✅ A4 ✅ B1 ✅ B2 ✅ D2 ✅ D3 ✅ D4 ✅ (P1–P10 all addressed). Later the same evening: sqlite thread-safety crash fixed (reader is a subprocess), Car-CAN passive signals + HVAC amp decoded, Vehicle / Tires / Climate tiles added, cycle time cut from ~28 s to ≤8 s, then to ~2 s with the tile-driven scheduler (C3 done in spirit); dynamic tiles menu; open-source docs + local git repo; Tile Studio (signal registry, per-tile menus, 12 renderers, 7 colour scales, user tiles) and the ADDING_SIGNALS routine. Since then: HVAC setpoint/fan calibration ✅ (walks 2026-08-24), N/Eco gear confirmation ✅ (all five `0x421` values live), drag-resize handles ✅ (gridstack). Next up: Phase 3 adaptive store rate, Phase 5 retention config, B3 cell-rank memory, B5/B6 12 V + insulation cards, C4 alerts, C1 coulomb counting, pedals walk (throttle `0x180` / brake `0x292` scales). **2026-08-28:** vehicle-profile seam cut (`vehicles/` package, `--vehicle` flag, contract in `vehicles/__init__.py`); first non-Leaf profile `lancer_2009` (standard mode-01 PIDs) decoding a live idle capture, 87 tests. Same day: Lancer DTC readout added (MIL lamp + stored/pending/trans codes, modes 01/03/07, read-only), 89 tests. **2026-09-02:** replay mode — `--adapter replay` drives the whole stack (reader, scheduler, transport, decoders, store, API, page) from a recorded session fixture made by `record_session.py`, so a profile can be written and reviewed with no car; `docs/REPLAY.md`. **2026-09-03:** the simulator — `--adapter sim` runs the same stack against a running model (`simulator/`, `hakake_sim.py`) with a provenance-labelled load table, one power identity wall → charger → loads → pack, a 20-row couplings audit, the ZE0 push-button start, a control API on `127.0.0.1:8099` and the cockpit at `/sim`; `docs/SIMULATOR.md` and `docs/SIMULATOR_CONTRACT.md`. Same day: the per-tile ⋯ menu portalled out of the card (toggle, Escape, Done) and the tire art scaled by the card. Same day: the USB transport measured and fixed — a 41 ms poll tick and a 50 ms post-prompt sleep were costing ~91 ms on every command; a blocking read to the prompt and `ATBRD` negotiation to 115200 cut a command round-trip from ~107 ms to ~5–9 ms, and `SPEED` makes the scheduler's cost model transport-aware. **2026-09-07:** audible threshold alerts — every tile's ⋯ menu lists the values it shows, each with below/above (or on/off), a tone and a 1–60 s repeat slider, the card flashing on every beep; `web/static/alerts.js` is a Web Audio tone generator plus a pure rule engine (hysteresis, staleness freeze, node-tested), rules ride in `opts.alerts`, built-in tiles declare their `signals` in the profile. The client half of C4. **2026-09-08:** the 3D battery pack tile — the pack drawn as it sits under the car from a layout table in the profile (`PACK_LAYOUT`, no CAD file), 96 cell-pair bodies in one three.js instanced mesh coloured on three scales (deviation from mean, the grid's absolute scale, drop from rest), orbit / zoom / hover / pin, ⋯-menu options through new Tile Studio hooks (`opts`, `enabled`, `menuExtra`, `tiles:applied`); three.js vendored under `web/static/vendor/three/` and reached through the page's first import map; where each pair sits is partly assumed and flagged (`docs/PACK3D.md`, SIGNALS "Cell order in the pack"). B3's heat-map colouring is the *deviation* scale. **Same day:** Live / Playback — a header switch and a page-level timeline (session picker from gaps in the data, SOC + current strip with a brush that re-fetches at full resolution, playhead, ½×–60× transport, keys) that drives every tile from stored frames through the same five sinks `poll()` uses; `Store.frames()` rebuilds the `/api/status` shape from rows (last real row per bucket, never averaged; cells joined on request), `/api/sessions` and `/api/playback/frames` carry it, `web/static/playback.js` is the pure clock (node-tested); recorded frames are never stale and alerts stay silent unless asked; `docs/PLAYBACK.md`. **Same day:** the cell log — `opts.celllog` on the cell grid or 3D pack tile moves `lbc02` into the fast lane (`reader.period_overrides()`, `Reader.period()`) and the main loop stores every fresh cell read (`cells_seq`), which also stops the sticky cache being stored as four identical cell sets; CELL LOG badge; the same read-only request, more often. **Later the same day:** the tile matched to the grid's colours, sensors coloured and selectable, a module pane with the pair voltages in their colours and the module's own spread and average, a pinned module marked with a box, pin and label, lowest/highest and threshold flashes, rounded module bodies, ⟳ and ⤢ on the pane; the timeline docks to the window; pairs counted 1–96 on screen with `cell_min_no`/`cell_max_no` in the record; the pack's section layout verified against the service manual (EVB-20). **Same evening:** timeline flags (`⚑ Flag`, `/api/bookmarks`, `web/bookmarks.json`) and auto-detected pulls (`/api/bookmarks/auto`), a grabbable playhead, the legend clear of the pill; the pack abstracted — `split` / `group` / `PACK_MODES`, bodies vs values, `docs/PACK3D_GUIDE.md` for other packs. **2026-09-09:** the ISO-TP separation time is per transport — `STMIN` 0x05 on USB serial, 0x20 on BLE, replay and sim — after a parked-car probe showed the 29-frame cell answer intact at 5 ms and 0 ms (1.18 s → 0.36 s); the passive captures' full-dwell `ATMA` wait is the next transport cost to cut. **Same day:** the native CAN transport — `cantransport.py`, an ELM327-speaking façade over a frame source, `LocalSource` on python-can for a CANable 2.0 class board (slcan / gs_usb / socketcan / virtual), passive items from a frame table with no dwell, UDS through ISO-TP with the raw frames captured, read-only enforced at the transport, EV-CAN listen-only always; and MQTT ingestion — `mqttsource.py` as the second frame source, a Raspberry Pi bridge (`bridge/`, SocketCAN → broker, kernel filters, batch mode, bounded queue, systemd), a JSON wire protocol with a schema per payload (`docs/MQTT.md`), and the reader publishing its decoded record and every value as retained topics for gauges. Both tested in-process only (virtual bus, fake broker); nothing on a board, a Pi or the car yet. **Same day:** the timing architecture — acquisition time per item (`item_ts`) in every record and row, `ts_source` on every row, the source clock kept beside the laptop's with the offset on the session and never applied, `peak` keys keeping their envelope between stored rows (`<key>_min/_max/_tmin/_tmax/_n`, the pull detector uses it), the per-tile "read at" badge measured from the frame in playback, and `bench_transport.py --timing`; `docs/TIMING.md`. **Same day:** several adapters at once — `bus` on items, `BUSES`, an `adapters` list (one transport per bus, polled concurrently, per-bus target / liveness / reconnect, `adapters` + `bus_alive` in the record, a chip per bus) — and provenance: `sources` / `tolerance` on a SIGNALS entry, a generic resolver (pin → verified → fresher → faster) writing `<key>_src` / `<key>_resolved` / `<key>_disagree` and a `source_disagree` event, the Leaf's `current_a` from groups 01 and 05 as the worked example. Fake transports only; two real adapters not yet run together. **Same day:** the simulated CAN bus — `--adapter sim --sim-can` runs the model's ECUs (`simulator/canbus.py`) on an in-process virtual channel at the surveyed ≈1,700 frames/s behind the native CAN façade, ISO-TP answers honouring flow control, `7F xx 11` for anything but a read, an EV-CAN channel whose seven layouts are ASSERTED from the dalathegreat DBC / OVMS; `tools/bench_canrate.py` (scheduled cycle ~4 ms, reader CPU ~15 % of a core at full rate — on the laptop, virtual bus); the pull scenario and `hakake_sim.py --pull` (sim DB with the cell log, both channels as JSONL, a synthetic fixture reaching the owner's observed −271 A); `tools/compare_sessions.py` for arrival day. The pedal term's shape changed (DRIVE_EFF applied, a constant-power knee) — still ASSERTED. **2026-09-10:** the cell grid and the 3D pack gained a *fixed range* colour scale beside the frame-relative one — `fixed: [lo, hi]` in `PACK_MODES`, overridable per tile in the ⋯ menu, clamped at the ends, legends naming the active range; the default stays the frame's own range (the owner's call), and nothing stored changes. **1181 passing.**

Assessment of the codebase as it stands after Sessions 1–5, followed by a phased plan.
Temperatures are shown as °C / °F throughout (project convention from this date on).

---

## 1. State of the codebase

> *Historical snapshot (2026-08-24, before the A1–A3 restructure) — kept for the
> record. Paths, symbols and line counts below no longer match; `ARCHITECTURE.md`
> describes the current layout.*

### What works well
- `elm327.py` transport abstraction (BLE + USB) with `configure_leaf_bms()` — clean, adapter-agnostic.
- `web/reader.py` decoders for LBC groups 01 / 02 / 04 / 05 are correct and were re-verified live today over BLE.
- `web/templates/index.html` is a polished 1,300-line dashboard: SOC ring, health card, temp gauge (already °F-first with °C sub-label), power gauge + signed sparkline, SOC history, 48-module cell tree.
- Docs: `WORKLOG.md` is a thorough log; every dead end is recorded.

### Problems found

| # | Issue | Where | Impact |
|---|-------|-------|--------|
| P1 | **History is pruned to 24 h** (`HISTORY_MAX_HOURS = 24`) | `reader.py:update_history` | The most valuable dataset — SOH/capacity over months — is thrown away. Only 754 rows from one day (2026-02-19) survive. Today's 1.7 Ah capacity drop had to be found by comparing a JSON snapshot to a log line. |
| P2 | **No reconnect / error recovery** | `reader.py:main`, `app.py:run_reader` | If BLE drops (car goes to sleep, walk out of range) the reader thread dies; dashboard shows a stale green "ok" forever. |
| P3 | **Copy-pasted transport & decoder code** | 5× `SerialELM` in `usb_*.py`, ~10× bleak boilerplate, 4× `parse_isotp`, 3× `decode_group01` | Bug fixes don't propagate; only `reader.py` uses `elm327.py`. |
| P4 | **Charge/discharge sign is inferred from SOC delta** in JS | `index.html:drawPowerSparkline` | Reader already has the true `discharging` flag from group 05 but doesn't write it to history. Heuristic misclassifies at low currents / stable SOC. |
| P5 | **Power uses an estimated pack voltage** (`(max+min)/2 × 96`) | `reader.py:decode_group05` | Real pack voltage is available in group 01 bytes 18–19 (÷100) — found today. |
| P6 | History file fully rewritten every poll (~100 KB JSON) | `reader.py` | Fine now; will not scale past days of data. |
| P7 | Console tools show temps in °C only | `battery_cell_read.py`, `usb_battery_read.py`, `BatteryLogger.py` | Project convention is °F alongside °C. |
| P8 | Stale label: `174` → "Climate/HVAC" | `live_stream.py:CAN_ID_LABELS` | It's gear position. |
| P9 | Naive local timestamps, no timezone | everywhere | Ambiguous in logs across DST. |
| P10 | No tests | — | We have raw frame captures; decoders could be regression-tested offline without the car. |
| P11 | Lancer notes shared this folder (since moved to gitignored `research/`) | — | Two vehicles share adapters and the ELM327 layer; the code isn't structured for that. |

### Newly decoded today (Group 01, 39-byte payload)

| Bytes | Field | Scale | Verified against |
|-------|-------|-------|------------------|
| 0–3 | HV current sensor 1 | s32 ÷ 1024 A (negative = discharge) | group 05 current, same magnitude |
| 6–9 | HV current sensor 2 | s32 ÷ 1024 A | same |
| 18–19 | **HV pack voltage** | u16 ÷ 100 V | 96-cell sum (384.26 vs 384.3 V) |
| 20–21 | 12 V battery | ÷ 1024 V | (already known) |
| 22–23 | Insulation | kΩ | (already known) |
| 26–27 | HX | ÷ 100 | (already known) |
| 29–31 | SOC | ÷ 10000 % | (already known) |
| 33–35 | Capacity | ÷ 10000 Ah | (already known) |
| 4–5, 24–25 | Unknown (0x0287 = 647, 0x00F2 = 242, static) | | candidates: charge-state flags / limits |

Consequence: a fast "power loop" needs only group 01 (6 frames) instead of group 01 + 05 + 02 (46 frames) → ~4× higher sample rate over BLE.

---

## 2. Plan

### Phase A — Foundation (make the data trustworthy and permanent)

A1. **SQLite time-series store** (`web/store.py`)
- Tables: `readings` (one row per poll: ts_utc, soc, pack_v, current_a, power_kw, discharging, temps 1–4, capacity_ah, soh, hx, lv_v, insulation, spread, min_cell_idx, max_cell_idx), `cells` (ts, idx, mv — 96 rows per full read), `sessions` (adapter, connect/disconnect times).
- Never prune. 10-second polling for 2 h/day ≈ 720 rows/day; a year is < 50 MB with cells.
- Migrate `battery_history.json` and `battery_log_20260215_230145.jsonl` into it on first run (both contain Feb data).
- Keep `battery_state.json` as the "latest" file for the dashboard; replace `/api/history` with SQL-backed downsampling (shipped as `?minutes=<n>` + `?max=<n>`).

A2. **Resilient reader loop**
- Wrap connect → configure → poll in a supervisor: on any exception, mark state `{"status":"reconnecting"}`, back off (2 s → 30 s), re-detect adapter, re-run `configure_leaf_bms`.
- Detect "car asleep" (all groups NO DATA) and drop to a 60 s heartbeat instead of hammering the adapter.
- Dashboard: yellow dot + "last reading 4 m ago" instead of stale green.

A3. **Decoder consolidation** (`leaf_decoders.py`)
- Single module: `parse_isotp`, `decode_group01` (extended with the fields above + `temp_f`), `decode_group02`, `decode_group04` (°C + °F), `decode_group05`, `decode_group06` (balancing flags — decoded in Session 4 but never wired in).
- Rewrite the `usb_*` and BLE one-offs to import from it, or archive them under `legacy/` with a note. Console tools print `34 °C / 93 °F`.
- Fix P4/P5: history stores `discharging`; power = pack_v(group 01) × current.

A4. **Offline decoder tests** (`tests/test_decoders.py`)
- Fixture raw frames from `query_results.log`, today's group 01 payloads, and a saved 2102 capture. Assert SOC, capacity, pack V, cell count = 96, temps.
- `python -m pytest` runs without the car; protects every later refactor.

### Phase B — Dashboard upgrades (use what we already collect)

B1. **Degradation view** — capacity Ah / SOH vs. calendar date across *all* readings, with a linear fit and "projected date to 8 bars / 30 Ah" line. This is the chart that answers "how fast is my pack dying" and it's impossible today because of P1.
B2. **Long-range selector** — 1 h / 24 h / 7 d / 30 d / all on both SOC and power sparklines (currently minutes only).
B3. **Cell-pair heat map with memory** — colour each of the 96 pairs by its deviation from pack mean, and add a "rank stability" overlay: pairs that have been in the bottom 5 for >N readings get a persistent marker. (Cell 53 was weakest in Feb; cell 55 today — same module region, worth watching.)
B4. **Cell balancing indicator** — group 06 flags shown as small dots on the module tree while the BMS is actively bleeding a pair (only visible during/after charge — a nice "is balancing working?" check).
B5. **12 V battery card** — `lv_volts` with °F-aware thresholds and a 7-day min/max. Weak 12 V is the #1 Leaf failure mode and we already read it every poll.
B6. **Insulation resistance trend** — plain sparkline + alert threshold (< 500 kΩ). Safety signal we already read but never display over time. *(The threshold half is now one row in the health tile's Alerts section, 2026-09-07; the sparkline remains.)*
B7. **Charge-session panel** — when `discharging == False` and current > 2 A, open a session: start SOC, kW curve, kWh delivered (∫P dt), estimated time-to-target. Close on current → 0.

### Phase C — Novel features

C1. **Empirical capacity via coulomb counting** — integrate group-01 current over a drive or a charge (∫I dt) and divide by ΔSOC. Gives a *measured* Ah independent of the BMS's own estimate; compare the two over time. Nobody's hobby dashboard does this because it needs the persistent store (A1) and a fast current loop (group 01 only).
C2. **Per-cell DC internal-resistance map** — capture cell voltages at two known currents (e.g. heater off → on, ~10 A step, or accessory load) and compute ΔV/ΔI per pair. Weak pairs show high IR long before they show low resting voltage. Requires a "snapshot mode" that reads 2102 immediately before/after a load step. Repeat quarterly; plot IR vs. cell index vs. date.
C3. **Drive-mode adaptive polling** — interleave a 200 ms `ATCRA 174` / `ATMA` on Car-CAN to read gear. In P/N: slow full reads (all groups, 30 s). In D/R: group-01-only power loop at max rate, plus 0x284/0x285 for speed → live Wh/mile and per-trip energy log. Turns the dashboard into a trip computer.
C4. **Weak-cell early-warning alerts** — rules over the SQLite store: spread > 50 mV at rest, any pair > 3σ below mean for 3 consecutive full reads, insulation < 500 kΩ, 12 V < 12.0 V at rest. Deliver via macOS `osascript` notification and/or ntfy.sh push (phone). Add a `/api/alerts` feed and a bell in the header. *Partly done 2026-09-07, client-side:* per-value audible thresholds live in each tile's ⋯ menu (`web/static/alerts.js`, rules in `opts.alerts`, a 🔔 in the header) and cover the single-value cases (spread, insulation, 12 V) with one row each. Still open: the store-backed multi-read rules (3σ over consecutive reads, "at rest" gating), `/api/alerts`, and push delivery that reaches a phone when no browser is open.
C5. **Temperature-normalized SOH** — capacity readings drift with pack temp (today 35 °C/95 °F vs Feb 18 °C/64 °F). Store temp with every capacity reading and fit `Ah = a + b·T + c·t` so the degradation slope isn't polluted by seasonal temperature.
C6. **"Battery passport" export** — one-click CSV/JSON of all cell voltages, SOH, IR map, and history, plus a printable summary page. Useful for resale, for comparing with other ZE0 owners, and for LeafSpy-format import.
C7. **Headless Pi Zero deployment** — the reader is already async and adapter-agnostic; package it as a systemd service on a Pi with the USB adapter permanently in the car, syncing SQLite to the Mac over Wi-Fi. This also serves the Lancer project's Phase 4.
C8. **Unified CAN discovery toolkit** — generalize `gear_probe.py` / `drive_eco_diff.py` into a `canprobe` CLI ("filter these IDs, prompt the user to do X, show only changing bytes"). Re-use immediately for the Lancer door-status hunt and for the Leaf's still-undecoded Car-CAN IDs (0x284/0x285 speed, 0x1D5 torque, 0x260 power limits).

### Phase D — Housekeeping
- D1. Restructure into a package: `leafobd/` (`elm327.py`, `decoders.py`, `store.py`, `reader.py`), `web/`, `tools/` (probes), `legacy/`. Lancer scripts get `lancer/`, sharing `elm327.py`.
- D2. `pyproject.toml`, pinned `requirements.txt` (add `pyserial`, `flask`, `pytest`).
- D3. UTC ISO timestamps with `Z`; render in local time on the dashboard.
- D4. Fix stale labels (`live_stream.py` 174 → Gear), add `README.md` quick start.

---

## 3. Suggested order & effort

| Step | Depends on | Effort | Payoff |
|------|-----------|--------|--------|
| A3 decoders + °F + group-01 voltage/current | — | S | correctness, removes duplication |
| A4 tests | A3 | S | safety net |
| A1 SQLite store + migrate Feb data | — | M | **unblocks every trend feature** |
| A2 reconnect supervisor | — | S | dashboard stops lying |
| B1 degradation chart, B2 ranges | A1 | M | the headline chart |
| B5/B6 12 V + insulation, B3 cell rank memory | A1 | S each | cheap wins |
| C4 alerts | A1 | S | phone push when something is wrong |
| C1 coulomb counting | A1, A3 | M | measured vs. reported capacity |
| C3 drive-mode polling + trip log | A2 | M–L | trip computer |
| C2 IR map | A3 | M | best early-warning signal |
| B7 charge panel, C5, C6 | A1 | M | polish |
| C7 Pi Zero, C8 canprobe | D1 | L | permanence, Lancer reuse |

S ≈ an hour or two, M ≈ an evening, L ≈ a weekend.

Recommended first sprint: **A3 → A4 → A1 → A2 → B1**. That turns the current "live viewer" into a real long-term battery-health logger, which is what the last six months of data show you actually need.

---

## Out of scope: HVAC / vehicle control (separate project)

Programmatic control of the car (fan speed, climate, anything that writes to a
bus) is **deliberately not part of this project** and will not be added here.
Decided 2026-08-25.

- **Why separate:** this repo is a read-only telemetry dashboard meant to be
  public and safe for anyone to run on their own Leaf (SECURITY.md). Write /
  actuation code — UDS `0x2F`/`0x31`/`0x27`, or injecting control frames —
  changes the risk profile of every fork and does not belong in something
  people are invited to plug into their car unsupervised.
- **Where it goes:** a future sibling project (working name **Leaf_Control**),
  started only once the native CAN hardware exists (Pi + MCP2515 / 2-CH HAT,
  or ESP32-S3 + transceivers). Control experiments need line-rate CAN,
  precise periodic injection, and a listen-only safety channel — none of
  which the ELM327 does well.
- **First experiment there (read-first):** on EV-CAN, capture the climate
  control panel → HVAC-amp command frame passively; identify the fan/mode/temp
  fields; only then replay exactly that one frame while parked, write path
  behind an explicit flag, watching for side effects. Never folded into a
  dashboard.
- **What this project may still do:** decode more *readable* signals (EV-CAN
  broadcasts once tapped), and expose them. Reading is always in scope;
  writing never is.

---

## Data-logging enhancement plan (2026-08-25, in progress on `feature/logging-granularity`)

Goal: answer questions like "how long was the A/C compressor on and at what RPM"
efficiently, and let the user cap data retention — without losing the long-term
SOH history. (A web charge report was part of this goal; it was built and then
removed at the owner's request — see Phase 4.)

**Phase 1 — promote high-value signals to indexed columns.  ✅ done** The store schema is
already self-migrating (`CREATE … IF NOT EXISTS`, `extra` JSON bag). Promote a
principled set out of `extra` into real columns: `hvac_ac_on`,
`hvac_compressor_rpm`, `hvac_on`, `hvac_fan_on/_speed`, `hvac_heater_level`,
`cabin_temp_c`, `hvac_ambient_c`, `hvac_evap_c`, `gear`, `speed_mph`. Add
`ALTER TABLE ADD COLUMN` migration + one-time back-fill from `extra` (meta
guard), a partial index on `hvac_ac_on`, and add the keys to the `insert_reading`
skip set so they stop duplicating into `extra`. Raw bytes and rare one-offs stay
in `extra`; transitions go to the events table (Phase 2), not columns.

**Phase 2 — events table for transitions.  ✅ done** `events(ts, name, value, prev)` +
`on_time(name, t0, t1)`. The reader diffs a small watch set each poll cycle
(A/C, HVAC on, gear, locked, doors, handbrake, high beam, fog) and records changes — so on-time
is exact and independent of the 5 s sample spacing, catching sub-5 s events.

**Phase 3 — two-tier adaptive store rate (small).** 5 s while charging / A/C on /
moving; 30 s when parked-steady (≤ 60 s keeps gaps unambiguous for any future
reporting over the data). Modest, deferred.

**Phase 4 — charge report from the web app.  ❌ removed** (built, then removed 2026-08-25 at the owner's request — the report feature was not wanted; the Phase 1/2 logging work it was built on is kept) Refactor `charge_report.py` into a
pure module (`ReportParams`, `reconcile`, `build_report`, `render_markdown`) + a
thin CLI, add `POST /api/charge-report` (multiple sessions) and a dashboard panel
with from/to pickers and kWh/price/accessory fields — rendered on demand, never
persisting personal data server-side. Uses Phase-2 `on_time` + Phase-1 columns.

**Phase 5 — configurable retention (last, guarded).** Gitignored `retention.json`
(`keep_days`, default 0 = never prune — the project exists to keep SOH history).
Recommend a *downsample* middle path (thin old rows, drop bulky per-cell data,
keep the degradation trend) over hard delete, behind a typed-confirmation red
warning. Pruning runs in the reader's own connection, off the hot path; never
auto-VACUUM.

**First sprint:** Phase 1 → 2 done 2026-08-25 (columns + events kept); Phase 4 (web charge report) built then removed at the owner's request. Remaining: Phase 3 (adaptive rate), Phase 5 (retention).

---

## Open: capture a real drive (2026-09-02)

Almost everything decoded so far was captured with the car parked, often in a
driveway with the A/C running. That has left a set of questions that only
motion can answer, and it leaves the simulator's drive behaviour asserted
rather than measured.

What a single logged drive would resolve:

- **The group-05 current scale above ~32 A.** `s16 ÷ 1024` saturates at
  ±32.0 A; this car draws roughly 78 A at gentle cruise and over 200 A at
  power. Every observation behind that scale was taken under 9 A. Capturing
  group 05 alongside group 01's `s32` sensors under real load settles it.
  See the note in `docs/SIGNALS.md` under group 05.
- **Throttle `0x180` and brake `0x292` scales**, both still tentative. The
  `pedals` preset exists in `calibrate_input.py` and has never been run.
- **Regen** — magnitude and shape, and whether D and Eco differ on the bus.
- **Available power `0x260` and torque `0x1D5`**, observed in February and
  never decoded.
- **Simulator calibration** — acceleration draw, regen curve, pack
  temperature rise under sustained load, voltage sag versus current. These
  are currently guesses in `simulator/model.py` and are labelled as such.
- **The session recorder's live path** (`record_session.py`), which has been
  exercised only against a simulated transport, never real hardware.

A field protocol with ranked routines, a suggested route, a pre-flight
checklist and what each capture proves lives in `research/` (local only,
gitignored — it describes the owner's own car and driving).

Safety: the dashboard is a passenger's tool. Anything above walking pace
needs a second person running the laptop, and acceleration or braking runs
belong in an empty lot, not in traffic.


## Extended CAN inputs — research done 2026-09-08, walks pending

A research pass (memo in `research/`, gitignored, with verbatim byte
definitions and a walker plan per signal) found that most of the "what is the
car doing" set is already broadcast on **Car-CAN**, reachable with the
adapter as it is, and walkable in a parked car with `calibrate_input.py`:

- throttle `0x180` (byte 5, × 0.5 %), brake pedal `0x292` (byte 6) with a
  **12 V voltage in byte 3**, applied regen torque `0x1D5`, target braking
  force `0x1CB`, ZE0-only brake pressures `0x1CA`, motor power in `0x260`
  (0.05 kW, with two limit fields matching the 53 kW / 5 kW the project has
  seen), steering angle `0x002` (÷ 10 °, signed; three independent sources),
  climate kW / aux / eco in `0x510`;
- the DBC places the fan / vent / intake / setpoint frames `0x54A` / `0x54B`
  on Car-CAN too, contradicting SIGNALS.md's "needs the re-pinned cable" —
  one `ATCRA 54B` capture settles it.

Needs the EV-CAN cable: the 10 ms set (`0x1DB` pack current and voltage,
`0x1DA` torque and rpm, `0x1D4` torque request — the load behind a cell sag
in playback — `0x1DC` limits) and the ZE0 charger frames; with the cable in,
every Car-CAN body signal and the HVAC amp go quiet. Not found anywhere: a
Leaf yaw-rate frame, 12 V current (the VCM answers this project NRC `0x80`;
parked under the read-only rule), a ZE0 DC-DC status. Suggested order: the
`pedals` walk (throttle, brake, 12 V), a `steer` walk, `0x260` power against
the LBC's own, `0x54B`, then `0x1D5` / `0x1CB` during a regen coast in an
empty lot with a passenger on the laptop.


## Timing architecture — as the transports speed up (2026-09-09)

A native CAN adapter and an MQTT bridge turn a 3 s cycle into a stream, and
the store decimates it. The timeline is only as honest as its timestamps, so
before the decimation is trusted:

- **Acquisition time per item, stored** (`item_ts`) next to `item_age`, so a
  record that mixes a 0.1 s-old current with a 20 s-old cell set says so in
  the database, and playback can show it per tile.
- **Two clocks, both kept**: the source's frame timestamp (bridge or driver)
  and the laptop's receive time, with the per-session offset published, never
  applied to stored values.
- **Peak-preserving decimation**: per-row min / max / time-of-max for the few
  signals where a peak matters (pack current, power, voltage, lowest cell), so
  the strip draws the envelope and the auto-pull detector sees the true peak
  at 5 s cadence.
- **High-rate samples** for the ids in the ring buffer, flushed on a flag,
  scrubbable in playback when zoomed in.
- **`ts_source` on every row**, and a `--timing` mode in the bench tool so the
  numbers the docs quote are measured, not modelled.

Authority once written: `docs/TIMING.md`. Design detail is in the sprint plan
in `research/` (local) until it lands.

**Landed 2026-09-09** (`docs/TIMING.md`): `item_ts` per item, the two clocks
with `clock_offset_s` on the session, `ts_source` on every row, the `peak`
envelope between rows (used by the pull detector), the `--timing` self-test.
Test-verified in-process and on replay; no native adapter or bridge measured
yet. **Still open:** the high-rate `samples` table, and the strip drawing the
envelope.

## Capture is a pillar (2026-09-09)

The capture routine is a module with several front ends, and **none of them
needs the laptop in the car**: the dashboard reader, a headless reader, a Pi
bridge streaming over MQTT, the same bridge logging to its SD card with no
network, a plain SocketCAN log, and the simulator and replay. Every front end
writes the same two formats — the replay fixture for frames, `readings` rows
for decoded values — with provenance on every record, so anything captured
anywhere plays back on the timeline with its flags and pulls, and feeds the
same tests. `record_session.py --from-mqtt` is the first non-laptop path;
`--from-candump`, a bridge-side log, and flags made without a browser follow.

## Several adapters at once, and where a value comes from (2026-09-09)

Car-CAN and EV-CAN are two networks, so two adapters of any kind, at once.
The design: items carry a `bus`, the reader holds one transport per bus and
polls the buses **concurrently**, each with its own liveness, sleep detection
and reconnect; the record lists every adapter and keeps the old single
`adapter_*` keys filled from the Car bus so nothing downstream breaks.

Some values are reachable two ways — pack voltage and current from LBC
group 01 over Car-CAN and from `0x1DB` on EV-CAN, SOC from group 01 and
`0x55B`, gear from `0x421` and `0x11A`. The rule: **every source writes its
own key, the registry names the canonical one and its sources, and a generic
resolver picks** — a user pin first, then verified over tentative, then
freshness, then rate — and stamps `<key>_src` on the record. When two fresh
sources disagree beyond a declared tolerance the record says so and an event
is written: the "group 05 against group 01" check of February, automated.
The Leaf's current fusion in `apply_policy` becomes a special case of this.
A second reader into its own database stays the first step for verifying
EV-CAN decoders before they join the profile.

Lands as one reader lane together with the timing architecture above, after
the CAN and MQTT transports integrate. Design detail in the sprint plan in
`research/` until then.

**Landed 2026-09-09** (`docs/ARCHITECTURE.md` "Several adapters" and
"Provenance"): `bus` / `BUSES`, the `adapters` list and `HAKAKE_ADAPTERS`,
concurrent per-bus polling with per-bus reconnect, `adapters` / `bus_alive`
in the record and `sessions.adapters`, one chip per bus, the generic
resolver with `sources` / `tolerance`, the `source_disagree` event, the Leaf's
`current_a` example. Test-verified with fake transports. **Still open:** two
real adapters on the car; the EV-CAN decoders that would give `pack_v` /
`soc` / `gear` their second source; the strip showing where sources diverged;
the raw source currents as first-class columns.

## To do: a "wide" display mode (2026-09-09)

The tile grid is fixed at the column count `COLS` in `web/static/tilestudio.js`
inside a page capped at 1400 px (`index.html`), which suits a laptop in the
passenger seat and turns a wide monitor or a dashboard-mounted screen into a
narrow strip with a scroll. Wanted: a **wide mode** the owner can switch on
(header toggle, remembered like the timeline dock; `?wide=1` for a link) that
lifts the page cap and lets the grid run to more columns — a second layout,
saved separately in `web/layouts.json` so the laptop layout is not disturbed —
with the built-in tiles allowed to take the width they draw best at (the cell
grid and the 3D pack side by side, the timeline strip full width, playback and
the module pane without the fold). gridstack already supports a column count
per breakpoint (`columnOpts`), so the mechanics are a responsive column set
plus a per-mode layout name; the work is deciding which tiles grow and how
their art scales. Nothing in the reader or the API changes.

## To do: stored values are the reported values; derivations are stamped (2026-09-09)

Audit of 2026-09-09: nothing between the adapter and the database smooths for
readability — the charts' `/api/history` averages per time bucket **on read**,
playback keeps the last real row per bucket, and the page draws what the API
returns. One thing is not raw: the Leaf's `apply_policy` rewrites `current_a`
before storage (the group-05 / sensor-2 fusion with a learned offset, the
user's zero calibration, the positive-while-discharging clamp) and derives
`power_kw` from it, so the `current_a` column holds a *derived* value. The raw
readings (`current_raw_a`, `hv_current1_a`, `hv_current2_a`, `g05_current_a`,
the learned offset) are kept in the row's `extra` JSON — nothing is lost, but
they are second-class. The rule going forward, folded into the provenance
lane: **every source's value is stored as reported, as a first-class column
where it has a trend; a canonical key that is derived says so** (`<key>_src`
naming the policy, the inputs kept), and any smoothing for readability lives
in the page or the read-side API, never in a stored value. **Done the same
day** for the Leaf's current: `apply_policy` now derives `current_adj_a` /
`power_adj_kw` (+ `current_adj_src`) and leaves `current_a` / `power_kw` as
reported; the power tile shows the adjusted value and its EMA is a display-only
setting in the tile's ⋯ menu (off / 3 / 5 / 10); `tests/test_policy_raw.py`
holds every profile to the rule. Still to do in the provenance lane: the raw
source currents (`hv_current1_a`, `hv_current2_a`, `g05_current_a`) as
first-class columns.

## Follow-up: do we stamp a value when we asked for it, or when it arrived? (2026-09-10)

The owner, watching a pull in playback: the current meter rises, and the cell
colours show the sag a frame *later*. The question behind it is whether the
cell voltages should carry a timing offset, given that they cost a
request-and-answer while the current does not.

**What the code does today.** `Reader.stamp()` records a UDS item's `item_ts`
**the moment its answer returns**, and `docs/TIMING.md` says so. A passive item
on a transport with a frame table is stamped by its newest frame's *arrival*,
which is better. The stored row's own `ts` is the moment the **cycle started**,
so neither value sits at its row's timestamp. Measured over the owner's own USB
rows: the current read finishes about 0.18 s into a cycle and the cell read
about 0.51 s, the two being adjacent in the poll order, so the cells are stamped
a median **0.33 s** after the current within one row. Over BLE, before the
separation-time work, the cell read alone was 1.18 s.

**Why return time is the wrong end for a multi-frame read.** The LBC fixes the
payload before it transmits it; the 29 frames that follow are transport, not
measurement. So the sample instant is at or before the *request*, and stamping
at return pushes cell data systematically late by the whole read duration.

**The offset is already recoverable, and no capture changes are needed.** Both
`item_ts_epoch[item]` and `timing[item]` are stored in every row's `extra`, so
the request instant is `item_ts_epoch − timing`. Acquisition is therefore
already an interval; it is simply implicit and undocumented as one.

**What to do, in order.**

1. Make the interval explicit — record the request instant per item rather than
   leaving it to arithmetic — and say in `docs/TIMING.md` which end a consumer
   should use for what, and why.
2. Let playback align a value at its interval start instead of the row's
   timestamp, as a stated choice rather than a silent correction. **Never shift
   a stored timestamp**: the same rule as stored values being the reported
   values, and the same rule the power tile's smoothing follows.
3. Measure the real end-to-end lag before trusting any constant. There is a
   clean way: the cells sum to a pack voltage (`pack_v_cells`) and group 01
   carries its own, read fast. Cross-correlating the two through a logged pull
   measures the lag including the LBC's internal scan latency, which nothing
   outside the car can observe directly. That is the technique the project
   already used for group 05 against group 01. Label the result MEASURED, and
   only then consider a calibrated offset.

**Keep the three causes apart.** Sampling cadence (the cells are read once a
cycle at best) is the largest term and is not a timestamp problem; the
stamp-at-return error is the second; the ECU's own scan latency is the third
and is unknown. The cell log already addresses the first, and the separation
-time work cut the second by two thirds over USB.

## Follow-up: three places the code should probably move, not the doc (2026-09-10)

The 2026-09-10 truth audit aligned the documentation with the code everywhere
except three, where the documented behaviour is the better one and the fix
belongs in the code. The docs now describe what actually happens; these are the
changes that would let them describe what was intended.

- **The legend does not honour `invert`.** A pack mode with `invert: true`
  (temperatures, where hot should read red) flips the bodies but not the
  gradient bar, so its legend reads backwards. No shipped mode inverts, so this
  is latent until another pack arrives. `web/static/pack3d.js`.
- **The `f` key only flags in playback.** Its handler returns early when not in
  playback, but the header button works live — and marking a pull as it happens
  is the use case the key exists for. `web/templates/index.html`.
- **An auto-detected pull is drawn at its run's first row.** `Store.pulls()`
  already returns the true peak and its time (`peak_a`, `t_peak`), computed from
  the per-row envelope, and the strip and the jump list both ignore them. Using
  them is what makes the peak-preserving decimation visible, and it bears on the
  question of when a value is stamped (see the follow-up above).

## Follow-up: give each cell pair its own resistance (2026-09-10)

Building the `pulls` scenario from the 2026-09-09 drive showed the gap. In the
car, the spread between the highest and lowest pair grows with the load —
across 207 rows it fits `spread_mV = 32 + 1.06 × |I|`, so 35 mV parked becomes
274 mV at −228 A — because each pair has its own internal resistance and the
weak ones sag further. `LeafModel.cells()` has one `internal_resistance_ohm`
for the whole pack and one `cell_spread_mv` knob, so every pair sags by the
same amount and the spread never moves.

The scenario works around it by stepping `cell_spread_mv` down its timeline,
which reproduces the *magnitude* honestly and says so, but not the *pattern*:
the pairs that go reddest are the seed's fixed shape rather than the pack's
weak modules. In the real stretch those were pairs 66, 70, 72 and 76 (about
100 mV below the mean sag) with pairs 1, 48, 53–56 and 92 sagging least.

The change: a per-pair resistance multiplier, normalised around 1.0, so that
`cells()` computes each pair's sag as `I × R_pair` instead of a shared mean
plus a fixed offset. `cell_spread_mv` then describes the *resting* spread only,
and the load-dependent part emerges. A profile could ship a measured pattern —
the drive above is enough to derive one for this pack — which would make the
3D tile show the right modules going red, not just the right number of them.

## The Leaf's own trouble codes — a car-side sprint (2026-09-10)

Deferred by the owner until he is in the car, and the research has already done the hard
part. The Leaf answers no standard OBD mode, so its codes come from UDS
**`19 02 0E` sent to the LBC at `0x79B`** — ReadDTCInformation, reportDTCByStatusMask — with
the reply on `0x7BB` as `59 02 <mask>` and then four bytes per code, a three-byte DTC and a
status byte. The request appears as a literal in `dalathegreat/Battery-Emulator`'s Leaf
driver, and **no DiagnosticSessionControl precedes it**, which is what makes the feature
possible here at all: this project will not send `0x10`.

What the sprint does, in the car, with the reader paused:

1. Confirm `19 02 0E` answers on a whole ZE0 rather than the bench LBC that source usually
   drives, and record the NRC if it does not — a `0x11` is as much a result as an answer.
2. Ask the VCM and the HVAC amp the same question; each is a separate conversation.
3. Capture **`19 02 FF` once, deliberately**. The wide mask is noisy — one logged capture
   returned 149 entries of which one had actually failed — but it is a read-only enumeration
   of every code the LBC knows, which is exactly the vehicle-specific scope list
   `docs/DTC_DICTIONARY.md` tells a user's agent to build from. Keep it as a fixture.
4. Then the code: a `dtc` item kind, a decoder beside the Lancer's `_dtc_codes()`, and the
   whitelist change — `0x19` in all four enforcement points (`CanFacade.send`,
   `LocalSource.send_uds`, `MqttSource.send_uds`, the bridge's `_run_uds`) behind the
   documented review `SECURITY.md` requires.

**One thing to write into `SECURITY.md` whether or not the sprint happens:** `14 FF FF FF`
is ClearDiagnosticInformation, it is a write, and it sits four bytes from the frame this
sprint intends to send. It deserves naming in the forbidden list the way mode `04` already
is, rather than resting on the general rule.
