<!-- SPDX-FileCopyrightText: 2026 David D. Karnowski -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# The simulator

Ha-Kake can run its whole stack against a **generated car**: a physical model
(`simulator/`) answers the UDS and monitor commands a real ELM327 would, and
the reader, the decoders, the store, the API and the dashboard cannot tell the
difference. It is a **development fixture** — a stand-in car for working on
the app's tiles, charts and reports — not a signal-verification tool (see
*The honest caveat*).

## Start here: one command

```bash
python web/app.py --adapter sim
# Ha-Kake dashboard   http://127.0.0.1:5000
#   simulator panel   http://127.0.0.1:5000/sim
#   control API       http://127.0.0.1:8099/sim/schema
```

That is the whole launch. One process starts the simulated car (inside the
reader), the dashboard, and the **control API** on `127.0.0.1:8099`; the
banner prints all three URLs. Open the dashboard and it looks like a car is
plugged in — with a **SIMULATED** badge in the header that links to `/sim`,
the **cockpit**: a page that looks like the car, where you open a door, press
the pedal, turn on the A/C, degrade a cell, and watch the dashboard react.

Useful variations, all real flags of `web/app.py`:

```bash
python web/app.py --adapter sim --scenario commute --seed 1     # a scripted drive
python web/app.py --adapter sim --knob soc=20 --knob fault.cell_degraded=true
python web/app.py --adapter sim --vehicle lancer_2009            # the other profile
python web/app.py --adapter sim --sim-control 0                  # control API on a free port
python web/app.py --adapter sim --no-sim-control                 # no control API at all
python web/app.py --adapter sim --port 5055                      # dashboard elsewhere
```

`--sim-control PORT` defaults to **8099 in sim mode**; `0` asks for any free
port (the reader reports the one it got as `sim_control_url` in `/api/status`,
and the cockpit finds it there); `--no-sim-control` opts out, in which case
`/sim` renders but has nothing to drive. If 8099 is already taken — a stale
rig, say — the reader does not crash: `SimELM.serve_control()` catches the
`OSError`, retries on a free port and logs both numbers (`elm327.py`).

### The other path: a real serial port

`hakake_sim.py --pty` runs the model as a **separate process** and publishes
a pseudo-terminal. The dashboard then connects with the real `SerialELM`,
over a real tty, exactly as it does to a USB dongle — so this path exercises
the serial transport that `--adapter sim` bypasses, and is the strongest form
of "it looks like a car". It works on macOS and Linux.

```bash
python hakake_sim.py --pty --scenario drive
# Ha-Kake SIMULATOR — no adapter, no car. Every value is generated.
#   vehicle:  leaf_ze0    scenario: drive    seed: 1
#   pty:      /dev/ttys005
#   start the dashboard against it with exactly this:
#             python web/app.py --adapter sim --sim-serial /dev/ttys005 --sim-control 8099 --port 5055
#   cockpit:  http://127.0.0.1:5055/sim?control=http://127.0.0.1:8099   <- the dashboard's simulator page
#   panel:    http://127.0.0.1:8099/   (minimal fallback panel served here)
#   control:  http://127.0.0.1:8099/sim/schema   (see docs/SIMULATOR.md)
```

The rig prints the exact dashboard command (one function builds both the
printed line and the spawned process, so they cannot drift), or runs it for
you: `--launch-dashboard [PORT]` starts `web/app.py` as a child on port 5055
by default and takes it down when the rig exits. It needs `--pty` — without
one there is nothing for a dashboard to connect to, and the rig says to use
`web/app.py --adapter sim` instead.

**Which one do you want?** `web/app.py --adapter sim`, unless you are
working on `elm327.py`'s serial transport. The pty path exists because the
transport is the one thing the in-process path does not test. Use
`--sim-serial`, **not** `--adapter usb`, against a pty: both would connect,
but only the first keeps the mode "sim", which is what sends generated rows
to the throwaway database instead of the one holding real readings.

### Three other ways to run without a car

| Mode | What it serves | Good for |
|---|---|---|
| `--demo` | frozen JSON in `docs/demo/` | reproducible screenshots |
| `--adapter replay` | a **recorded** session fixture (`docs/REPLAY.md`) | "does this profile decode a real capture?" |
| `--adapter sim` | a **running model** you can change mid-run | "what does the dashboard do when a cell degrades?" |
| `hakake_sim.py --generate` | **months of history**, written straight to a database | "what does this chart look like with six months behind it?" |

A recording can only replay the conditions that happened to exist when
someone was sitting in a parked Leaf with the AC on. A simulator can be told
to drop the state of charge to 15 %, degrade cell 55, and fail the insulation
test, **while you watch the tiles move** — none of which can be ordered up
in a real car.

## The honest caveat

**A simulator verifies consistency, not truth.** The round trip
model → encode → adapter → decode → compare pins the decoders against an
explicit spec, which makes refactors safe. It cannot catch an error the
encoder and the decoder share: it would **not** have caught the group-05
÷1024-vs-×0.001 scale bug, because both sides would have been wrong together.

A green simulator run is not verification against a real car. Anything the
model produces is labelled — `simulated: true` in every state record, `sim`
as the adapter type, a badge on the dashboard, a hazard bar on the cockpit,
and a shout in the startup log — precisely so that no screenshot, database
row, or API response can later be mistaken for a reading from a vehicle.
Simulated rows go to `web/sim_<profile>.db`, never to `web/leaf_battery.db`.

The model's numbers are honest about their provenance, too. Every row of the
load table (below) is labelled **MEASURED**, **OWNER REPORT** or
**ASSERTED**; the motor and regen are asserted shape, not measurement, apart
from Eco coast regen, which the 2026-09-03 drive corroborated. That drive was
the one `docs/ROADMAP.md` called for, and what it could and could not settle
is written out under "Motor and regen" below. The cockpit's cluster draws the real car's indicators the model has
no driver for — ABS, VSP, brake, PS, shift control, VDC, seat belt, air bag
— **dim and dashed**, listed in `lamps_unmodelled`, never lit. The head
unit's AUTO, MODE, FRESH/RECIRC, DEFROST and DEFOG buttons are drawn
**inert**, because this project cannot read them from the car: vent MODE,
AUTO and fresh/recirc were walked with `calibrate_input.py` on 2026-08-24
and moved nothing anywhere, including HVAC group 00, which stayed
`80 01 80 00`; the defrost and defog encodings are unresolved
(`docs/SIGNALS.md`). The pedal knobs carry a *not readable* tag for the same
reason: `accel_pedal_pct` and `brake_pct` are simulated, but 0x180 is not in
the Leaf profile's `ITEMS`, so no tile displays them. A panel that faked any
of those would be lying about the one thing this repo is careful about.

---

## The cockpit (`/sim`)

The dashboard serves it (`web/templates/sim.html` + `web/static/sim.js`), on
the dashboard's own tile engine (`tilestudio.js`, gridstack) and with the
dashboard's own four styled tiles (`web/templates/tiles/*.html`,
`web/static/tiles.js`) — so what you drive here looks exactly like what the
dashboard shows. Its JavaScript talks to the control API cross-origin
(the API sends `Access-Control-Allow-Origin: *` on JSON and answers
`OPTIONS` with 204).

**It always renders.** With no simulator behind it — the dashboard was
started on a real adapter, or with `--no-sim-control` — the page shows the
two launch commands instead of cards. It finds the control API in this
order: `?control=http://127.0.0.1:PORT` in the URL (what the `--pty` rig's
printed cockpit link carries), then the URL `web/app.py` knew at startup,
then `sim_control_url` from `/api/status` (how `--sim-control 0` is found;
the page keeps asking while the reader says it is simulated and has not
published a port yet).

**What is on it**, top to bottom by default — every card can be dragged by
its title, resized or hidden from its ⋯ menu, and the arrangement persists
in `web/sim_tiles.json` (gitignored; `GET/PUT /api/sim/tiles`, with a
localStorage mirror `hakake-sim-tiles-v1`). *Cards ▾ → reset arrangement*
puts it back.

* **A hazard bar** saying SIMULATOR, and a *Dashboard →* link; the dashboard's
  SIMULATED badge links back.
* **Scenario select**, with *— none (free-running) —* first and a sentence on
  what a scenario is; loading one reloads the page (a scenario may switch
  vehicles). *Reset knobs to defaults* is `POST /sim/reset`.
* **The power switch.** A round POWER button above the cluster, with a *hold
  the brake* toggle beside it — the ZE0's push-button start (see below). Dim
  for OFF, an amber ring for ACC, green for ON, lit green for READY (the READY
  lamp in the strip follows). Every push is a `POST /sim/power`; what the car
  did comes back as a line beside the button ("refused — Remove charge
  connector"). Drawn only for a profile whose schema has an ignition knob, and
  removed if the control API answers 501: a car with a key has no button.
* **Instrument cluster — emulated.** The twin combination meter's shape in
  original SVG: the eyebrow with the digital speed, ECO lamp and eco trees;
  the bubble power meter (the dot walks left into the charge bubbles on
  regen, right under load, and the *available* bubbles narrow when the pack
  is hot, cold or low — the same `output_avail()` curve that lights the
  turtle); the pack-temperature gauge, the remaining-energy gauge, the 12-bar
  capacity gauge (a different fact from the energy gauge), distance to
  empty, odometer and shift position. Above them the **indicator strip**, lit
  from `record.lamps`: READY, blinking turn arrows, low/high beam, position,
  fog, parking brake, door ajar, plug-in, 12 V, EV system, turtle, low
  battery, TPMS, headlight-left-on, security, and the red and yellow master
  warnings; the unmodelled lamps drawn dim with a *no driver in the model*
  tooltip. The dot-matrix line shows `record.messages` ("Battery level is
  low", "Motor power is limited", "Check tire pressure", …). Outside
  temperature and every gauge readout are °F with °C alongside.
* **Climate head unit — emulated.** OFF, TEMP ▲/▼ (the setpoint, 1 °F a
  press), A/C, FAN ▲/▼ are live and name the knob they move on hover;
  setpoint, cabin, evaporator and ambient read °F / °C; the blower bars and
  the HVAC draw in watts from `loads_w`, and — while a charge is running —
  what that draw leaves for the pack. The inert buttons are the ones the
  car does not let this project read (above).
* **The dashboard's four tiles, made interactive** — present only for a
  profile that declares them (`TILES`), and only while the record can drive
  them:
  - *Tires*: a slider and a number box under **each wheel**, bound to
    whichever `tpms_<corner>` knobs exist, in half-psi steps;
  - *Body*: click a **door** to open or close it, a **headlamp**, **fog** or
    **parking** lamp to switch it, a **turn or side repeater** to cycle
    off → left → right → hazards, a **brake lamp** to press the brake (0 ↔
    50 %), a **lock** to lock or unlock; a button strip covers the same
    knobs plus high beam;
  - *Vehicle*: click a **shifter slot** to select the gear, the state line to
    cycle `start_state`, the parking-brake mark to toggle it;
  - *Climate*: the dashboard's climate tile, reading what the head unit set.
  Every handler installs only when the knob exists in the schema, so a
  profile with no doors simply has no clickable doors.
* **Simulated time.** The effective clock (`12×`, and where it came from),
  simulated time elapsed, **clock speed** presets 1 / 10 / 60 / 600 / 3600 ×
  plus a number box (the `clock_scale` knob — present only when the model has
  one, and *locked* with the reason when the run was started with `--speed`,
  which overrides it), and **Skip ahead** buttons — 10 s, 1 min, 10 min,
  1 h, 6 h — each a `POST /sim/step {"sim_seconds": N}`: the model jumps by
  that much simulated time whatever the clock is doing.
* **Live model state**: the power budget as a line — wall → charge → loads →
  pack, with the house sign explained on hover — then a chip per switched-on
  consumer, then every field of the record (or of `state()`, for a core with
  no record), with each `x_c` / `x_f` pair shown as one temperature the house
  way, and the raw JSON under a disclosure.
* **One card per knob category** — *Battery pack*, *Load & motion*,
  *Climate*, *Body & lights*, *Rig & environment*, and *Faults — destructive
  states* in a hazard-striped card (on the Lancer: *Engine*, *Diagnostics*,
  *Rig*, *Faults*). Each knob shows its **label** as the title with the knob
  name small beneath, its unit and its help on hover: a slider *and* a number
  box for bounded numbers, a toggle for booleans, a dropdown for choices, a
  plain field for anything unbounded, and for any temperature knob a **paired
  °F / °C control** — slider in °F, a number box for each, either box drives
  the other — whatever unit the model keeps it in.

The page polls `/sim/record` and `/sim/knobs` once a second and `/sim/info`
every five, and never overwrites a control you are in the middle of editing.
Errors surface in the page: a bad value shows the API's own near-match
suggestion, and the suggestion is clickable.

**Two rules it is built to.**

*Generated, not written.* There is no knob list in `sim.html`, `sim.js` or
the fallback `simulator/panel.html`. Every control comes from `/sim/schema`,
and the grouping comes from the `category` field on each knob
(`simulator/knobs.py`), so the Leaf's sixty-odd knobs and the Lancer's 28
render from the same code and so will the next profile's. The skin is
**feature-detected, never profile-detected**: the cluster draws when the
record carries `lamps` and a state of charge, the head unit when it carries
the HVAC keys and the schema the HVAC knobs, each reused tile stays only if
its renderer found its keys — and a core whose `/sim/record` answers **501**
(the Lancer's, today) gets knob cards, the time card and the raw state, and
nothing Leaf-shaped, with zero profile code. `tests/test_sim_panel.py` fails
if a knob name is pasted into a page beyond the few an interactive skin has
to drive; `tests/test_sim_page.py` renders the Lancer and checks nothing
Leaf-shaped came out.

*Honest about what it cannot see.* The inert buttons, the unmodelled lamps
and the *not readable* tags, as described above. And nothing on the page is
a photograph, screenshot, manual scan or manufacturer mark: the cluster and
head unit are original SVG and CSS drawn from published descriptions of the
layout, no CDN, no image, no build step, and Ha-Kake is not affiliated with
or endorsed by Nissan (see `NOTICE`).

**The fallback panel.** The control API still serves a page of its own at
`/`, `/panel` and `/sim/panel` (`simulator/panel.html`): a minimal,
schema-generated knob list for `--pty` runs before the dashboard is up, and
for anyone who opens the API port in a browser. Everything the cockpit can
do, it can do — and so can `curl`.

---

## The load model

Pack current is a **sum**, not a knob, and there is exactly one place the sum
is done: `LeafModel.power()` in `simulator/model.py`. Everything else — the
current, the dashboard's power figure, the cockpit's chips — reads that.

### One power identity

```
wall_kw  ──►  charger_kw  ──►  loads_kw  ──►  pack_kw
   │              │                │              │
   │              │                │              └─ charger + regen − loads + extra
   │              │                └─ Σ loads_w (everything but regen)
   │              └─ DC into the car, after the SOC taper and any derate
   └─ charger_kw / CHARGE_EFF — what the meter at the wall would read

I = pack_kw · 1000 / V_ocv        (positive = into the pack, the house sign)
```

`state()` and `record()` carry the whole thing as `power` — `wall_kw`,
`charger_kw`, `loads_kw`, `loads_total_w`, `regen_kw`, `extra_kw`, `pack_kw`,
`hvac_kw`, `motor_kw`, `charge_eff`, `load_override` — plus `pack_kw` and
`loads_total_w` at the top level. `power_kw` is the same number at the
**terminals** rather than at the OCV, so the two differ by the pack's own I²R,
a few tens of watts; `Σ loads ≈ −power_kw·1000 + I²R` is still a test.

A worked example, the one the owner reported. Level 2, A/C at full blast, a
35 °C cabin, setpoint 60 °F, fan 7:

| | kW |
|---|---|
| wall | 3.626 |
| → charge into the car | 3.300 |
| → loads (base 90 W + blower 300 W + A/C 2750 W) | −3.140 |
| → **pack** | **+0.160** |

Before this the same state reported the full 3.3 kW going into the pack and
0 W of A/C, because `loads()` read the `base_off` row while `charging` and
gated HVAC on `start_state == "ready"`.

`extra_kw` is the `current_a` knob expressed as power, and is 0 unless
somebody asked for it:

```
I = (P_charge + P_regen − Σ loads) / V_ocv  +  current_a
```

where the loads are the switched-on rows of the table below, charging and
regen put watts back, and `current_a` is an **extra** current on top of the
model, default **0** — it kept its name when its meaning changed on
2026-09-03, because renaming it would have broken the contract, sixteen test
sites and five scenarios (which were re-authored to drive `speed_mph`,
`accel_pedal_pct` and `brake_pct` instead). `load_kw > 0` is an **absolute
override** that bypasses the whole model, kept for tests that want an exact
number. Setting `current_a` to what used to be the whole draw now
double-counts.

Before this, `current()` was a three-way selector — charging, else `load_kw`,
else the raw knob — so the A/C, the 5 kW heater, the blower and the
headlights changed the current by exactly **zero watts**, and the −1.5 A idle
default (about 576 W) was neither measured nor gated on READY: a powered-off
car drained 35 kWh a day. The owner's review of the first panel caught it.

### `LOADS_W`, with provenance

Every row says whether it was **MEASURED** (a number somebody read off an
instrument, source named), an **OWNER REPORT**, or **ASSERTED** (a plausible
figure nobody here has measured). This is the table in `simulator/model.py`;
`tests/test_sim_loads.py` fails if a row loses its label.

| Row | W | Provenance |
|---|---|---|
| `base_ready` | 150 | **MEASURED** — READY, everything off: 140–160 W |
| `base_on` | 60 | ASSERTED — ignition ON without READY: dash and ECUs up, no inverter |
| `base_acc` | 40 | ASSERTED — accessory position |
| `base_charging` | 90 | ASSERTED — plugged in and charging: contactors closed, so the DC-DC, the LBC, the coolant pump and the charger's control electronics are all awake. The `base_on` class of draw, not a sleeping car. The charger's *conversion* loss is not here — that is `CHARGE_EFF`, on the wall side |
| `base_off` | 3 | ASSERTED — contactors open; the LBC's quiescent draw and 12 V top-ups. ≈0.8 %/day on this 24 Ah pack, the "about 1 %/day sitting" owners report |
| `low_beam` | 70 | **MEASURED** — +LED low beams: 160 → 230 W |
| `high_beam` | 200 | **MEASURED** — +halogen high beams: 160 → 360 W. Measured with the low beams lit underneath, so the row already contains them: while high beam is on, `low_beam` reads 0 |
| `position` | 15 | ASSERTED — position/parking, tail and plate lamps |
| `fog` | 110 | ASSERTED — two 55 W halogen fog lamps |
| `turn` | 21 | ASSERTED — one 21 W bulb per side at ~50 % blink duty, averaged; hazards draw both sides |
| `brake_lamps` | 42 | ASSERTED — two 21 W stop lamps, lit while `brake_pct > 2` |
| `reverse_lamps` | 36 | ASSERTED — two 18 W reversing lamps, lit in R |
| `blower_max` | 300 | ASSERTED — blower at 11 V; scaled as `(V/11)²` from the amp's `FAN_VOLTS` table |
| `ac_min` … `ac_max` | 1500 … 3000 | **OWNER REPORT** — ~1.5 kW at a mild cabin, car stopped; up to ~3 kW with a hot cabin in hot weather. Scaled by `clamp((cabin − setpoint)/8, 0.3, 1)` and by how hot the ambient is |
| `ptc_max` | 5000 | **OWNER REPORT** — the resistive heater is 4.5–5.5 kW flat out; linear in `heater_level / 40` |

The measured rows come from the mynissanleaf "Lab Test" thread, where user
Ingineer put a kelvin shunt on the cell interconnects of a 2011 and read the
pack-side draw directly.

Rules the table is applied with:

* **The base draw follows `start_state`** (`off / acc / on / ready`), except
  while `charging`, which has a base row of its own (`base_charging`). A Leaf
  will not go to READY with the connector latched, so the ignition knob does
  not decide a charging car's base draw — but the car is anything but asleep.
* **HVAC draws in READY *or* while charging**; the motor draws only in READY,
  and never with the connector latched; the lamps draw in every ignition
  state. Running the climate control on the plug is what pre-conditioning
  *is*, and it comes off the charge power: on Level 2 with the A/C at full
  demand, `pack_kw` is below `charger_kw` by the whole HVAC draw. The taper is
  still a function of SOC applied to `charger_kw`; the loads come off after.
* **Motor and regen: one number corroborated, the rest still ASSERTED**, and
  the source says which is which. Road load is `(150·v + 0.38·v³) / 0.85` W
  with v in m/s — ≈5.7 kW at 40 mph, ≈11 at 55, ≈19 at 70 (`cruise_kw(mph)`)
  — plus a pedal term shaped like a motor's envelope up to 80 kW, in D / Eco
  / R only, and only while the pedal is pressed: lifting off is coasting.
  Regen is 2 kW (D) or 4 kW (Eco) on lift-off above 3 mph plus
  `brake_pct/100 · 30 kW`, fading in below 15 mph and out between 90 and
  95 % SOC.

  The 2026-09-03 drive (13 min, 3.5 mi, peak 41.4 mph, pack current to −66 A)
  was the capture these were waiting for, and it settled exactly one of them.

  * **Eco coast regen, 4.0 kW — corroborated.** Rows in Eco with the brake
    released and power flowing back gave a median +3.60 kW at 10–20 mph
    (n = 9), peaking at +7.68 kW. This is the one term road grade cannot
    fake: lift-off regen is a commanded torque, so a hill changes how fast
    the car slows, not the watts returned at a given speed. Not promoted to
    MEASURED, because the accelerator position is never logged and each row
    averages 5–6 s of a decelerating car.
  * **Road load — still ASSERTED, deliberately.** Grade is unmeasured (no
    GPS, no altitude, no inclinometer; `0x1D5` torque and `0x260` power
    limits are not polled), so fitting power against speed would record the
    route's hills as rolling resistance. The 35–40 mph steady bin spans
    −15.07 to −3.72 kW at n = 12; one "steady" 32 mph row was *regenerating*;
    the speed profile's mirror correlation is −0.05, so grade cannot be
    cancelled by out-and-back averaging either. The asserted curve is not
    contradicted — it lies inside the observed envelope, toward its low-draw
    edge — and that is all this drive can say.
  * **The pedal term and 80 kW peak — still ASSERTED**, and unmeasurable
    here: the accelerator is not on Car-CAN. The largest traction draw seen
    was −23.8 kW at 26.6 mph, a lower bound and nothing more.
  * **D coast regen and `brake_pct/100 · 30 kW` — still ASSERTED.** The drive
    yielded one usable D coast sample, and the brake pedal never went past
    8.6 %, so nine-tenths of the brake range is untouched.
  * **Nothing above 41.4 mph is calibrated by any of this**, and no highway
    road-load curve should be extrapolated from a 41 mph drive.

  What would settle the rest: `research/driving_capture_plan.md` R1's
  steady-speed ladder driven out-and-back over the same stretch so grade
  cancels, or R7's deliberate hill climb and descent.
* **Charge efficiency** (`CHARGE_EFF`): `l1` 0.78 and `l2` 0.91 are
  **MEASURED** (same thread: Level 1 77.5–78.3 %, Level 2 90.9 % — 3.756 kW
  at the wall → 3.414 kW into the pack); `l2_66` 0.91, `dcfc` 0.95 and
  `custom` 0.90 are ASSERTED. `state()` reports `wall_kw` — what the meter at
  the wall would read for the pack-side `charge_kw` — and `charge_eff`.

Everything is observable: `state()` (and `record()`) carry `loads_w` — one
entry per row in dashboard order, `base, low_beam, high_beam, position, fog,
turn, brake_lamps, reverse_lamps, blower, ac, ptc, motor, regen` (`regen` is
listed for the cockpit's benefit but is a source, not a sink) — plus
`load_total_w`, `motor_kw`, `regen_kw` and `hvac_kw`. At READY idle the model
reads 150 W and about −0.39 A; switched off it loses 0.8 % a day. The
identity `Σ loads ≈ −power_kw·1000 + I²R` is a test.

```bash
python hakake_sim.py --dump-state | jq '.state.power, .state.loads_w, .state.messages'
```

### The couplings audit

A dashboard is only as honest as the relations behind it, so every one of them
is written down in `COUPLINGS` in `simulator/model.py` with one of three
verdicts — **implemented**, **kept** (it was already there) or **not
modelled** (deliberately, with the reason). `tests/test_sim_loads.py` fails if
a row loses its verdict. The short version:

| Relation | Verdict |
|---|---|
| charging → accessories (HVAC on the plug, `base_charging`) | implemented |
| the power budget → `current()` | implemented |
| A/C demand ← cabin − setpoint, ambient | kept |
| compressor rpm ← A/C demand (was: fan speed) | implemented |
| blower → cabin approach rate and watts | kept |
| HVAC power → cabin (only when powered *and* a compressor or heater runs) | implemented |
| PTC → cabin rise and draw | kept |
| evaporator ← compressor and airflow | implemented |
| charging → pack heat, charger loss | kept |
| `lv_volts` ← converter, lamps, time | implemented |
| SOC → taper knee, regen limit | kept |
| pack temperature → charge derate, `output_avail` | kept |
| motor ← speed, pedal, gear | kept |
| regen ← gear, speed, brake, SOC | kept |
| range ← SOC × capacity × voltage ÷ (driving + accessories) | implemented |
| headlights while off → 12 V drain | implemented |
| HX ← SOH | **not modelled** — two data points (35 % ↔ 17.96, 91 % ↔ 92) fit no simple law, and HX is read straight off group 01 |
| internal resistance ← SOH | **not modelled** — never measured on this car; deriving it would make the calibration fixture depend on an invented law |
| lamps while off drawn from the 12 V battery, not the pack | **not modelled** — every load row stays observable in every ignition state; the 12 V drain is the honest half |
| door → interior lamp | **not modelled** — a few watts on the 12 V side, below anything the dashboard reads |

Two of these deserve their reasons spelled out.

**`lv_volts` is not forced to a 14 V bus.** The usual assertion is that the
DC-DC holds the 12 V system at 14.2–14.6 V whenever the car is READY or
charging. The owner's own 2026-08-24 capture reads **12.677 V on this byte
with the car READY**, and a model that contradicts the only measurement of a
value is worse than one that leaves it alone. So the knob stays the reported
figure, and what is coupled is the part nothing measured contradicts: the 12 V
battery **drains** while the car is OFF with lamps burning — leave the
headlights on and the `charge_12v` lamp eventually lights — and recovers
toward its resting figure while the converter runs.

**Range now costs what the accessories cost.** Usable energy is the same three
numbers the health tiles show; consumption is the ASSERTED 0.17 kWh/km of
driving *plus* the accessory draw charged at a nominal 45 km/h. 3 kW of A/C
takes about a quarter off the guess-o-meter, which is what the real dash does.

---

## The power button

The ZE0 has a push-button start, and the simulator emulates it:
`LeafModel.press_power(brake=None, hold=False)`, `Simulator.press_power()`,
`POST /sim/power {"brake": true}` (501 when the core has no power switch — the
Lancer has a key), and the round button on the cockpit. `start_state` stays a
directly settable knob: the button is a second, faithful way in, not a
replacement.

The rules are the 2012 Owner's Manual's, cited in the source (pp. 5-7..5-13):

| Now | Push | Result |
|---|---|---|
| OFF | no brake | ACC (p. 5-8) |
| ACC | no brake | ON |
| ON | no brake | OFF |
| OFF / ACC / ON, gear P or N | **brake held** | READY — "with the power switch in any position" (5-8, 5-11) |
| OFF / ACC / ON, gear R / D / Eco | brake held | refused, *"Shift to P or N"* (5-11: the EV will not operate unless the selector is in P or N; wording ASSERTED) |
| any, charge connector latched | brake held | refused, *"Remove charge connector"* (p. 2-19; wording ASSERTED). ACC and ON are still available |
| READY, stopped | either | OFF, **and the gear becomes P by itself** (5-11 step 4; the 5-13 NOTE: the vehicle automatically applies P when the power switch is OFF) |
| READY, moving | either | refused, *"Stop vehicle"* (ASSERTED, from 5-14) |
| READY, moving | `hold: true` | OFF — the emergency shut-off (ASSERTED, from 5-9), the only way OFF is reached while rolling |
| OFF / ACC / ON, moving | `hold: true` | the ordinary no-brake cycle — the 5-9 procedure stops the EV system *while driving*, so it presupposes READY. `speed_mph` is an independent knob, so an API caller can present a car that is OFF and "moving"; it does not get an emergency stop it is in no state to perform |

`brake` defaults to reading the `brake_pct` knob, so a cockpit that drives the
pedal needs no second control; pass `true`/`false` to override it. The reply is
`{"start_state", "gear", "message", "accepted", "messages"}` — `accepted` is
false for a refusal, and `message` is what the car would say. LOCK (5-13, after
OFF once a door is opened) is treated as OFF and is not modelled.

There is a knob for the connector itself: **`plugged_in`**, separate from
`charging` because a car can be plugged in without drawing (finished, or
waiting on a timer). Setting `charging` latches it; unplugging clears
`charging`. Both light the plug-in lamp and put "Charge connector connected"
on the dot-matrix line.

```bash
curl -s -X POST localhost:8099/sim/power -H 'content-type: application/json' \
     -d '{"brake": true}'
# {"start_state": "ready", "gear": "P", "message": "Ready to drive", "accepted": true, ...}
```

---

## Temperatures: °F everywhere

The house rule (`CLAUDE.md` §3) applies to the simulator without exception:
**every temperature `state()` or `record()` emits carries its `_f` twin** —
`pack_temp_c/f`, `temp_avg_c/f`, `temps_c/f`, `cabin_temp_c/f`,
`ambient_c/f`, `evap_c/f`, `charge_temp_limit_c/f`; the setpoint is the one
knob the car keeps in °F, so it gets a `hvac_setpoint_c` twin instead; the
record adds the decoders' own pairs (`hvac_ambient_c/f`, `hvac_evap_c/f`,
`hvac_target_c/f`). Knobs stay in the unit the car's own byte uses; the twin
is derived, never a second input. `tests/test_sim_loads.py` checks every
twin is present.

There is no sim-side string formatter, because nothing sim-side prints a
temperature: the rig's banner prints none, and every page formats its own.
`simulator/units.py` is conversions only (`c_to_f`, `f_to_c`). A `fmt_temp_f`
helper lived there and in `util.py` for a while on the assumption that some
printer would want it; it never gained a caller and was deleted rather than
shipped as an unused public function. (`util.fmt_temp`, which the older CLI
tools print as `34 °C / 93 °F`, is a different function with real callers and
is left alone on purpose.) The cockpit shows every temperature through the
dashboard's own `Tiles.fmtTemp` (`93°F / 34°C`) and edits every temperature
knob in °F with °C alongside.

---

## Iterating on the UI (start here if you are working on a chart or a report)

The simulator was built to inject faults. The thing it gets used for most is
different: **getting a database full of realistic history in seconds so the
way the data is drawn can be worked on.** Two commands, and you are looking at
six months of a car's life:

```bash
python hakake_sim.py --generate --days 180 --out /tmp/ui.db
python web/app.py --db /tmp/ui.db --no-reader --port 5001
# http://127.0.0.1:5001 — degradation, history, power, cells, A/C usage, all populated
```

180 days takes **tens of seconds** (about half a minute on the machine this
was written on; it is CPU-bound and sub-stepped) and produces roughly
**50,000 readings**, 68,000 per-cell rows and 3,900 events. Everything goes in through
`Store.insert_reading()` with the record shape the profile's `decode()`
produces, so every API route and every chart works unmodified — there is
nothing to teach the dashboard about generated data. The generator drives
the same load model as the live simulator (`simulator/history.py` sets
speed, pedals and HVAC and lets the model produce the current; it no longer
carries a load lump of its own).

### What is in a generated database

* **A daily rhythm.** Weekday commutes leaving around 07:00 and returning
  around 17:00, weekend errands, evening charges, long parked gaps, and about
  one day in ten where the car never moves. Not a sawtooth.
* **Charges with a real curve** (see "The charge curve" below), mostly Level 2
  with some Level 1 and the occasional DC fast session.
* **The long arcs the degradation chart exists for**: capacity fading on a
  `t^0.75` curve with per-day scatter, SOH tracking it, the cell spread slowly
  widening, and a seasonal ambient temperature the pack temperature follows.
* **Cells** four times a simulated day, and **events** for every A/C, gear,
  lock and door transition the profile watches — so the cell-drift view and
  the A/C on-time queries have something to chew on.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--generate` | — | do this instead of running a rig |
| `--days N` | 180 | days of history, ending now |
| `--out PATH` | `web/sim_history.db` | where to write (the real database is refused) |
| `--seed N` | 1 | same seed, same code and the same day, same database, byte for byte. The window ends *now*, so tomorrow's run of the same command lands on a different calendar and produces different counts |
| `--sample S` | 120 | seconds between rows while the car is doing something |
| `--idle-sample S` | 1800 | seconds between rows while it is parked |
| `--cells-per-day N` | 4 | full 96-cell reads per simulated day |
| `--json` | — | emit the summary as one JSON object |

### For an agent

```bash
python hakake_sim.py --generate --days 180 --out /tmp/ui.db --seed 1 --json
# {"rows": 49553, "cell_rows": 67968, "events": 3882, "drives": 332,
#  "charges": 145, "idle_days": 25, "path": "/tmp/ui.db",
#  "state_path": "/tmp/ui_state.json", "seed": 1, "days": 180,
#  "vehicle": "leaf_ze0", "synthetic": true, "start": "...", "end": "...",
#  "seconds": 31.6, ...}   # the counts move when the model does; the seed
#                          # only promises the same run from the same code

python web/app.py --db /tmp/ui.db --no-reader --port 5001 &
curl -s localhost:5001/api/health  | jq '.[0], .[-1]'    # the degradation chart's data
curl -s "localhost:5001/api/history?minutes=0" | jq 'length'
curl -s localhost:5001/api/status | jq '{soc, capacity_ah, simulated, generated}'
```

**Regenerate with the same seed after changing a chart** and any visual
difference is the chart's, because the data is identical — within the same
session, on the same code. The generated window ends at *now*, so a run on a
different day starts on a different weekday and the daily rhythm shifts.

`--generate` also writes a `<name>_state.json` beside the database, shaped
like the reader's state file, so `/api/status` has a last reading to serve
with no reader running. `--db` picks it up automatically.

### This data is synthetic, and it says so

There is a strong rule in this project about not confusing real and generated
data, and generated history is the easiest thing to get confused six months
from now. So:

* the `meta` table carries `synthetic = true`, a `warning`, the seed, the
  day count and the span;
* every row's `adapter` column is `sim-generated`, and its `extra` bag carries
  `simulated: true, generated: true`;
* `web/app.py --db` prints **SYNTHETIC DATABASE** on startup when it sees the
  meta flag, and the state file's `message` says the same;
* the generator **refuses** to write to `web/leaf_battery.db`, or to any file
  named like it, with an error rather than a warning.

If you ever have a database and cannot tell:

```bash
sqlite3 that.db "SELECT value FROM meta WHERE key='synthetic'"
```

Never merge a generated database into the real one. There is no supported way
to do it and there should not be.

---

## The charge curve

`charging` used to produce a flat current, and `scenarios/charge.json` faked a
taper by stepping `charge_kw` down at two scripted times. That was worse than
useless for UI work: a developer would design a chart around a discontinuity
no car produces. Charge power is now a function of **SOC**, and the shape is
the model's:

* constant power up to the taper knee;
* above it, `P = Pmax · (1 − x)^n` where `x` runs 0 → 1 from the knee to
  100 % SOC, falling smoothly to a trickle; the trickle floor holds flat and is
  retired over the last two points of SOC (`TRICKLE_END_SOC`) so the charge
  actually **finishes** instead of approaching 100 % as an asymptote — it used
  to scale with the same head as the curve above it, which meant the last point
  of SOC took as long as the one before it, forever;
* a `charger` knob for the four cases — `l1` (1.4 kW), `l2` (3.3 kW),
  `l2_66` (6.6 kW) and `dcfc` (44 kW CHAdeMO, which on a ZE0 tapers hard
  above 80 % and is thermally limited);
* pack temperature rising during a charge, steeply on DC fast, and the
  charge power derating when the pack gets hot (or is freezing).

**Modelled versus asserted.** `n ≈ 1.8` and the knee sliding down as SOH falls
were fitted to two real charge sessions in the owner's own database — his
35 %-SOH pack tapers from ~69 %, not 85 %, and the model reproduces that.
Everything about DC fast charge, and both derate slopes, is **asserted**: a
plausible shape with invented numbers, because nobody has put this car on a
CHAdeMO post with a logger. The full accounting is in the `CHARGERS` comment
in `simulator/model.py`; read it before quoting any of these numbers.

```bash
# watch a whole charge compress into a couple of minutes
python web/app.py --adapter sim --scenario full_charge
# or turn the knobs by hand
curl -s -X POST localhost:8099/sim/knobs -H 'content-type: application/json' \
     -d '{"charger": "dcfc", "charging": true, "soc": 30}'
```

`charger` re-seats `charge_kw` and the taper knobs, so set it **first** if you
also want to override the power.

---

## Flags

| Flag | Where | Meaning |
|---|---|---|
| `--scenario NAME` | both | a shipped scenario name (below) or a JSON file |
| `--seed N` | both | same seed, same run |
| `--knob NAME=VALUE` | both | startup knob; repeatable |
| `--speed X` | both | effective clock scaling — `--speed 60` runs an hour of battery drain in a minute. **Overrides** a scenario's `clock_scale`; see [Compressing time](#compressing-time) |
| `--vehicle NAME` | both | the profile (`leaf_ze0`, `lancer_2009`) |
| `--sim-control PORT` | `app.py`, `reader.py` | control API port; **default 8099 in sim mode**, `0` = a free port. With `--sim-serial` it names the port the external rig already serves on, so the pages can link to it |
| `--no-sim-control` | `app.py` | no control API |
| `--sim-serial DEV` | `app.py`, `reader.py` | talk to a `--pty` rig over real serial |
| `--pty` | `hakake_sim.py` | publish a pseudo-terminal |
| `--launch-dashboard [PORT]` | `hakake_sim.py` | with `--pty`: also start `web/app.py` against it (default port 5055), stopped when the rig stops |
| `--control PORT` / `--no-control` | `hakake_sim.py` | control API (default port 8099) |
| `--json` | `hakake_sim.py` | one JSON object per line instead of prose; the first is a `ready` object with `pty`, `control`, `dashboard_command`, `panel_url`, `scenario`, `seed`, `time_scale` and a `warning` |
| `--report S` / `--duration S` | `hakake_sim.py` | status line period (0 = never) / exit after S seconds |
| `--dump-schema` / `--dump-state` | `hakake_sim.py` | print and exit |

---

## Driving this from an agent

An agent must be able to use the simulator without reading any source. Three
things make that true: a machine-readable **schema**, a **control API** that
changes conditions mid-run, and **errors that say what to do instead**.

### 1. Discover what you can change

```bash
python hakake_sim.py --dump-schema | jq '.knobs | keys'
```

Sixty-odd knobs for the Leaf, twenty-eight for the Lancer, each with `type`,
`unit`, `min`, `max`, `default`, a `category`, a human `label` and a one-line
`help`. Never guess a knob name — list them.

```json
{
  "soc":                 {"type": "float", "unit": "%",  "min": 0, "max": 100,
                          "default": 85.0, "category": "battery",
                          "label": "State of charge", "help": "State of charge, %"},
  "fault.cell_degraded": {"type": "bool",  "unit": "", "category": "faults",
                          "label": "Degraded cell pair", "default": false,
                          "help": "One cell pair collapses (-350 mV at weak_cell_index) and the spread widens"}
}
```

`category` is the model's own grouping — `battery`, `load`, `climate`, `body`,
`rig`, `faults` on the Leaf; `engine`, `diagnostics`, `rig`, `faults` on the
Lancer — and every knob has one. It exists so a UI can lay out the control
surface without a hardcoded knob list; the cockpit is built entirely on it.
A knob whose name starts with `fault.` is always in `faults`. `label` is the
title a UI shows (`Low beam`, not `headlights`); it is derived from the first
clause of `help` when a knob does not declare one.

### 2. Start it

```bash
python web/app.py --adapter sim --port 5055 &
# dashboard :5055, cockpit :5055/sim, control API :8099
```

or run the rig separately and point the dashboard at its pty:

```bash
python hakake_sim.py --pty --json --control 8099 --report 0 > /tmp/sim.log &
PTY=$(head -1 /tmp/sim.log | jq -r .pty)
python web/app.py --adapter sim --sim-serial "$PTY" --sim-control 8099 --port 5055 &
# or simply: python hakake_sim.py --pty --launch-dashboard
```

### 3. The control API

Loopback only (`127.0.0.1`), stdlib `http.server`, no authentication, no new
dependency. Every JSON response carries `"simulated": true` and
`Access-Control-Allow-Origin: *` (the cockpit calls it from the dashboard's
origin); `OPTIONS` answers 204 with the three CORS headers and **no body** — a
204 that carries one may make a browser reject the preflight and with it the
real request, which happened once and is now pinned by
`tests/test_sim_control.py`.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/sim/schema` | — | every knob: type, unit, range, default, category, label, help |
| GET | `/sim/state` | — | the model's own state (`state()`), incl. `power`, `loads_w`, `lamps`, `messages` |
| GET | `/sim/record` | — | the state in the **dashboard's vocabulary** (`record()`: decoder keys, every `_f` twin, `lamps`, `lamps_unmodelled`, `messages`, `loads_w`) — **501** when the core has no `record()` |
| GET | `/sim/knobs` | — | current knob values |
| GET | `/sim/faults` | — | active faults |
| GET | `/sim/info` (also `/sim`) | — | vehicle, seed, scenario, the **effective time scale** and its source, `speed_override`, `time_scale_max`, the scenario list, endpoint list |
| GET | `/sim/scenarios` | — | the shipped scenario names, and the current one |
| GET | `/health` (also `/sim/health`) | — | `{"ok": true, "simulated": true}` |
| GET | `/`, `/panel`, `/sim/panel` | — | the **fallback panel** (HTML) |
| POST | `/sim/knobs` | `{"soc": 20, "fault.cell_degraded": true}` | `{"applied": {...}}` |
| POST | `/sim/scenario` | `{"name": "drive"}` or `{"path": "..."}` | the loaded scenario and its knobs |
| POST | `/sim/scenario` | `{"name": ""}` (or `null`) | **clears** the scenario: the timeline stops, every knob stays where it is, the model free-runs. 400 on a core with no `clear_scenario()` |
| POST | `/sim/power` | `{"brake": true}` | one push of the car's power switch: `{"start_state", "gear", "message", "accepted"}` — **501** when the core has no `press_power()`. See [The power button](#the-power-button) |
| POST | `/sim/reset` | `{}` | every knob back to its schema default |
| POST | `/sim/step` | `{"dt": 30}` | advance 30 **real** seconds (the clock scale applies, as it does live), 0–86400 |
| POST | `/sim/step` | `{"sim_seconds": 3600}` | advance 3600 **simulated** seconds whatever the clock scale is — a "skip ahead"; 0–7 days; `dt` and `sim_seconds` together is a 400 |

`/sim/record` is answered per request, not decided at startup: a core that
cannot speak the vocabulary (the Lancer, today) gets a 501 that says so, the
cockpit falls back to `/sim/state` and draws knob cards only. A 200 there is
what lights the cluster, the head unit and the four tiles.

A bad knob name is a **400 with near matches**, never a 500:

```bash
curl -s -X POST localhost:8099/sim/knobs -H 'content-type: application/json' -d '{"sock": 20}'
{"error": "unknown knob(s): sock",
 "unknown": ["sock"],
 "suggestions": {"sock": ["soc", "locked", "soh", "clock_scale", "sunload"]}}
```

Knobs apply all-or-nothing: if one name is wrong, none of them are applied, so
a retry after fixing the typo is not a partial write.

### 4. Worked example — start, drop SOC to 15 %, degrade a cell, confirm

This is the run to copy. The numbers are from a real run of it over the pty
path (the in-process path gives the same shape with different rounding).

```bash
# start the rig behind a real pty, and the dashboard in front of it
python hakake_sim.py --pty --json --control 8099 --scenario drive --seed 1 --report 0 > /tmp/sim.log &
sleep 3; PTY=$(head -1 /tmp/sim.log | jq -r .pty)          # -> /dev/ttys005
python web/app.py --adapter sim --sim-serial "$PTY" --sim-control 8099 --port 5055 > /tmp/app.log &
sleep 20

# before
curl -s localhost:5055/api/status | jq '{soc, cell_spread, cell_min, insulation_kohm}'
# { "soc": 76.87, "cell_spread": 30, "cell_min": 3925, "insulation_kohm": 885 }

# change the car, mid-run, while the dashboard is polling it
curl -s -X POST localhost:8099/sim/knobs -H 'content-type: application/json' \
     -d '{"soc": 15, "fault.cell_degraded": true, "fault.insulation_low": true}'
# {"applied": {"fault.cell_degraded": true, "fault.insulation_low": true, "soc": 15.0},
#  "simulated": true}

sleep 25

# after — the dashboard, not the simulator, is being asked
curl -s localhost:5055/api/status \
  | jq '{soc, cell_spread, cell_min, cell_min_idx, insulation_kohm, simulated, sim_transport}'
# { "soc": 12.75,          <- 15 %, then coulomb-counted down under load
#   "cell_spread": 360,    <- was 30 mV
#   "cell_min": 3290,
#   "cell_min_idx": 55,    <- the degraded cell, by index
#   "insulation_kohm": 18, <- was 885
#   "simulated": true,
#   "sim_transport": "serial /dev/ttys005" }
```

Note the last two fields. Everything that came out of `/api/status` says it is
generated, and says how.

### 5. Reading state without the dashboard

`GET /sim/state` is the model's own view; `GET /sim/record` is the same state
in the decoders' vocabulary; `/api/status` is what survived encoding, the
wire, and the decoders. **Compare them** — that is the round trip the
simulator exists for, and a mismatch is a decoder bug (or an encoder bug,
which is why it is only a consistency check). `tests/test_sim_record.py`
holds `record()` equal to `decode(encode(state()))` on every key the real
decoders also produce.

### 6. Reproducibility

Same `--seed`, same `--scenario`, same knob sequence, same run. If you need a
run to be repeatable, drive time with `POST /sim/step` rather than with
`sleep`; wall-clock advance depends on how fast the machine happened to be.

---

## Scenarios

Declarative JSON in `simulator/scenarios/`, and shipped by name:

| Scenario | For looking at |
|---|---|
| `idle` | the 2026-08-24 baseline, parked and READY |
| `drive` | the original pull-away / cruise / stop |
| `commute` | a drive with *texture* — surface streets, a freeway leg, two regen events, lights |
| `charge` | a Level 2 charge from 40 %, tapering on the model's own curve |
| `full_charge` | the whole curve on a healthy pack: long flat phase, high knee, trickle to full |
| `dc_fast` | CHAdeMO: 44 kW, hard taper past 60 %, the pack heating into its derate |
| `degradation_arc` | two years of ageing walked through in a couple of minutes |
| `degraded_pack` | the failure the simulator was built for: a collapsed cell pair |
| `lancer_idle`, `lancer_dtc` | the other profile |

For a *populated* degradation chart use `--generate`; `degradation_arc` walks
the live tiles through the range but cannot write months of history.

The shape (illustrative — the shipped `drive.json` has more steps and injects
no fault):

```json
{"name": "example", "vehicle": "leaf_ze0", "seed": 1,
 "knobs": {"soc": 80, "ambient_c": 22},
 "timeline": [{"t": 0,  "set": {"gear": "D", "speed_mph": 0}},
              {"t": 10, "set": {"speed_mph": 45, "accel_pedal_pct": 25}},
              {"t": 60, "set": {"fault.cell_degraded": true}}]}
```

`t` is seconds of simulated time from the start. Scenarios drive **speed and
pedals**, and let the load model produce the current; one that sets
`current_a` is adding to it. Load one mid-run with `POST /sim/scenario`,
clear it with `{"name": ""}`, or pass it at startup with `--scenario`.

### Compressing time

There are two ways to ask for a faster clock and **exactly one of them wins**.
They used to multiply, which was a mistake and an expensive one — see the note
below.

| You give | Effective multiplier |
|---|---|
| `--speed X` | **X.** It overrides the scenario's `clock_scale`; it does not multiply it. |
| nothing, but the scenario sets `clock_scale` | the scenario's `clock_scale` |
| neither | 1.0 — real time |

**Every shipped scenario runs at real time**, with one exception. A scenario
that carries its own clock decides how fast you see it whether you wanted that
or not, and the number then has to be discovered and overridden; `--speed` and
the cockpit's *Simulated time* card are how time gets compressed, so the clock
is always one you chose. The exception is `degradation_arc`, which keeps
`clock_scale: 3600` — one simulated hour per real second — because two years
of ageing at 1× is two years of waiting, and its description says so in
capitals.

`clock_scale` is still a live knob: with no `--speed` in play, `POST /sim/knobs`
(or the cockpit's *Clock speed* buttons) changes the clock mid-run. With
`--speed` in play the knob keeps its value and is simply not the one being
used; the rig says so on the banner and the cockpit locks the control with
the reason.

The effective multiplier is **clamped to 0.01 – 3600×** and is reported in
four places, so it can never be a surprise:

* the startup banner — `clock: 120.0x simulated time  (--speed (overrides …))`
* `GET /sim/info` — `time_scale`, `time_scale_source`, `speed_override`,
  `clock_scale`, `time_scale_max`
* `sim.state()` — `time_scale` and `time_scale_source`, so anything reading
  the model sees it too
* the cockpit's *Simulated time* card

Skipping ahead is a different thing from running faster: `POST /sim/step
{"sim_seconds": N}` jumps the model N simulated seconds once, whatever the
multiplier — the cockpit's *Skip ahead* buttons — while `{"dt": N}` is N
real seconds with the multiplier applied, which is what the live transport
does every cycle.

```bash
# real time, like every shipped scenario bar degradation_arc
python hakake_sim.py --scenario charge --pty

# compress it yourself: 120x puts the five-hour charge into two and a half minutes
python hakake_sim.py --scenario charge --speed 120 --pty
#   clock:    120.0x simulated time  (--speed)

# the one that ships with a clock, overridden
python hakake_sim.py --scenario degradation_arc --speed 30 --pty
#   clock:    30.0x simulated time  (--speed (overrides the scenario's clock_scale))
#   note:     --speed 30.0 overrides clock_scale 3600.0 from scenario 'degradation_arc'; …

python web/app.py --adapter sim --scenario charge --speed 120
```

Each scenario's description says how long it takes at 1× and suggests a
`--speed` that puts it into a couple of minutes.

> **Why it works this way (2026-09-02).** `--speed 120` on the `charge`
> scenario, which carries `clock_scale: 120`, used to give an effective
> **14400×**. Eight seconds of startup was thirty-two simulated hours; the
> charge finished before the first sample and the pack "reached" 80 °C on a
> 3.3 kW Level 2 charge. Two bugs in one: a silent product, and an
> integrator that could not survive it (see *Time scale cannot break the
> model*, below). The command line is the human in the room, so it wins;
> and the number it resolves to is now printed rather than inferred.

### Time scale cannot break the model

The 80 °C pack was not a clamp and not a bad constant — it was forward Euler
exploding. The model integrates with a fixed step, and a single `step(dt)` was
being handed hours at a time; the pack's relaxation term,
`(T − ambient) · dt / 1800`, flips sign and grows once `dt > 3600`.

`step()` now **sub-steps**: it chops the interval into pieces of at most
`MAX_SUBSTEP_S` (5 s, in `simulator/model.py`) and loops, for every integrated
quantity — SOC, pack temperature, cabin, evaporator, odometer and `lv_volts`
on the Leaf (`INTEGRATED` in `simulator/model.py`), the Lancer's coolant,
fuel and run timer. So `step(3600)` and 3600 calls to
`step(1)` land in the same place, and no time scale changes the *size* of an
integration step, only how many of them a wall-clock second buys.

`tests/test_sim_stability.py` pins both halves. Equivalence is asserted to
0.1 % SOC, 0.05 °C on the pack, 0.5 °C on cabin and evaporator (the fastest
states, τ = 60–90 s, and both integers in `state()` anyway) and 0.01 mi of
odometer, over durations up to eight hours and at every chunk size from 0.25 s
to a single call. Boundedness is asserted by walking the extremes of every
knob that feeds the thermal model — including combinations no car can be in,
like 400 A through a 2 Ω pack — at 3600× and requiring the pack to stay inside
−40…70 °C.

Scenario timeline entries also fire *per sub-step* now rather than all firing
up front, so a compressed clock plays a timeline in order instead of
collapsing it.

The cost is about 3 µs per sub-step: `--generate --days 180` roughly doubled
in wall-clock time, which is the right trade.

---

## What is where

| Piece | File |
|---|---|
| The model: knobs, load table, charge curve, `lamps()`, `record()` | `simulator/model.py` (Leaf), `simulator/lancer.py` |
| The wrapper the rig and transports drive (`make_sim`, `Simulator`) | `simulator/__init__.py` |
| Knob registry, `label` / `category` | `simulator/knobs.py` |
| °C/°F helpers | `simulator/units.py`, `util.c_to_f` / `util.fmt_temp` |
| The encoder (state → UDS / CAN bytes) | `simulator/encode.py` |
| Bulk history generation | `simulator/history.py` |
| Scenarios | `simulator/scenarios/*.json` |
| The transports (`SimELM`, `SimSerialELM`), control-port fallback | `elm327.py` |
| The rig: pty, control API, `--launch-dashboard`, CLI | `hakake_sim.py` |
| The fallback panel on the API port | `simulator/panel.html` |
| The cockpit page | `web/templates/sim.html`, `web/static/sim.js`, `web/static/sim.css` |
| The tiles the cockpit and dashboard share | `web/templates/tiles/*.html`, `web/static/tiles.js`, `web/static/tiles.css`, `web/static/tilestudio.js` |
| `--adapter sim`, `/sim`, `/api/sim/tiles`, the banner | `web/app.py`, `web/reader.py` |
| Tests | `tests/test_simulator.py`, `test_sim_loads.py`, `test_sim_record.py`, `test_sim_lamps.py`, `test_sim_charge.py`, `test_sim_history.py`, `test_sim_stability.py`, `test_sim_timescale.py`, `test_sim_transport.py`, `test_sim_control.py`, `test_sim_launch.py`, `test_sim_panel.py`, `test_sim_page.py` |
| A contract-only stand-in core | `tests/sim_stub.py` |
| The contract everything above is built against | `docs/SIMULATOR_CONTRACT.md` |

`tests/sim_stub.py` implements the contract in about 200 lines, without
`record()`. The transport, the control API and both pages are tested against
**it** as well as the real core, so they cannot quietly grow a dependency on
the core's internals — and the stub is how the 501 path is exercised without
a Lancer.

## Safety rules, restated

- Auto-detect **never** picks the simulator. `--adapter sim` must be asked
  for, the same rule replay follows, for the same reason: a dashboard with a
  dead adapter must show a dead adapter, not a healthy imaginary car.
- Simulated rows go to `web/sim_<profile>.db` and `web/sim_<profile>_state.json`
  (both gitignored). `web/leaf_battery.db` is never opened in this mode.
- Generated history goes to `--out` (default `web/sim_history.db`, gitignored).
  The generator refuses `web/leaf_battery.db` and anything named like it, and
  every generated database is stamped synthetic in its `meta` table. Generated
  data is **never** merged into the real database.
- The simulator is **read-only about the car** in the only sense that applies:
  there is no car. `SECURITY.md`'s rule is untouched — nothing here writes to
  a vehicle, because nothing here is connected to one.
- The control API binds `127.0.0.1` and has no authentication. It can put a
  fault on a dashboard someone is reading. Do not expose it.
