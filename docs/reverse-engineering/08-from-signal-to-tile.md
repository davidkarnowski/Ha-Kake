# 08 — From signal to tile

**What this chapter teaches**

- The full path a value takes, from raw ELM327 lines to a card on a dashboard and a row in SQLite.
- The six-step routine for adding an input, and where the authoritative version of it lives.
- What an *item* is, what it costs, and why the scheduler refuses to poll one nobody is looking at.
- What the *signal registry* buys you: a dozen renderers and seven colour scales from one dictionary entry.
- How a *vehicle profile* packages all of it, and where the seam is.
- How a profile declares its own history columns, and the one limitation that remains.

**What this chapter assumes:** you have a decode you believe in and a confidence label you can defend ([07](07-confidence-and-honesty.md)).

---

## The whole path, once

Here is every stage a number passes through in Ha-Kake. Nothing in this chapter is more complicated than this list.

```
  the car
     │  CAN
  ELM327 adapter
     │  ASCII lines: "7BB 10 3D 61 01 FF FF FF FF"
  elm327.py  (BleELM.send / SerialELM.send / passive_capture)
     │  {item_id: [lines]}
  VEHICLE.decode(responses)          ← vehicles/leaf_ze0.py, vehicles/lancer_2009.py
     │  one flat record: {"soc": 75.75, "pack_v": 383.5, "gear": "D", ...}
  reader cache  (self.cache.update(rec))
     │
     ├── apply_policy()  → per-vehicle sensor fusion / calibration
     ├── emit_events()   → state transitions into the events table
     │
     ├─→ battery_state.json   every cycle   →  GET /api/status  →  a tile
     └─→ SQLite readings row  every 5 s     →  GET /api/history →  a graph
```

Four things are worth noticing before the details.

**The record is flat.** `decode()` returns a plain dict of scalar keys. Not nested, not typed, not a class. Every stage downstream — the cache, the state file, the API, the registry, the store — is just moving that dict around. A new signal is a new key, and nothing in between has to learn about it.

**The cache is sticky.** `self.cache.update(rec)` merges. Items polled every 5 minutes still appear in every state file write, at their last known value, and their staleness is published separately as `item_age` so a tile can say "42 s ago" rather than pretending. This is why a slow signal does not make the dashboard flicker.

**The state file and the database have different jobs.** From `CLAUDE.md`: `STORE_PERIOD` is 5 s even though cycles run at about 2 s, because "the state file is live, the database is for trends." The state file is rewritten every cycle via a temp file and `os.replace()`, so a reader of `/api/status` never sees a half-written JSON document.

**The reader is the only process that talks to the car.** Flask runs it as a supervised subprocess. That separation was born from a crash (chapter [06](06-when-youre-wrong.md)) and kept for architecture.

---

## The six-step routine

`docs/ADDING_SIGNALS.md` is the authority and you should follow it there rather than here. This is the shape of it, so you know what you are in for:

| Step | What | Deliverable |
|---|---|---|
| 0 | Work out where the signal lives: passive Car-CAN, LBC group, another ECU's UDS group, or unreachable | a decision, possibly "stop here" |
| 1 | **Capture it with the physical input changing** | a raw JSON fixture in `tests/fixtures/`, dated |
| 2 | Decode it | a function in `leaf_decoders.py` (or your profile) |
| 3 | Pin it with a test | an assertion per transition you actually observed |
| 4 | Make the reader fetch it | an entry in the profile's `ITEMS` |
| 5 | Register it | an entry in the profile's `SIGNALS` |
| 6 | Document it | a `docs/SIGNALS.md` row with tier, sample and date, plus a `WORKLOG.md` entry |

All of it lands in **one commit**. The repo's `CLAUDE.md` puts it bluntly: "A decoder change without a SIGNALS.md change in the same commit is incomplete."

Two lines from that document are worth repeating because they are the ones people skip:

> A capture where nothing changed proves nothing.

> assert the decoded value you saw with your own eyes. One assertion per transition you observed is ideal.

Steps 1 and 3 together are what make step 6's confidence label mean something. Steps 4 and 5 are the subject of the rest of this chapter.

---

## Items: what you poll, and what it costs

An **item** is one thing the reader can ask the car for. Exactly one of:

- a UDS group read against a target ECU (`{"kind": "lbc", "cmd": "2101", ...}`)
- a passive capture of a single CAN ID (`{"kind": "passive", "id": "421", "secs": 0.2, ...}`)

Items live in the vehicle profile. Here are four real ones from `vehicles/leaf_ze0.py`:

```python
"lbc01":  {"kind": "lbc", "cmd": "2101", "period": 0,  "timeout": 10.0, "est": 0.45, "label": "battery state"},
"lbc02":  {"kind": "lbc", "cmd": "2102", "period": 20, "timeout": 15.0, "est": 1.3,  "label": "cell voltages"},
"p421":   {"kind": "passive", "id": "421", "secs": 0.2, "period": 0,   "label": "gear"},
"p355":   {"kind": "passive", "id": "355", "secs": 0.2, "period": 300, "label": "units"},
```

### period

`period` is seconds between refreshes. **`period: 0` is the fast lane** — that item runs every single cycle, unconditionally, before anything else is considered.

Everything else is scheduled by how overdue it is. `Reader.plan()` builds each cycle as: the fast lane, plus the most-overdue slow items that fit inside a time budget, ordered by `KIND_ORDER` so the adapter does as few ECU switches as possible. On the Leaf that order is `("lbc", "hvac", "passive")`.

### est, and why you should be stingy

`docs/ADDING_SIGNALS.md` states the cost model in one clause:

> be stingy — every fast item costs ~0.4 s per cycle over BLE

That is not a rule of thumb somebody invented, it is measured. `Reader.estimate()` is where the arithmetic lives:

```python
if "est" in it:
    return it["est"]
if TARGETS[it["kind"]] is None:
    return it["secs"] + 0.25       # passive: capture window plus overhead
return 0.35                        # a UDS group read
```

Items with unusual costs declare their own `est`. The 29-frame cell read is `1.3`. Group 05 is `0.6`. A passive capture costs its monitor window plus 0.25 s.

Do the sums before you set `period: 0`. Three fast items is about a second of every cycle, and the whole Leaf cycle measures 1.9–2.9 s over BLE with everything enabled. The Leaf profile keeps exactly three items in the fast lane: `lbc01` (battery state), `p421` (gear) and `p358` (turn signals). Gear and turn signals are there because a dashboard shifter or blinker that lags by three seconds is worse than not having one. Odometer runs every 15 s. Units run every 300 s, because the driver is unlikely to switch between miles and kilometres mid-drive.

### Nothing is polled unless a tile wants it

This is the part that makes the cost model livable. The reader reads `web/tiles.json` (re-reading it when its mtime changes) and computes the set of items that **enabled** tiles actually need. Built-in tiles declare their items directly in `TILES`; user signal tiles resolve theirs through the registry's `item` field.

Anything nobody is looking at is not asked for. WORKLOG entry 56 records the verification: all built-in tiles off plus a single cabin-temperature arc tile, and only `hvac10` got polled.

So the honest way to think about adding an item is not "does this cost me 0.4 s" but "does this cost 0.4 s *for the people who turn it on*". That makes it reasonable to add niche signals. It does not make it reasonable to put them in the fast lane.

### ITEM_KEYS

When an item stops being polled, its keys would otherwise sit in the sticky cache forever, growing stale in silence. `ITEM_KEYS` maps each item to the record keys that should be dropped when it is disabled:

```python
"pid_maf":  ("maf_gs",),
"pid_fuel": ("fuel_pct",),
```

Add your new keys here or they will haunt you.

---

## The signal registry: one entry, a dozen renderers

Steps 4 and 5 are separate for a reason. Step 4 tells the reader *how to get* a value. Step 5 tells the user interface *what the value means*.

`signals.py` owns the vehicle-independent machinery: the colour scales, the renderer list, and the resolvers the reader and the browser share. The entries themselves come from the active profile's `SIGNALS` dict, bound by `reader.set_vehicle()`.

One entry looks like this:

```python
"coolant_temp_f": {
    "label": "Coolant temp", "unit": "°F",
    "min": 60, "max": 260, "dec": 0,
    "item": "pid_coolant", "color": "heat",
    "alt": "coolant_temp_c", "alt_unit": "°C",
},
```

| Field | Does what |
|---|---|
| `label`, `unit`, `dec` | how it reads on the card |
| `min`, `max` | the domain for every gauge and colour scale |
| `item` | **which item must be polled for this to have a value** — this is the link back to step 4 |
| `color` | default colour scale: `soc`, `good-high`, `good-low`, `heat`, `diverge`, `mono`, `band` |
| `kind` | `number` (default), or `bool` / `text`, which the UI renders as lamp and text tiles |
| `hist` | the store column to graph, if the store keeps one |
| `alt`, `alt_unit` | the °C twin of an °F value, so the tile can show both |
| `key` | a dotted index is allowed, e.g. `temps_f.0` for the first pack temperature |

Having written that, the signal appears in **Tiles ▾ → add** with every renderer the UI implements — number, ring, arc, dial, bar, thermometer, battery, line, area, bars, text, lamp — and every colour scale, with per-tile invert and min/max overrides. No JavaScript. No template. No front-end change at all.

The Lancer's DTC readout is the proof. Adding trouble codes to a whole new vehicle needed **zero UI changes**, because a boolean signal rendered as a lamp (`mil_on`) and a text signal rendered as a text tile (`dtc_stored`) were already generic capabilities.

The registry is also what closes the loop on polling. `signals.signal_item(sig)` maps a signal key back to its item, which is how the reader knows that a user's cabin-temperature arc tile requires `hvac10` and nothing else.

---

## Vehicle profiles

A profile is one module in `vehicles/` that packages everything above for one car. `vehicles/__init__.py` carries the contract in its docstring; read that file, it is the authority. In summary a profile exports:

| Export | What |
|---|---|
| `NAME`, `TITLE` | id and human name |
| `ITEMS`, `TARGETS`, `KIND_ORDER` | what is pollable, where it lives, and in what order |
| `TILES`, `DEFAULT_SPAN`, `DEFAULT_TILES` | built-in tiles and the out-of-the-box layout |
| `SIGNALS` | the registry entries |
| `ITEM_KEYS`, `WATCH`, `FAST_ONLY` | cache cleanup, which signals get event rows, `--fast` mode |
| `configure(elm)` | the adapter setup sequence for this car |
| `decode(responses) -> (record, alive)` | raw lines in, flat record out |
| `apply_policy(cache, calib, state)` | optional per-vehicle sensor policy (the Leaf's current fusion lives here) |

`reader.set_vehicle()` binds the chosen profile into the reader's module globals and rebinds `signals.SIGNALS`. That was a deliberate choice: it means the scheduler tests, the store, the supervisor, the API and Tile Studio keep reading `reader.ITEMS` unchanged and needed no vehicle knowledge at all. Selection is `--vehicle` on `app.py` or `reader.py`, the `HAKAKE_VEHICLE` environment variable, or `"vehicle"` in `config.local.json`, defaulting to `leaf_ze0`.

`get_vehicle()` validates cross-references loudly on load: every tile must reference a real item, every signal must reference a real item, every item must have a kind that exists in `TARGETS`. A typo fails at import, not at 2 a.m. in a parking lot.

**`vehicles/lancer_2009.py` is the minimal example**, and it was written to be. Fifteen items of plain SAE J1979 plus four DTC reads, a decode function built from a dict of lambdas, no built-in tiles (those are Leaf SVGs), and a default layout made entirely of user signal tiles. The whole profile is about a page. Adding a car that speaks standard OBD-II should look like this.

`CLAUDE.md` states the boundary the seam exists to enforce:

> Vehicle-specific code (items, tiles, signal entries, decode, sensor policy) lives in `vehicles/<profile>.py`; `reader.set_vehicle()` binds it and `--vehicle` selects it (default `leaf_ze0`). Nothing outside `vehicles/` and `leaf_decoders.py` may hardcode a vehicle.

A fuller "adding a vehicle" guide is planned but not written. Until it exists, `vehicles/__init__.py` plus `vehicles/lancer_2009.py` are the documentation, and they are short enough to read in one sitting.

---

## History: your profile declares its own columns

Most of the record rides in a single `extra` JSON column, which costs nothing and loses nothing. But a signal you want to *graph* or *aggregate* — average compressor rpm last Tuesday, capacity against date across eight months — wants a real indexed column.

Your profile declares those itself, in `HISTORY_COLS`. The store reads the declaration and builds the schema, the insert, the history query and the daily rollups from it. There is no shared list to edit and no Leaf vocabulary anywhere in `web/store.py`.

A declaration is a column name mapped to a small spec:

```python
HISTORY_COLS = {
    "coolant_temp_c": {"kind": "real", "key": "coolant_temp_c",
                       "hist": "coolant", "round": 1, "hist_f": "coolant_f",
                       "daily": {"avg": "coolant_avg"}},
    "rpm":            {"kind": "int",  "key": "rpm", "hist": "rpm"},
}
```

`key` is where the value comes from in the decoded record — a plain key, a dotted path like `"temps.0"`, or a callable for something derived. `hist` names it in history responses, `daily` maps `avg`/`min`/`max` to the name that aggregate takes in `daily_health()`, `index` gets a partial index, and `hist_f` names the °F twin the store emits alongside it, because this project always presents temperatures both ways. `hist_f` is a *name*, not a flag, and it needs a `hist` to derive from — `python vehicles/__init__.py` says so if you forget.

Schema changes are additive and self-migrating: new columns are added with `ALTER TABLE`, nothing is ever dropped or renamed, and an existing database keeps working untouched. Rows are stamped with the profile that produced them, so two vehicles can share one file without their history mixing. A profile can also claim its own database file with `DB_FILE`.

One honest limitation remains. The `cells` table stores 96 per-cell voltages per read, which is a traction-battery shape. Any profile emitting a `cells` list gets rows there; profiles that do not never touch it. Rather than invent a per-vehicle abstraction for a sample size of one, it is documented as what it is.

---

## Apply this

To take a decode you trust and put it on a screen:

1. **Run the routine, not this chapter.** `docs/ADDING_SIGNALS.md` is the authority and it has a checklist at the bottom. Use the checklist.

2. **Add the item, and choose its period like it costs money.** It does: roughly 0.4 s per cycle over BLE for a fast item. `period: 0` is for things a human watches change in real time. Everything else gets a number.

3. **Write the registry entry before you think about the UI.** If you find yourself editing JavaScript to display a scalar, stop — you probably wanted a `kind` and a `color` instead.

4. **Add your keys to `ITEM_KEYS`** so they disappear when the item is switched off.

5. **Decide whether it needs history.** If you want to graph or aggregate it, give the signal a `hist` field and declare a column in your profile's `HISTORY_COLS`. If not, `extra` is fine and costs you nothing.

6. **Check the whole thing offline before you go near the car:**

   ```bash
   pytest -q                       # with the venv active; no hardware needed
   python scripts/privacy_sweep.py --log 50
   ```

7. **Commit it all together** — fixture, decoder, test, item, registry entry, `docs/SIGNALS.md` row and `WORKLOG.md` entry — as `signals: <what> from <ID/group>`.

That is the end of the guide. Back to [the index](00-index.md).
