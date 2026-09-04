# Leaf OBD Dashboard — Improvement Plan (2026-08-24)

> **Status 2026-08-24 (evening):** first sprint complete — A1 ✅ A2 ✅ A3 ✅ A4 ✅ B1 ✅ B2 ✅ D2 ✅ D3 ✅ D4 ✅ (P1–P10 all addressed). Later the same evening: sqlite thread-safety crash fixed (reader is a subprocess), Car-CAN passive signals + HVAC amp decoded, Vehicle / Tires / Climate tiles added, cycle time cut from ~28 s to ≤8 s, then to ~2 s with the tile-driven scheduler (C3 done in spirit); dynamic tiles menu; open-source docs + local git repo; Tile Studio (signal registry, per-tile menus, 12 renderers, 7 colour scales, user tiles) and the ADDING_SIGNALS routine. Since then: HVAC setpoint/fan calibration ✅ (walks 2026-08-24), N/Eco gear confirmation ✅ (all five `0x421` values live), drag-resize handles ✅ (gridstack). Next up: Phase 3 adaptive store rate, Phase 5 retention config, B3 cell-rank memory, B5/B6 12 V + insulation cards, C4 alerts, C1 coulomb counting, pedals walk (throttle `0x180` / brake `0x292` scales). **2026-08-28:** vehicle-profile seam cut (`vehicles/` package, `--vehicle` flag, contract in `vehicles/__init__.py`); first non-Leaf profile `lancer_2009` (standard mode-01 PIDs) decoding a live idle capture, 87 tests. Same day: Lancer DTC readout added (MIL lamp + stored/pending/trans codes, modes 01/03/07, read-only), 89 tests. **2026-09-02:** replay mode — `--adapter replay` drives the whole stack (reader, scheduler, transport, decoders, store, API, page) from a recorded session fixture made by `record_session.py`, so a profile can be written and reviewed with no car; `docs/REPLAY.md`. **2026-09-03:** the simulator — `--adapter sim` runs the same stack against a running model (`simulator/`, `hakake_sim.py`) with a provenance-labelled load table, one power identity wall → charger → loads → pack, a 20-row couplings audit, the ZE0 push-button start, a control API on `127.0.0.1:8099` and the cockpit at `/sim`; `docs/SIMULATOR.md` and `docs/SIMULATOR_CONTRACT.md`. Same day: the per-tile ⋯ menu portalled out of the card (toggle, Escape, Done) and the tire art scaled by the card. Same day: the USB transport measured and fixed — a 41 ms poll tick and a 50 ms post-prompt sleep were costing ~91 ms on every command; a blocking read to the prompt and `ATBRD` negotiation to 115200 cut a command round-trip from ~107 ms to ~5–9 ms, and `SPEED` makes the scheduler's cost model transport-aware. **730 tests.**

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
B6. **Insulation resistance trend** — plain sparkline + alert threshold (< 500 kΩ). Safety signal we already read but never display over time.
B7. **Charge-session panel** — when `discharging == False` and current > 2 A, open a session: start SOC, kW curve, kWh delivered (∫P dt), estimated time-to-target. Close on current → 0.

### Phase C — Novel features

C1. **Empirical capacity via coulomb counting** — integrate group-01 current over a drive or a charge (∫I dt) and divide by ΔSOC. Gives a *measured* Ah independent of the BMS's own estimate; compare the two over time. Nobody's hobby dashboard does this because it needs the persistent store (A1) and a fast current loop (group 01 only).
C2. **Per-cell DC internal-resistance map** — capture cell voltages at two known currents (e.g. heater off → on, ~10 A step, or accessory load) and compute ΔV/ΔI per pair. Weak pairs show high IR long before they show low resting voltage. Requires a "snapshot mode" that reads 2102 immediately before/after a load step. Repeat quarterly; plot IR vs. cell index vs. date.
C3. **Drive-mode adaptive polling** — interleave a 200 ms `ATCRA 174` / `ATMA` on Car-CAN to read gear. In P/N: slow full reads (all groups, 30 s). In D/R: group-01-only power loop at max rate, plus 0x284/0x285 for speed → live Wh/mile and per-trip energy log. Turns the dashboard into a trip computer.
C4. **Weak-cell early-warning alerts** — rules over the SQLite store: spread > 50 mV at rest, any pair > 3σ below mean for 3 consecutive full reads, insulation < 500 kΩ, 12 V < 12.0 V at rest. Deliver via macOS `osascript` notification and/or ntfy.sh push (phone). Add a `/api/alerts` feed and a bell in the header.
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
