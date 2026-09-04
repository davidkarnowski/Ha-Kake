<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Adding a vehicle

How to make this dashboard work on your car.

Everything car-specific in Ha-Kake lives in one module: `vehicles/<name>.py`.
The reader, the scheduler, the SQLite store, the API, Tile Studio and the
whole page are vehicle-agnostic, and a profile is the only thing you write.
Two ship today — `vehicles/leaf_ze0.py` (2012 Nissan Leaf, largely reverse
engineered) and `vehicles/lancer_2009.py` (2009 Mitsubishi Lancer, standard
SAE J1979, 255 lines including the decoder).

This document is about adding a **car**. Adding one more *signal* to a car
that already has a profile is a different, shorter routine:
[`ADDING_SIGNALS.md`](ADDING_SIGNALS.md).

---

## 1. Start here: which kind of job is this?

Ask one question first, because it decides whether this is an afternoon or a
project.

**Does your car answer standard OBD-II mode 01?**

Almost every petrol car sold in the US since 1996 and in the EU since 2001
does. Plug the adapter in, start the engine, and ask it for engine RPM from any
ELM327 terminal:

```
ATZ
ATSP6
ATSH 7E0
010C            -> 7E8 04 41 0C 0B B8      = it answered
                -> NO DATA                 = it did not
```

The quickest real test inside this repo is to point the Lancer profile at your
car for ten seconds and see what comes back — the mode-01 PIDs it asks for are
the standard ones:

```bash
python record_session.py --vehicle lancer_2009 --adapter usb --seconds 10 \
    --out /tmp/probe.json
#   frame 1 at t=0.0s — N UDS, 0 monitored ids      <- it answers; N is how many PIDs replied
#   nothing answered at t=0.0s (car asleep?)        <- it does not
```

| Answer | What you are in for | Read |
|---|---|---|
| `41 0C …` comes back | **An afternoon.** The PIDs are a published standard; you are choosing which of them to poll and how to render them. Copy `vehicles/lancer_2009.py`, change the PID list, done. | this document |
| `NO DATA`, or you want anything the standard does not carry — doors, locks, state of health, cell voltages, HVAC, TPMS | **Reverse engineering.** Nothing is published; you find it yourself, one physical input at a time. | [`reverse-engineering/`](reverse-engineering/00-index.md) first, then come back here |

Most interesting profiles are both: a standard mode-01 core that works on day
one, plus manufacturer-specific signals added later, one at a time, each with
a capture behind it. Ship the boring half first.

**A third case:** the data you want is on a bus the OBD-II port does not
expose. On the Leaf, all real-time EV-CAN traffic needs a re-pinned cable
(pins 13/12) and is out of reach of a stock connector. Find that out early —
chapter [01](reverse-engineering/01-getting-on-the-bus.md) tells you how.

---

## 2. The contract

`vehicles/__init__.py` is the authority: its module docstring is the contract,
and `validate_profile()` in the same file *enforces* it. Everything below is
that contract explained, but if the two ever disagree, the code is right.

Run the checker at any point — it needs no car, no adapter and no test suite:

```bash
python vehicles/__init__.py                  # every profile in vehicles/
python vehicles/__init__.py civic_2006       # just yours
```

It prints one line per profile and exits non-zero on a problem:

```
lancer_2009: OK (2009 Mitsubishi Lancer ES, 19 items, 20 signals, 18 history columns)
leaf_ze0: OK (2012 Nissan Leaf (ZE0), 18 items, 57 signals, 33 history columns)
```

It returns the *whole* list of problems rather than raising on the first, so
a half-written profile tells you everything left to do in one run.

### 2.1 Required attributes

Fourteen. `validate_profile()` refuses a profile missing any of them, and
`get_vehicle()` refuses to bind an invalid profile at all.

| Attribute | Shape | Notes |
|---|---|---|
| `NAME` | `str` | Must equal the module filename — `"civic_2006"` in `vehicles/civic_2006.py`. Checked. |
| `TITLE` | `str` | Human name in the page chrome: `"2009 Mitsubishi Lancer ES"`. Non-empty. |
| `TARGETS` | `dict` kind → `(tx, rx)` or `None` | The ECUs you talk to. `("7E0", "7E8")` is a request/response header pair for a request-response (UDS-style) kind; `None` marks a **passive** kind, captured by monitoring the bus (`ATCAF0` + `ATCRA` + `ATMA`). Non-empty. |
| `KIND_ORDER` | `tuple` of kinds | Poll order within one cycle. The scheduler sorts a cycle's items by this so the adapter switches ECU as few times as possible — each switch costs real milliseconds. Every kind used by an item must appear here *and* in `TARGETS`. |
| `ITEMS` | `dict` item id → spec | Everything pollable. See below. Non-empty. |
| `TILES` | `list` of `{id, name, items}` | Built-in dashboard tiles — hand-written HTML/SVG partials. **May be empty**, and for a new car it should be: the shipped built-ins are Leaf art. Every `items` entry must name a real item, and every tile needs a `DEFAULT_SPAN`. |
| `DEFAULT_SPAN` | `dict` tile id → int | Grid columns (the dashboard grid is 12 wide) for built-in tiles only. `{}` when `TILES` is empty. |
| `DEFAULT_TILES` | `list` of dicts | The out-of-the-box layout. Each entry needs an `id`; an entry that is not a built-in tile id must carry `signal` naming a key in `SIGNALS`. Other honoured fields: `kind` (`"signal"`), `type` (a renderer), `enabled`, `span`, `title`, `opts`, `x`, `y`, `h`. |
| `ITEM_KEYS` | `dict` item → tuple of record keys | Which cached keys to drop when a tile stops needing that item. Keys must be strings; items must exist. |
| `WATCH` | `tuple` of record keys | Values logged to the `events` table on every change (gear, lock state, MIL…). |
| `FAST_ONLY` | `set` of item ids | What `--fast` polls — the one or two items worth a tight loop. |
| `SIGNALS` | `dict` key → registry entry | What the dashboard can display. See §2.3. |
| `configure(elm)` | `async def` | Full adapter setup for this vehicle. Must be a coroutine function; it is checked. |
| `decode(responses)` | `def` → `(record, alive)` | The whole decode. See §2.4. |

### 2.2 `ITEMS` — one entry per thing you poll

```python
ITEMS = {
    "pid_rpm": {"kind": "pid", "cmd": "010C", "period": 0, "timeout": 4.0,
                "label": "engine RPM"},
    "p421":    {"kind": "passive", "id": "421", "secs": 0.2, "period": 0,
                "label": "gear"},
}
```

| Field | Required | Meaning |
|---|---|---|
| `kind` | always | A key of `TARGETS`, and it must be in `KIND_ORDER`. |
| `period` | always | Seconds between refreshes. **`0` means every cycle** (the "fast lane"). Numeric and ≥ 0. |
| `label` | always | Non-empty; it appears in logs and diagnostics. |
| `cmd` | request/response kinds | The request string sent verbatim: `"010C"`, `"2101"`, `"03"`. |
| `id` + `secs` | passive kinds | The CAN id to filter to, and how long to listen. Both required, and validated. |
| `timeout` | optional | Seconds to wait for an answer; default 8.0. |
| `est` | optional | What one poll of this item *costs*, in seconds. See §3.4. |

Be stingy with `period: 0`. On BLE every fast item costs roughly 0.4 s of
every cycle, and a cycle is what the whole dashboard's freshness is made of.

### 2.3 `SIGNALS` — the registry the UI reads

One entry makes a value available in **Tiles ▾ → add** in every renderer
(number, ring, arc, dial, bar, thermometer, battery, line/area/bars, text,
lamp) and every colour scale, and tells the reader which item a user tile
needs. The renderers and scales themselves are vehicle-independent and live
in `signals.py`.

```python
"coolant_temp_f": {"label": "Coolant temp", "unit": "°F", "min": 60, "max": 260,
                   "dec": 0, "item": "pid_coolant", "hist": "coolant_temp_f",
                   "color": "heat", "alt": "coolant_temp_c", "alt_unit": "°C"},
```

- `item` must name a real item. That is the link the scheduler follows: turn a
  tile on and its item starts being polled.
- `kind` is `"number"` (the default), `"bool"` or `"text"`. A `number` must
  carry `unit`, `min` and `max` — validated.
- `label` is required.
- `hist` names the value's column in stored history (§2.5) — omit it and the
  signal simply cannot be graphed.
- `color` picks a scale from `signals.COLOR_SCALES`; `dec` is decimal places.
- A signal key may carry a dotted index (`"temps.0"`) to pull one element out
  of a list the decode produced.

**The °F rule is enforced.** A signal with `"unit": "°F"` must carry its
Celsius twin as `alt` plus `alt_unit: "°C"`, or the profile is invalid:

```
civic_2006: °F signal 'iat_f' must carry its °C twin as 'alt' + 'alt_unit': '°C'
            (house rule: always °C and °F)
```

So your `decode()` emits both `*_c` and `*_f` for every temperature. This is a
house convention with teeth, not a suggestion.

### 2.4 `configure()` and `decode()`

```python
async def configure(elm):
    """Full adapter setup for this vehicle."""
    await elm.send("ATZ", wait=1.5)
    for cmd in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6",
                "ATFCSH 7E0", "ATFCSD 30 00 20", "ATFCSM1"):
        await elm.send(cmd, wait=0)
```

Headers and filters per target are the reader's job — it calls `switch()`
before each item and points the adapter at that kind's `TARGETS` pair (or
`ATCAF0` for a passive kind). What belongs in `configure()` is the base init
and anything global: protocol, echo, headers, and flow control if you expect
multi-frame answers.

```python
def decode(responses):
    """{item_id: raw lines} -> (flat record, alive)"""
```

`responses` is a dict keyed by *item id*, holding the raw response lines for
the items polled this cycle — only those. The record you return is flat:
`{"rpm": 743.0, "coolant_temp_c": 88, "coolant_temp_f": 190.4}`.

**`alive` is a tri-state, and getting it wrong is the classic bug.**

| Return | Meaning | The reader does |
|---|---|---|
| `True` | the primary ECU answered | keep polling normally |
| `False` | it was asked and said nothing | probe the adapter, then decide asleep vs. disconnected |
| `None` | **no primary item was polled this cycle** | leave the previous verdict alone |

`None` is not "don't know" as a shrug — it is a positive statement that this
cycle carries no evidence either way, because the only items due were passive
or belonged to a secondary ECU. Both shipped profiles start `alive = None` and
only ever assign `True`/`False` from an actual attempt. Return `False` when
nothing was polled and the dashboard will declare a perfectly healthy car
asleep every time a slow cycle comes around.

### 2.5 Optional profile attributes

| Attribute | Shape | What it does |
|---|---|---|
| `HISTORY_COLS` | `dict` column → spec | Which record keys get **real, indexed SQLite columns** instead of riding in the `extra` JSON bag. Anything not listed here is stored forever but cannot be charted or aggregated. `web/store.py` builds the schema, the insert, `history()` and `daily_health()` from this — a profile never edits the store. |
| `EXTRA_SKIP` | `tuple` of record keys | Keys never worth putting in `extra` at all: raw dumps, lists already stored as columns. The Leaf skips `("temps", "temps_c", "temps_f", "temps_raw", "balancing", "readings")`. |
| `apply_policy(cache, calib, state)` | callable | Per-vehicle sensor policy, run every cycle after decode: fusion, calibration, sign correction. `state` is a dict the profile owns and the reader carries between cycles. The Leaf uses it to apply a zero-current offset and take current *direction* from the BMS discharge flag rather than the sensor's sign. |
| `DB_FILE` | `str` | A database file of this profile's own, in `web/`. Unset is the normal case: every profile shares `web/leaf_battery.db` and rows are separated by the `vehicle` column. |
| `LOGO` | `str` | Header wordmark art. `"leaf"` gives the leaf silhouette; anything else — including omitting it — gives a neutral dial. The mark fills with the first level-ish signal your registry declares (`soc`, then `fuel_pct`), or stays static if you declare neither. |

`HISTORY_COLS` spec keys, all optional except `kind`:

```python
HISTORY_COLS = {
    "coolant_temp_c": {"kind": "real", "hist": "coolant_temp", "round": 1,
                       "hist_f": "coolant_temp_f",
                       "daily": {"avg": "coolant_temp_c", "max": "coolant_max_c"}},
    "rpm":            {"kind": "real", "hist": "rpm", "round": 0,
                       "daily_filter": True, "daily": {"avg": "rpm", "max": "rpm_max"}},
    "mil_on":         {"kind": "bool", "hist": "mil_on", "index": "idx_readings_mil"},
}
```

- `kind` — `"real"` / `"int"` / `"bool"` / `"text"` (required). `type`
  overrides the SQL type it implies.
- `key` — the record key; defaults to the column name. May be a dotted list
  index (`"temps.0"`) or a `callable(record)` for a derived value.
- `hist` — the name this value gets in `/api/history` entries. Omit to store
  without charting. It must be unique, and a signal's `hist` must point at a
  name some column actually produces — validated.
- `round` — decimals in history (default 2). `hist_f` — also emit the °F twin
  under this name.
- `daily` — `{"avg"|"min"|"max": output name}` for `/api/health`.
- `daily_filter` — daily aggregation skips rows where this column is NULL. At
  most one column may set it.
- `index` — name of a partial index on `(ts_epoch)` where this column is 1.

Column names may not collide with the built-ins `id`, `ts`, `ts_epoch`,
`adapter`, `vehicle`, `extra`. The migration is additive and self-healing: a
new column appears in existing databases on the next start and is backfilled
from `extra`.

The `cells` table is a profile-specific extra rather than a general mechanism —
a record carrying a `cells` list of per-cell millivolts gets one row per cell,
which is what the Leaf's 96 cell pairs need. Emit no `cells` key and that table
is never touched.

### 2.6 What is *not* a profile attribute

The simulator's optional calls — `record()`, `can_record()`, `press_power()`,
`can_press_power()`, `clear_scenario()`, `lamps()`, `messages()` — belong to a
**simulator core**, not to a vehicle profile. They are defined on the model
classes in `simulator/` (`simulator/model.py` for the Leaf,
`simulator/lancer.py` for the Lancer) and surfaced by `simulator/__init__.py`;
the contract for them is [`SIMULATOR_CONTRACT.md`](SIMULATOR_CONTRACT.md).
Writing a simulated car is a separate, optional piece of work (§3.3) — you can
ship a complete, useful profile without one.

---

## 3. Building it without the car in front of you

This is the part that makes "add your car" an honest invitation rather than a
slogan. You need the car for the *captures*. You do not need it for anything
after that, and neither does whoever reviews your pull request.

### 3.1 Replay — the whole real stack against a recording

```bash
python web/app.py --adapter replay                          # the Leaf
python web/app.py --adapter replay --vehicle lancer_2009    # the Lancer
python web/app.py --adapter replay --vehicle civic_2006 --fixture my_drive.json
python web/app.py --adapter replay --speed 10               # 10x the clock
```

Replay swaps the transport and nothing else: the reader, the scheduler,
`elm327.py`'s command surface, your `configure()` and `decode()`, the store,
the API and the page are all the live ones. It writes to its own throwaway
database (`web/replay_<profile>.db`), and every record carries `replay: true`,
so a playback can never be mistaken for a reading. The default fixture for a
profile is `tests/fixtures/session_<name>.json`. Details:
[`REPLAY.md`](REPLAY.md).

### 3.2 Recording your own fixture

```bash
python record_session.py --vehicle civic_2006 --adapter usb --seconds 60
# -> tests/fixtures/session_civic_2006.json
```

`record_session.py` polls every item your profile declares, once per
`--period` (default 2 s) for `--seconds` (default 60), and writes a replay
fixture with provenance: adapter, capture time, source, notes. Take that one
file home and you can develop the rest of the profile at a desk.

`--derive` is the other route — it builds a session fixture out of raw captures
already in `tests/fixtures/`, which is how both shipped fixtures were made.

### 3.3 The simulator — a car made of arithmetic

```bash
python web/app.py --adapter sim
```

One command starts a simulated car, the dashboard on `:5000`, the cockpit page
at `/sim`, and a control API on `127.0.0.1:8099`. Unlike replay it is a
*running model*, so you can produce conditions no parked car will give you on
demand. See [`SIMULATOR.md`](SIMULATOR.md).

For a new vehicle this is optional and it comes last. A simulator core for your
car is a model class in `simulator/` registered in `simulator.VEHICLES` and
`simulator.KNOBS`; it must implement
[`SIMULATOR_CONTRACT.md`](SIMULATOR_CONTRACT.md), whose optional calls a caller
feature-detects, so a core with no `record()` simply means the cockpit draws no
vehicle tiles for that profile. Write the profile first. Write the simulator
when you are tired of driving to the car.

### 3.4 The cost model, and why a cycle is a budget

The reader polls what the enabled tiles need, and it does it inside a
per-cycle time budget (default 1.5 s of "slow lane", `--budget`).

- Items with `period: 0` run **every cycle**, unconditionally, and are not
  charged to the budget. That is the whole reason to be stingy with them.
- Everything else is round-robin by *overdue ratio* — how many multiples of its
  own period it has waited — and the most overdue items are taken until the
  budget is spent.
- The estimate the budget spends is `est` if you declared one, else
  `secs + 0.25` for a passive item, else `0.35` seconds.

So `est` is not decoration: it is what stops one 1.3-second cell-voltage read
from starving five other items in the same cycle. Time a poll over your actual
adapter, and declare it if it is not close to 0.35.

### 3.5 The sticky cache

The reader keeps one `cache` of the latest decoded value of every key, across
cycles. A tile polled every 5 minutes still shows its last value in between —
that is the point — and `/api/status` publishes `item_age` per item so the page
can say how stale something is.

That cache is why `ITEM_KEYS` exists. When a tile is switched off and its item
stops being polled, the values it produced would otherwise sit on the screen
forever, looking live. `ITEM_KEYS` says which keys to evict:

```python
ITEM_KEYS = {
    "pid_coolant": ("coolant_temp_c", "coolant_temp_f"),
    "pid_mil":     ("mil_on", "dtc_count"),
}
```

Every key an item is the sole source of belongs in its tuple. Nothing enforces
completeness — this one is on you.

### 3.6 One more trap: `get_vehicle()` mutates `SIGNALS` in place

On the first bind, `get_vehicle()` walks your `SIGNALS` and fills in the
defaults the UI relies on — `kind` defaults to `"number"`, `key` defaults to
the registry key — **writing into your dicts**. Consequences:

- Do not share one dict object between two signal entries; they would both get
  the same `key`.
- Do not treat `SIGNALS` as immutable or compare it to a literal after a bind.
- Profiles are cached per process, so this happens once, not per call.

---

## 4. Worked example: the Lancer, end to end

`vehicles/lancer_2009.py` is 255 lines and needed no reverse engineering at
all. Here is the same path for a car of your own.

**1. Capture.** `record_session.py` polls the items *a profile declares*, so
copy `lancer_2009.py` to your own name first, edit the PID list, and record
against that. It is fine for the first draft to be wrong — you are collecting
raw answers, and the decode can be fixed at a desk. Engine idling, adapter on
the port, one recording:

```bash
python record_session.py --vehicle civic_2006 --adapter usb --seconds 60 \
    --notes "idle, warm, park"
# -> tests/fixtures/session_civic_2006.json
```

The real Lancer captures are `tests/fixtures/lancer_idle_raw_20260828.json`
(mode 01 PIDs at idle) and `lancer_dtc_raw_20260828.json` (modes 01/03/07 from
both ECUs). Date in the filename; personal or noisy dumps go to `research/`,
which is gitignored.

**2. Targets and kinds.** Two ECUs, both request/response, no passive capture:

```python
TARGETS = {"pid": ("7E0", "7E8"), "pid_t": ("7E1", "7E9")}
KIND_ORDER = ("pid", "pid_t")
```

**3. Items.** One per PID, with a period chosen by how fast the value actually
moves — RPM and speed every cycle, coolant every 5 s, fuel level every minute,
barometric pressure every 5 minutes:

```python
"pid_rpm":     {"kind": "pid", "cmd": "010C", "period": 0,   "timeout": 4.0, "label": "engine RPM"},
"pid_coolant": {"kind": "pid", "cmd": "0105", "period": 5,   "timeout": 4.0, "label": "coolant temp"},
"pid_baro":    {"kind": "pid", "cmd": "0133", "period": 300, "timeout": 4.0, "label": "barometric pressure"},
```

**4. Decode.** A helper that pulls the data bytes out of one `41 <pid>`
single-frame answer, and a table of one-line decoders:

```python
_DECODERS = {
    0x05: [("coolant_temp_c", lambda b: b[0] - 40),
           ("coolant_temp_f", lambda b: _c2f(b[0] - 40))],   # both, always
    0x0C: [("rpm", lambda b: round((b[0] * 256 + b[1]) / 4.0, 1))],
    0x0D: [("speed_kmh", lambda b: b[0]),
           ("speed_mph", lambda b: round(b[0] * 0.621371, 1))],
}
```

`decode()` walks `responses`, parses each answer, sets `alive = True` on any
successful parse, `alive = alive or False` on a silent one — and leaves `alive`
at `None` if nothing was asked.

**5. Signal.** One registry entry per value you want on screen:

```python
"coolant_temp_f": {"label": "Coolant temp", "unit": "°F", "min": 60, "max": 260,
                   "dec": 0, "item": "pid_coolant", "hist": "coolant_temp_f",
                   "color": "heat", "alt": "coolant_temp_c", "alt_unit": "°C"},
```

**6. History column**, because a coolant temperature is worth graphing:

```python
"coolant_temp_c": {"kind": "real", "hist": "coolant_temp", "round": 1,
                   "hist_f": "coolant_temp_f",
                   "daily": {"avg": "coolant_temp_c", "max": "coolant_max_c"}},
```

**7. Tiles.** `TILES = []` and `DEFAULT_SPAN = {}` — no hand-built art. The
default layout is signal tiles, which work for any profile:

```python
DEFAULT_TILES = [
    {"id": "u_coolant", "kind": "signal", "signal": "coolant_temp_f",
     "type": "thermo", "enabled": True, "span": 3},
    {"id": "u_rpm",     "kind": "signal", "signal": "rpm",
     "type": "dial",   "enabled": True, "span": 3},
]
```

**8. Test.** `tests/test_vehicles.py` parametrises over `vehicles.available()`,
so dropping `civic_2006.py` into the directory is enough to get the contract,
the °F-with-°C convention and the history-column declaration tested. What that
does *not* cover is whether your arithmetic is right, so add the test that
asserts a value you saw with your own eyes against your captured fixture —
`test_lancer_decodes_real_idle_capture` and
`test_lancer_dtc_readout_from_real_capture` are the models.

**9. Run it:**

```bash
python vehicles/__init__.py civic_2006
pytest -q
python web/app.py --adapter replay --vehicle civic_2006
```

---

## 5. Read-only, still

Everything in `SECURITY.md` applies to your profile. The dashboard's reader
sends read services only; the Lancer's DTC readout uses modes 01/03/07 and
deliberately **not** mode 04, which clears codes. A profile that sends a
control, write, routine or security-access service will not be merged without a
documented safety review. If your car needs a seed/key handshake to give up a
value, the value is out of scope here.

## 6. Checklist

- [ ] `NAME` equals the filename; `TITLE` names the car
- [ ] captures in `tests/fixtures/` with the date in the filename
- [ ] every temperature emits `*_c` and `*_f`; every current is negative for discharge
- [ ] `decode()` returns `None` for `alive` when no primary item was polled
- [ ] `ITEM_KEYS` covers every key its item is the sole source of
- [ ] `est` declared for anything much slower than 0.35 s
- [ ] `HISTORY_COLS` for everything you want to graph or aggregate
- [ ] `python vehicles/__init__.py <name>` reports OK
- [ ] a test asserting an observed value from your own fixture
- [ ] `python web/app.py --adapter replay --vehicle <name>` puts real numbers on the page
- [ ] `pytest -q` green, `python scripts/privacy_sweep.py` OK
- [ ] `docs/SIGNALS.md` rows for anything you reverse engineered, with confidence and date

## 7. Where the authority lives

| Question | File |
|---|---|
| What must a profile provide? | `vehicles/__init__.py` docstring, and `validate_profile()` below it |
| The minimal worked example | `vehicles/lancer_2009.py` |
| A large, reverse-engineered example | `vehicles/leaf_ze0.py` |
| How do I find a signal nobody has documented? | [`reverse-engineering/`](reverse-engineering/00-index.md) |
| How do I add one more signal to an existing profile? | [`ADDING_SIGNALS.md`](ADDING_SIGNALS.md) |
| How do the processes fit together? | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Running with no car | [`REPLAY.md`](REPLAY.md), [`SIMULATOR.md`](SIMULATOR.md) |
| What may a profile send? | `SECURITY.md` |
| Submitting it | `CONTRIBUTING.md` |
