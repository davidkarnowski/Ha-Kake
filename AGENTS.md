<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# AGENTS.md — orientation for an AI agent

Read this to get your bearings, then go to the authority for whatever you are
actually doing. This file is deliberately short.

**`CLAUDE.md` is the working guide** — conventions, branching, commit style,
the privacy sweep, the progress-log rule. It is not repeated here. Read it
before you change anything.

## What this is

Ha-Kake is a **read-only** OBD-II telemetry dashboard — read-only meaning every service it sends is a read (UDS `0x21`, OBD modes `01`/`03`/`07`, ELM327 monitor mode), never that it stays off the bus: it transmits requests like any scan tool. See `SECURITY.md`. One process
(`web/reader.py`) talks to the car through an ELM327 adapter; Flask serves the
page; SQLite keeps every reading forever. It was developed on a 2012 Nissan
Leaf and is now vehicle-agnostic: everything car-specific lives in
`vehicles/<profile>.py`, and `vehicles/lancer_2009.py` (2009 Mitsubishi Lancer,
standard SAE J1979) is the proof.

Every decode in this repository was verified in a real car. Do not add one that
was not.

## Repo map

| Path | Role |
|---|---|
| `elm327.py` | transports: BLE, USB, replay, sim; adapter detect, ECU targeting, passive capture |
| `vehicles/` | one module per car — items, targets, tiles, signal registry, `decode()`. `__init__.py` is the contract *and* its validator |
| `leaf_decoders.py` | the Leaf's decoders, plus generic ISO-TP reassembly |
| `signals.py` | vehicle-independent registry machinery: colour scales, renderer list, resolvers |
| `web/reader.py` | the only process that talks to the car — tile-driven scheduler, reconnect, pause |
| `web/store.py` | SQLite time series; schema/insert/history/daily built from the profile's `HISTORY_COLS` |
| `web/app.py` | Flask dashboard + API; supervises the reader subprocess |
| `web/static/tilestudio.js` | Tile Studio: per-tile menus, add-tile, renderers, drag/resize |
| `simulator/`, `hakake_sim.py` | the simulated car: model, knobs, scenarios, encoder; rig, control API, history generator |
| `record_session.py` | record a drive, or derive a fixture from raw captures |
| `tests/` | pytest, no hardware; `tests/fixtures/` holds real captured frames |
| `docs/` | the documentation set (below) |
| `research/` | gitignored: local captures, notes, agent progress logs. Never published |
| `legacy/` | superseded one-off scripts. Do not resurrect their transports |

## Invariants that bite

These are not style preferences. Each one has cost a debugging session.

1. **Read-only, absolutely.** The reader sends UDS `0x21` and ELM327 monitor
   mode only; console probes also use `0x22`/`0x1A`/mode `09`. No control,
   write, routine or security-access service exists anywhere in the project,
   and none may be added without a documented safety review. `SECURITY.md`.
2. **`ATCAF0` for passive sniffing, `ATCAF1` for UDS.** Mixing them produces
   `DATA ERROR`. This cost a whole session in February.
3. **Never share a `sqlite3` connection across threads** — it segfaults. Flask
   uses a thread-local `Store`; the reader is a separate process.
4. **Temperatures always emit °C *and* °F** (`*_c`, `*_f`). `validate_profile()`
   rejects a `°F` signal without its `alt`/`alt_unit: "°C"` twin.
5. **Negative current means discharge.** Power follows the same sign.
6. **Nothing outside `vehicles/` and `leaf_decoders.py` may hardcode a
   vehicle.** A change that breaks that seam is wrong even if the tests pass.
7. **`decode()` returns `alive` as a tri-state**: `True` answered, `False`
   asked and silent, `None` no primary item was polled this cycle. `False`
   where `None` belongs makes a healthy car look asleep.
8. **`docs/SIGNALS.md` is the authority on bytes**, and a decoder change
   without a `SIGNALS.md` change in the same commit is incomplete.
9. **`WORKLOG.md` is append-only.** Never edit an old entry.

## Running everything with no car

You do not need hardware for any of this, and there is no excuse for shipping
an unverified change because "I could not test it".

```bash
source venv/bin/activate

# Replay: the whole real stack against a recorded fixture
python web/app.py --adapter replay                          # the Leaf
python web/app.py --adapter replay --vehicle lancer_2009    # the Lancer
python web/app.py --adapter replay --speed 10               # 10x the clock

# Simulator: a running model you can drive, plus the cockpit and control API
python web/app.py --adapter sim                             # :5000, /sim, control :8099
python web/app.py --adapter sim --scenario drive --seed 1
python web/app.py --adapter sim --no-sim-control            # opt out of the API

# Canned JSON, no reader at all — the fastest way to look at the page
python web/app.py --demo                                    # serves docs/demo/

# Months of synthetic history in seconds, for working on the charts
python hakake_sim.py --generate --days 180 --out /tmp/ui.db
python web/app.py --db /tmp/ui.db --no-reader

# The reader alone, same transports
python web/reader.py --adapter replay --vehicle lancer_2009
```

`--adapter replay` and `--adapter sim` each write to their own throwaway
database and never touch the real store. Replay records carry `replay: true`;
simulated ones carry `simulated: true`. Neither is evidence about a real car —
say so in any report you write.

**The control API** (loopback, no auth, JSON, `Access-Control-Allow-Origin: *`)
is the agent-facing surface of the simulator:

```bash
curl -s localhost:8099/sim/schema      # every knob: type, unit, range, default, label
curl -s localhost:8099/sim/state       # the model's own state
curl -s localhost:8099/sim/record      # the same in the dashboard's vocabulary (501 if unsupported)
curl -s -X POST localhost:8099/sim/knobs -H 'content-type: application/json' \
     -d '{"soc": 15, "fault.cell_degraded": true}'
curl -s -X POST localhost:8099/sim/step -H 'content-type: application/json' -d '{"dt": 30}'
```

Discover knobs from `/sim/schema` rather than hardcoding a list; an unknown
knob is a 400 with near matches, and knobs apply all-or-nothing. Optional calls
(`/sim/record`, `/sim/power`, clearing a scenario) answer 501/400 on a core
that lacks them — feature-detect, never guess. Full surface:
`docs/SIMULATOR.md`, contract in `docs/SIMULATOR_CONTRACT.md`.

## How to verify

```bash
pytest -q                          # 730 passing as of 2026-09-03, ~2 min, no hardware
python vehicles/__init__.py        # lint every vehicle profile against the contract
python scripts/privacy_sweep.py --log 50   # must print "privacy sweep OK" before any push
```

The privacy sweep is non-negotiable before a push (the pre-push hook runs it
too). It refuses home paths, device UUIDs, VINs, secrets and session URLs in
tracked files. Machine specifics go in `config.local.json` (gitignored).

## Progress logs are mandatory for delegated work

`CLAUDE.md` §3b: any task run by a sub-agent keeps an append-only markdown log
at `research/agent-logs/<task-slug>-<YYYYMMDD>.md` — the brief in one
self-contained paragraph, the file-ownership list, a timestamped entry at every
milestone with done/pending per item, test status, and a `NEXT:` line kept
current. Write the first entry **before** touching anything. On resume, read
the log, then `git diff --stat`, then continue from `NEXT:`. Logs stay in
`research/`; they never move into the repo.

## Where the authority lives

| Question | Authority |
|---|---|
| Conventions, branching, commits, the privacy sweep, progress logs | `CLAUDE.md` |
| What does byte N mean, and how sure are we? | `docs/SIGNALS.md` — then `leaf_decoders.py` |
| Processes, scheduler, data model, Tile Studio | `docs/ARCHITECTURE.md` |
| What must a vehicle profile provide? | `vehicles/__init__.py` docstring + `validate_profile()` |
| How do I add a car? | `docs/ADDING_A_VEHICLE.md` |
| How do I add one signal to a car that already works? | `docs/ADDING_SIGNALS.md` |
| How was a signal found in the first place? | `docs/reverse-engineering/`, then `WORKLOG.md` (search the CAN id) |
| Running with no car | `docs/REPLAY.md`, `docs/SIMULATOR.md` |
| What a simulator core must provide | `docs/SIMULATOR_CONTRACT.md` |
| What may be sent to a car | `SECURITY.md` |
| Adapter will not talk | `elm327.py` header comments, README "Hardware" |
| Roadmap and status | `docs/ROADMAP.md` |
| Submitting a change, licensing of contributions | `CONTRIBUTING.md` |

## Two habits worth having here

**Say what you did not verify.** Confidence tiers are a first-class idea in
this project — `docs/SIGNALS.md` marks decodes verified or tentative, and the
simulator's load table labels every row MEASURED or ASSERTED. A green test run
against a simulator is evidence about the code, not about a car. Write reports
the same way.

**Update the doc in the same commit as the change.** Status tables, counts and
the docs for the subsystem you touched are part of the change, not follow-up
work.
