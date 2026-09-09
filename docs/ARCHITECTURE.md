# Architecture

```
        vehicles/<profile>.py — items, decode(), TILES, SIGNALS, HISTORY_COLS
                  │  set_vehicle() binds it to the reader, signals, store, page
                  ▼
            BLE (bleak)  ─┐                          ┌─ battery_state.json ──► /api/status ─► browser (1 s poll)
 car ◄─ ELM327 clone ◄────┤  elm327.py  ◄─ reader.py ─┤
            USB (pyserial)┘   transport    scheduler  └─ leaf_battery.db ─────► /api/history, /api/health, /api/cells
                                                ▲
                                     web/tiles.json ◄── PUT /api/tiles ◄── Tiles menu
```

## Processes

**`web/app.py`** — Flask, bound to 127.0.0.1. Serves the single-page dashboard
and a small JSON API. Spawns **`web/reader.py`** as a supervised subprocess and
restarts it if it exits (a crash costs seconds, not the page). Each request
thread gets its own SQLite connection — sharing one segfaulted (2026-08-24).

**`web/reader.py`** — the only process that talks to the car. One asyncio
loop; a supervisor around connect → configure → poll that reconnects with
back-off on any transport error, detects "car asleep" (the primary ECU silent),
and honours `web/reader.pause` so calibration tools can borrow the adapter. It
imports no vehicle module — everything car-shaped arrives through the profile,
and its per-cycle console line is built from the first few fast-lane signals in
the registry. Shared car-independent helpers (`c_to_f`, `fmt_temp`, and `env()`,
which reads the `HAKAKE_*` variables with the older `LEAF_*` names as silent
fallbacks) live in `util.py` at the repo root; `leaf_decoders.py` re-exports the
temperature pair for its long-standing callers.

## Vehicle profiles

Everything vehicle-specific lives in one module per vehicle under
`vehicles/` — its items and UDS/passive targets, built-in tiles, signal
registry entries, the `HISTORY_COLS` it wants as real database columns,
`configure(elm)`, `decode(responses)`, and an optional sensor policy (the
Leaf's current fusion). `NAME`, `TITLE` and an optional `LOGO` name the car
in the page chrome. `reader.set_vehicle()` binds the active profile to the
reader's globals and the signal registry; `--vehicle` on `app.py` /
`reader.py`, `HAKAKE_VEHICLE`, or `"vehicle"` in `config.local.json`
selects it, default `leaf_ze0`. The scheduler, store, supervisor, API and
Tile Studio are all profile-agnostic — `vehicles/lancer_2009.py` (standard
mode-01 PIDs, no reverse engineering) is the proof and the template.

The contract is documented in `vehicles/__init__.py`, which also enforces
it. `validate_profile(mod)` returns the *list* of problems instead of
raising on the first one (and instead of `assert`, which disappears under
`-O`); `get_vehicle()` runs it and refuses to bind an invalid profile,
raising a `ValueError` that names every problem at once; and
`python vehicles/__init__.py [name …]` runs the same checks standalone as a
lint. `tests/test_vehicles.py` parametrises over `vehicles.available()`, so
the contract test extends itself to a new profile the moment the file
exists. Built-in dashboard tiles are Leaf SVGs; other profiles ship a
default layout of user signal tiles, which work everywhere.

## Scheduler

Every signal source is an **item** the profile declares: a UDS request to one
ECU (the Leaf's LBC and HVAC groups, the Lancer's mode-01 PIDs) or a passive
capture of one CAN ID. Each has a period. Period 0 = **fast lane**, run every
cycle (on the Leaf: battery state, gear, turn signals). Everything else is
**round-robin by overdue ratio** inside a per-cycle time budget (default
1.5 s), so a long cell read never starves the gear display. Items are polled in
the profile's `KIND_ORDER` (Leaf: LBC → HVAC → passive) to minimise ECU
switching; `elm327.configure_uds(elm, tx, rx)` sets up one such conversation
(`configure_leaf_bms()` is now just that call with the Leaf's headers).

**Only items needed by enabled tiles are polled.** The profile's `TILES`
(bound onto `reader.TILES`) maps each built-in dashboard tile to its items and
`signals.py` resolves a user tile's; `web/tiles.json` (written by the Tiles
menu) says which tiles are on. Turning a tile off hands its bus time to the rest.

**A tile option can also change how often.** `opts.celllog` on an enabled
tile that polls the cell voltages (the cell grid or the 3D pack) is the one
such option today: `reader.period_overrides()` turns it into `{lbc02: 0}`,
`Reader.period(i)` consults that before the profile's period, and the cell
read joins the fast lane — every cycle instead of every 20 s, budget or not.
The main loop then stores a row for every *fresh* cell read (a `cells_seq`
counter bumps on each real decode, so the sticky cache is never re-stored as
four identical cell sets), on top of the usual row every `STORE_PERIOD`. The
record carries `celllog: true` and the header shows a CELL LOG badge while it
is armed: on BLE the read costs ~1.3 s a cycle, and the database grows by 96
cell rows a cycle. It is the same read-only request, more often — what a
drive log needs for playback to show an acceleration event pair by pair.

An item's `est` is what one poll costs **over BLE** — that is the link the
numbers were timed on, and they stay in those units so a vehicle profile never
has to know which adapter is plugged in. The transport supplies the conversion:
each transport class carries a `SPEED` multiplier (BLE 1.0 by definition, USB
0.1) and `Reader.estimate()` multiplies by it. A passive capture is the
exception — `ATMA` runs for a wall-clock `secs` that no link can shorten, so
only its per-command overhead scales.

Measured over BLE: ~0.2 s per command round-trip plus ~40 ms per CAN frame;
a fast-lane cycle is ~1.5–2 s, full refresh of everything ≈ 20–60 s by period.

Measured over USB (CH340 ELM327 v1.5 clone, 2026-09-03, `tools/bench_transport.py`):
a command round-trip is **5–10 ms** at 115200 and a 435-byte answer 44 ms. The
adapter itself accounts for ~6 ms of that; the rest is wire time, which is why
the transport negotiates 115200 with `ATBRD` (see below). A full Leaf cycle
with every tile enabled models at ~3.4 s, of which ~3.2 s is `ATMA` dwell time
for the eleven passive captures and only ~0.24 s is UDS and adapter setup. **On
USB the passive dwell is the cycle time; nothing else is close.**

## Transport

`elm327.py` presents one `send(cmd)` coroutine over four back ends: BLE
(bleak), USB serial (pyserial), a recorded session, and a running model. Two
things about the USB path are worth knowing because they cost real time:

- **The answer ends at the `>` prompt.** The serial path blocks on
  `read_until(b'>')` with the serial timeout as the bound. It used to poll
  `in_waiting` and sleep 50 ms when idle, and then sleep the caller's `wait`
  on top — so an `ATI` the adapter answered in 11 ms took 107 ms to come back.
  `wait` still means something on BLE, where a reply arrives as a series of
  20-byte notifications and a late chunk can follow the one carrying the
  prompt; on a byte stream it bought nothing, so the serial path ignores it.
- **The wire rate is negotiated.** Every ELM327 powers up at 38400, where a
  29-frame answer spends ~200 ms just being transmitted. `SerialELM.set_baud()`
  asks for 115200 with `ATBRD` and *verifies* it: the chip answers OK at the
  old rate, sends its ID at the new one, and wants a bare CR back inside
  ~75 ms, so every failure path puts the link back at 38400 and carries on.
  `ATZ` — the first thing `configure_uds()` sends — resets the chip to 38400,
  so `send()` follows it down and negotiates back up; a run that died with the
  chip left fast is found again by `_recover_baud()`. `HAKAKE_SERIAL_BAUD`
  overrides the target (`off` stays at 38400). 230400 and 500000 negotiate on
  the clone tested but drop bytes, so they are opt-in, not the default.

`tools/bench_transport.py` is the measuring stick: it times round-trips at
several baud rates, compares the blocking read against the old polling loop,
and models a full cycle from the result. It is safe to run with the car asleep
— it sends AT commands, UDS *read* service 0x21 and monitor mode only.

## Data model

The vehicle profile's `decode()` turns raw ELM327 lines into one flat record
(the Leaf's via `leaf_decoders.py`). `web/store.py` persists a row per
`STORE_PERIOD` (5 s) to `readings`, per-cell rows to `cells`, sessions to
`sessions`.

**The profile decides what gets a column.** `HISTORY_COLS` in
`vehicles/<profile>.py` maps a SQL column name to a spec — `kind`
(`real`/`int`/`bool`/`text`), `type` (the SQL type, defaulted from `kind`),
`key` (the record key; may be a dotted list index like `"temps.0"`, or a
callable taking the whole record for a derived value), `hist` (the name the
value takes in `/api/history` entries), `round`, `hist_f` (also emit the °F
twin), `daily` / `daily_filter` (what `daily_health()` aggregates), and
`index` (a partial index on `ts_epoch` where the column is 1). `store.py`
builds the `readings` DDL, the insert, the downsampled `history()` and the
daily rollups from that declaration and holds no vehicle vocabulary of its
own — the Leaf declares 33 columns, the Lancer 18. Anything else the decode
produces rides in the `extra` JSON column, where it can be read back but not
charted or aggregated; `EXTRA_SKIP` drops keys not worth even that (raw
dumps, lists already stored in columns).

**Rows are stamped with the vehicle.** `readings`, `events` and `sessions`
all carry a `vehicle` column set to the active profile's `NAME`, and every
read filters `vehicle = <active> OR vehicle IS NULL`. Two cars can therefore
share one database file — every profile writes `web/leaf_battery.db` unless
it sets `DB_FILE` — without mixing new data. Rows written before the column
existed are NULL and are deliberately *not* back-filled: attributing them
after the fact would be a guess, so they stay visible to every profile.

**Migration is additive.** On open, any column the profile declares that the
file lacks is added with `ALTER TABLE … ADD COLUMN` and back-filled from
existing rows' `extra` JSON where it is still NULL. Nothing is ever dropped
or renamed, so a database written by an older version — or by a different
profile — keeps working untouched.

`cells` (one row per cell, per full read) is the honest exception: it is
driven by a generic record key (a `cells` list of millivolts, so profiles
that never emit one never touch the table), but its shape is the Leaf's 96
cell pairs. A pack that reports differently would need more than a key.

An **`events`** table records state *transitions* — the keys the profile
lists in `WATCH` (on the Leaf: A/C, HVAC on, gear, locks, any door, the
handbrake, high beam, fog) — the moment they happen; `on_time()` gives exact
durations independent of sample spacing.
Never pruned; downsampled on read.
All timestamps UTC ISO-8601 with `Z`; legacy naive-local data was converted on
migration.

**Playback frames go the other way.** `Store.frames(t_from, t_to)` rebuilds
the `/api/status` shape from rows — the `extra` bag, the columns, the
temperature lists and every °F twin from the °C columns, the cells joined in
one query — and marks each record `playback: True`, so the page can paint
every tile from a stored moment exactly as it paints them from the live state
file (`docs/PLAYBACK.md`). Thinning keeps the last *real* row of each time
bucket rather than averaging, because a frame is a state (gear, doors, cells)
and not a line. `Store.sessions()` derives sessions from gaps in the data
(the `sessions` table has no epoch column and no link to readings). Lost on
the way back, and documented as such: `adapter_port`, the `readings` counter
and the raw `balancing` list.

## Dashboard and Tile Studio

**Two page modes.** In *live* mode `poll()` fetches `/api/status` and
`/api/history` once a second and fans them out to five sinks — `updateTrend`,
`updateDash`, `updateSparkline`, `TileStudio.update`, `TileStudio.history`.
In *playback* mode `poll()` stands down and `renderFrame(k)` feeds the same
five sinks from a window of stored frames (`/api/playback/frames`), so no tile
knows the difference; the clock is `web/static/playback.js`, a pure transport
(seek / play / speed / step / jump, node-tested) ticked from one
`requestAnimationFrame` loop. The timeline is a page-level card above the
tile grid — no `data-tile`, no profile entry, so it exists for every vehicle —
shown only in that mode. Recorded frames carry `playback: true`: the status
dot never calls them stale, the adapter badge says "recorded", and
`TileStudio.runAlerts` ignores them unless the timeline's alerts box is
ticked. The timeline docks to the window, carries the owner's flags
(`web/bookmarks.json`, `/api/bookmarks`) and the auto-detected discharge
pulls (`Store.pulls()`, `/api/bookmarks/auto`), and its playhead is grabbable.
`docs/PLAYBACK.md`.

`web/templates/index.html` (no framework) is rendered with the active
profile's chrome: the page title and header subtitle come from `TITLE`, the
wordmark silhouette from `LOGO` (`leaf_ze0` sets `"leaf"`; a profile without
one gets a neutral dial), and the mark's level fill appears only when the
profile's registry declares a level signal (`soc`, else `fuel_pct`).

The same file holds the twelve built-in tiles — SOC, health, temps,
vehicle/shifter, tires, body (doors/locks/lights on a top-down car), climate,
power, history, degradation, cells, and the 3D pack — five of which (vehicle,
tires, body, climate, pack3d) are `{% include %}`d from
`web/templates/tiles/*.html`; the first four are painted by
`web/static/tiles.js`, so the cockpit can host the same markup from the same
record. Those are Leaf assets: they belong to whichever profile lists them in
`TILES`, and for a profile that lists none (the Lancer's `TILES = []`)
`tilestudio.js` takes them out of the grid and hides the cards rather than
leaving twelve that will never take a value.

The 3D pack tile is the page's one ES module (`web/static/pack3d.js`, three.js
via an import map). It reads the profile's `PACK_*` geometry from a `PACK`
constant the template writes, turns it into 96 bodies through the pure
`web/static/pack_layout.js`, and publishes `window.Pack3D`; `updateDash` calls
`Pack3D.render(document, data)` each poll, parking the record in
`window.__pack3dPending` when the module has not finished loading (module
scripts are deferred). Tile Studio gives such tiles three small hooks:
`TileStudio.opts(id)`, `enabled(id)`, `tile(id)`, `size(id, w, h)`, `setOpt(id, key, value)`,
and `menuExtra(id, fn)` to add tile-specific rows to the ⋯ menu, plus a `tiles:applied` DOM event after
every layout change so the tile can re-read its opts and re-measure. See
`docs/PACK3D.md`.
`web/static/tilestudio.js` owns everything configurable:

- **Layout:** [gridstack.js](https://github.com/gridstack/gridstack.js) v13
  (MIT, vendored in `web/static/vendor/`, no CDN so the car works offline).
  The 3D pack tile's [three.js](https://threejs.org) r170 is vendored the same
  way under `web/static/vendor/three/` and reached through an import map, since
  three.js ships as ES modules only.
  12 columns × 40 px rows; every tile carries `x, y, span, h`. Grab the
  **title bar** to move a tile anywhere — tiles it lands on are pushed aside
  and everything compacts upward (`float:false`); grab the **bottom-right
  corner** to resize. One-column mode below 720 px. Tiles without a stored
  position auto-place, and their height is measured from content once.
  gridstack's `change` event writes positions back to the config.
- **Per-tile ⋯ menu:** width, colour scale (+ invert, min/max), hide; for user
  tiles also signal, display type, title, history range, remove. The menu is
  **portalled to `document.body`** and placed in viewport coordinates —
  right-aligned under its ⋯ button, flipped above when there is no room below,
  clamped eight pixels inside the viewport, a fixed 280 px wide, scrolling
  internally when the window is shorter than it is. A card clips its own
  overflow, so a menu drawn inside one lost its edges on a narrow or short
  tile. A `requestAnimationFrame` tracker, alive only while a menu is open,
  follows the button through scrolling, resizing and gridstack's move
  animation, and closes the menu if its tile goes away. ⋯ toggles, Escape
  closes, and a *Done* button sits at the right of the foot beside hide and
  reset — changes were always applied live, so Done only closes.
- **Alerts (in the same menu):** one row per value the tile shows — a signal
  tile its one signal, a built-in tile the `signals` list its profile
  declares on the `TILES` entry (served as `tile_signals` by `/api/signals`,
  so the cockpit sees the same lists; a tile that declares none offers every
  non-text signal its items produce). A row is a rule: *below* / *above* (or
  *when on* / *off* for a bool), a tone, a *repeat* slider (every 1–60 s,
  default 10; `Alerts.REPEAT`, mirrored by `reader.ALERT_REPEAT_*`) and a
  tick to arm it. Thresholds commit on `change`, not per keystroke, so typing
  "50" never fires at "5". `web/static/alerts.js` owns the sound and the
  engine: the tone is a Web Audio oscillator (four patterns, gain-ramped so
  it does not click; the `AudioContext` is unlocked on the page's first
  pointerdown/keydown, and ▶ on a row doubles as that gesture), and
  `createEngine().evaluate(rules, record, now, ctx)` is pure — it fires on
  the transition into breach, fires again every `repeat` seconds while the
  value is actually past the line (each fire sounds the tone and restarts the
  card's `.alert-flash` animation, so the flash keeps the tone's beat; mute
  silences the tone only), re-arms after the value comes back inside by a
  hysteresis of 1 % of the signal's registry range, and freezes (no fire, no
  re-arm) while `status` is not `ok`, the item's `item_age` is missing or
  past `max(90 s, 3 × period)`, or the value is null. Breach state is
  in-memory only; a reload re-evaluates and a still-breached rule sounds
  once. 🔔 in the header is a global mute in `localStorage`
  (`hakake-alerts-muted`), shared by the cockpit, which runs the same engine
  through `TileStudio.update()`; a breached card carries `.alerting`.
- **User tiles:** *Tiles ▾ → add* creates a tile for any entry in
  `signals.py` with any renderer: number, ring, arc gauge, dial, bar,
  thermometer, battery, line / area / bar graph (from `/api/history`), text,
  lamp. Values are colour-encoded through the chosen scale as they change.
- **Calibration:** per-car offsets (current zero) live in gitignored
  `web/calibration.json` via `GET/PUT/DELETE /api/calibration`.
- **Persistence:** `web/tiles.json` via `GET/PUT /api/tiles` (order, enabled,
  x/y/span/h, type, opts, signal) is the *active* layout. Alert rules travel
  inside `opts.alerts`, one `{signal, min, max, when, tone, repeat, enabled}`
  per value; `reader._clean_alerts()` keeps them well-formed on every write
  (unknown signals, unknown keys and rules with no threshold are dropped,
  numbers coerced), for the dashboard store and the cockpit's alike. **Named layouts**
  live in `web/layouts.json` (`/api/layouts`): save the active one under a
  name, load one back (it overwrites `tiles.json`, so the reader follows),
  delete, or reset to defaults. Both files are gitignored — layouts are
  personal. Each tile's ⋯ menu has *reset tile* (default size, style,
  colours, title, fresh auto-position). The reader reads the same file, so **a tile that
  is off — built-in or user — is not polled**; a user tile pulls in exactly
  the one item its signal needs.
- `/api/signals` serves the registry (signals, colour scales, tile types,
  items, `tile_signals`) so the UI never hard-codes them.

The **tires** tile is drawn by `web/static/tiles.js` into a 2×2 block that is
an inline-size container, so the wheel art is sized by the card and not by the
window: `clamp(64px, 24cqw, 200px)` — 64 px wheels at a two-column span, ~93 px
at the default four, 200 px at full width — with the psi and name type scaling
modestly and the SVG shrinking first when the tile is short. Nothing there
depends on the card's height being known in advance, so `measureRows()` still
sees a width-derived height on first load. The same files serve the dashboard
and the cockpit.

Every tile that depends on a slow item shows its age. Power sign comes from
the BMS, not from an SOC-delta guess. Temperatures are °F with °C beside them.

## Testing

`tests/` runs without hardware: decoders against captured frames
(`tests/fixtures/`), the store against a temp database (including two
profiles sharing one file and opening a database written before the columns
existed), the profile contract over every module `vehicles.available()`
finds, the reader's supervisor/scheduler against a fake adapter that can die,
go silent, or be paused, the privacy sweep, and — front-end — that gridstack
is vendored with its license and the dashboard JS passes `node --check`
(skipped where node is absent). `pytest -q`.

Two harnesses run the *whole* stack — reader, scheduler, transport,
decoders, store, API, page — with no hardware:

- **Replay** (`--adapter replay`, `docs/REPLAY.md`): a `ReplayELM` answers
  every AT and UDS command from a recorded session fixture in
  `tests/fixtures/`, made by `record_session.py`. `tests/test_replay.py`
  checks the fixtures are valid and come from the committed captures, and
  that the transport behaves like an adapter (`ATSH` selects the ECU,
  `ATCRA` filters, a missing command is `NO DATA`, frames advance with time);
  `tests/test_replay_e2e.py` runs the reader end to end for both profiles and
  checks that rows reach the store, that everything is labelled `replay`, and
  that a Lancer never shows a Leaf signal.
- **The simulator** (`--adapter sim`, `docs/SIMULATOR.md`): a physical model
  in `simulator/` behind the same interface (`docs/SIMULATOR_CONTRACT.md`),
  driven live through a control API and the `/sim` cockpit. Its suites cover
  the model (charge curve, load table with provenance, the one power identity
  wall → charger → loads → pack, the push-button start's state machine, °F
  twins, lamps, sub-stepped integration, time scale), the round trip
  `record() == decode(encode(state()))`, the transports, the control API
  and the launch plumbing against both the real core and a contract stub
  (`tests/sim_stub.py`), the bulk history generator, and both pages — the
  cockpit, which must generate every control from `/sim/schema`, and the
  control API's own fallback landing page (`simulator/panel.html`), which is
  endpoints and curl lines — neither of which may name a knob.

The dashboard's tile engine and the four styled tiles are a library now
(`TileStudio.init(...)`, `web/static/tiles.js`), so `tests/test_layout_engine.py`
and `tests/test_dashboard_tiles.py` check the page boots them the same way it
always did and that the cockpit can reuse them.

CI (`.github/workflows/ci.yml`) runs `pytest -q` on Python 3.10 and 3.12
and then the privacy sweep, on every push and pull request — the two gates
that must stay green. 800 tests at the time of writing.
