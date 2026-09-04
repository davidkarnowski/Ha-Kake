# Simulator contract

The interface between a simulator core and everything that drives it — the
transports in `elm327.py`, the rig and control API in `hakake_sim.py`, the
history generator, the cockpit page, and any sub-agent building one of those.
It stays a separate document on purpose: `docs/SIMULATOR.md` is the guide to
*using* the simulator; this is what a core must provide and what a caller may
assume. `tests/sim_stub.py` implements exactly this and nothing more, and the
transports, the control API and both pages are tested against it as well as
the real core, so a caller cannot quietly depend on the Leaf model's
particulars.

## Package

`simulator/` at the repo root.

```python
from simulator import make_sim, KNOBS

sim = make_sim(vehicle="leaf_ze0", knobs={"soc": 42.0}, seed=1, scenario=None)
```

## The object

| Call | Returns | Notes |
|---|---|---|
| `sim.step(dt)` | `None` | Advance the physical model by `dt` seconds of **real** time; `dt × sim.time_scale()` seconds of simulated time pass. Deterministic for a given seed, and independent of how the caller chops up the interval — see *The clock* below. |
| `sim.state()` | `dict` | Full machine-readable model state. JSON-serialisable, flat-ish, stable key names. |
| `sim.set(**knobs)` | `dict` of applied values | Apply knobs at runtime. Unknown knob → `ValueError` naming it and suggesting near matches. |
| `sim.get_knobs()` | `dict` | Current value of every knob. |
| `sim.knob_schema()` | `dict` | Machine-readable description of every knob: type, unit, range, default, one-line help. This is how an agent discovers what it can do without reading source. |
| `sim.respond(cmd, tx, rx)` | `list[str]` | ELM327 response lines for a UDS/AT command at the given header pair. Unknown request → `["NO DATA"]`, never invented data. |
| `sim.frames(can_id, secs)` | `list[str]` | Passive monitor lines for one CAN id over `secs` of simulated time. |
| `sim.load_scenario(path_or_name)` | `None` | Apply a scenario (see below). |
| `sim.faults()` | `dict` | Currently active faults. |
| `sim.time_scale()` | `float` | The effective multiplier: simulated seconds per real second. |
| `sim.time_scale_info()` | `dict` | `time_scale`, `source`, `speed_override`, `clock_scale`, `clamped`, `min`, `max` — so nothing has to infer it. |
| `sim.set_speed(x, outer=1.0)` | `float` | Install (or clear, with `None`) an explicit `--speed` override. |
| `sim.record(cells=True)` | `dict` | **Optional per core.** The state in the dashboard's own vocabulary — every key the profile's `decode()` produces (`door_driver`, `hvac_ambient_f`, `hvac_target_c`, `range_mi`, `soh_dash_pct`, `brake_on`, `doors_raw` …, every `_f` twin) plus `lamps`, `lamps_unmodelled`, `messages`, `loads_w` and the marker fields `simulated`, `vehicle`, `seed`, `scenario`, `time_scale`, `faults`. Held to `record() == decode(encode(state()))` by `tests/test_sim_record.py`. A core without one raises `NotImplementedError` (`sim.can_record()` says which) — a control server answers 501 and a page draws no vehicle tiles for that profile. |
| `sim.can_record()` | `bool` | Whether `record()` will answer. A control server checks this (or catches the `NotImplementedError`) per request, never at startup. |
| `sim.press_power(brake=None, hold=False)` | `dict` | **Optional per core** (Leaf today). One push of the car's power switch: returns `{"start_state", "gear", "message", "accepted"}` plus the marker fields. The rules are the 2012 Owner's Manual's — no brake cycles OFF → ACC → ON → OFF, the brake reaches READY only from P or N, a press while READY switches off and applies P, a latched charge connector refuses READY — and are documented in `docs/SIMULATOR.md`. `start_state` stays a directly settable knob. A core without one raises `NotImplementedError` (`sim.can_press_power()` says which); `POST /sim/power` answers 501 and a page draws no button. |
| `sim.can_press_power()` | `bool` | Whether `press_power()` will answer. Checked per request, like `can_record()`. |
| `sim.clear_scenario()` | `None` | **Optional per core.** Drop the timeline and the scenario name; every knob stays where it is (free-running from here). `POST /sim/scenario {"name": ""}` is a 400 that says so on a core without it. |
| `sim.model.lamps()` | `dict` | **Optional per core** (Leaf today). Every ZE0 cluster indicator the model can drive, as booleans, plus the aggregates `master_red` / `master_yellow`. `LAMPS_UNMODELLED` (ABS, VSP, brake, PS, shift control, VDC, seat belt, air bag) are always `False` and listed by `lamps_unmodelled()` — never faked. `messages()` is the dot-matrix line, in display order. All three ride inside `state()` and `record()` as `lamps`, `lamps_unmodelled`, `messages`; a page draws a cluster only when the record carries them. |
| `sim.seed` / `sim.vehicle` | attrs | Introspection. |

**Optional versus required.** `step`, `state`, `set`, `get_knobs`,
`knob_schema`, `respond`, `frames`, `load_scenario`, `faults`, the clock
calls and `seed` / `vehicle` are required — the stub has them all.
`record()` / `can_record()`, `press_power()` / `can_press_power()`,
`clear_scenario()` and `lamps()` / `messages()` are optional: a caller feature-detects them with `getattr` and
degrades (501, 400, no cluster), never guesses. That is how a Lancer core
hides the Leaf tiles with no profile code.

`KNOBS` is the module-level registry; `sim.knob_schema()` derives from it.
Knobs are **per vehicle** — a Leaf has no `rpm` and a Lancer has no
`cell_spread_mv` — so `KNOBS` is keyed by profile name: `KNOBS["leaf_ze0"]["soc"]`.
`sim.knob_schema()` returns the flat schema for *that sim's* vehicle.

## Knobs

Every condition worth simulating is a knob. Settable at construction, from the
CLI, and at runtime. Each declares: `type`, `unit`, `min`, `max`, `default`,
`help`, a `category` (`battery`, `load`, `climate`, `body`, `rig`,
`faults`, …) so a UI can group the control surface without a hardcoded knob
list, and a `label` — a human title ("Low beam", not `headlights`), at most
40 characters, never the raw name; derived from the first clause of `help`
when a knob does not declare one. `knob_schema()` emits every one of those
fields per knob, `label` and `category` included (`simulator/knobs.py`;
`tests/test_sim_lamps.py` checks every knob of every profile has one that differs from its name and fits).
Anything named `fault.*` is in `faults`. A caller must not assume more than
this: the stub's schema has no `category` and calls its text type `str`, and
the pages still render against it.
Required coverage for `leaf_ze0`:

**Battery** `soc`, `capacity_ah`, `soh`, `hx`, `pack_temp_c`, `cell_spread_mv`,
`weak_cell_index`, `weak_cell_offset_mv`, `internal_resistance_ohm`,
`lv_volts`, `insulation_kohm`

**Load and motion** `current_a`, `load_kw`, `speed_mph`, `gear`,
`accel_pedal_pct`, `brake_pct`, `charging` (+ `charge_kw`)

`current_a` **keeps its name and changed its meaning** (2026-09-03): it is the
*extra* pack current added on top of the modelled loads, default `0`. Pack
current is a sum — `I = (P_charge + P_regen − Σ loads) / V + current_a` —
where the loads are the switched-on rows of `simulator.model.LOADS_W` (READY
electronics, lamps, blower, A/C, PTC heater, motor), each labelled MEASURED
or ASSERTED in the source. HVAC and the motor draw only while
`start_state == "ready"`. `load_kw > 0` remains an absolute override that
bypasses the whole model (for tests that want an exact number). Setting
`current_a` to what used to be the whole draw now double-counts; scenarios
drive `speed_mph` / `accel_pedal_pct` / `brake_pct` and the HVAC knobs
instead.

**Climate** `hvac_on`, `hvac_ac_on`, `hvac_fan_speed`, `hvac_setpoint_f`,
`cabin_temp_c`, `ambient_c`, `evap_c`, `heater_level`

**Body** `doors` (per corner), `locked`, `headlights`, `high_beam`,
`parking_lights`, `fog_lights`, `turn_signal`, `handbrake`, `odometer_mi`,
`tpms_psi` (four)

**Rig** `noise` (sensor jitter amplitude), `clock_scale`. (`ambient_c` is a
*climate* knob — it is listed above, not here.)

**Faults** — the point of the whole exercise, since these cannot be produced
on demand in a real car: `fault.cell_degraded`, `fault.insulation_low`,
`fault.lv_battery_weak`, `fault.sensor_dropout`, `fault.car_asleep`,
`fault.adapter_silent`, `fault.bus_noise`, `fault.ecu_nrc`.

`lancer_2009` needs its own smaller set (`rpm`, `coolant_temp_c`, `load_pct`,
`throttle_pct`, `maf_gs`, `map_kpa`, `iat_c`, `fuel_pct`, `module_v`,
`mil_on`, `dtc_stored`, plus relevant faults).

## Scenarios

Declarative JSON in `simulator/scenarios/`, shipped by name (`idle`, `drive`,
`commute`, `charge`, `full_charge`, `dc_fast`, `degradation_arc`,
`degraded_pack`, `lancer_idle`, `lancer_dtc`). Shape:

Shape (illustrative; the shipped `drive.json` has more steps and injects no
fault):

```json
{"name": "example", "vehicle": "leaf_ze0", "seed": 1,
 "knobs": {"soc": 80, "ambient_c": 22, "gear": "P", "start_state": "ready"},
 "timeline": [{"t": 0,  "set": {"gear": "D", "handbrake": false}},
              {"t": 15, "set": {"speed_mph": 45, "accel_pedal_pct": 35}},
              {"t": 60, "set": {"fault.cell_degraded": true}}]}
```

`t` is seconds of simulated time from start; entries fire per sub-step, in
order, whatever the clock scale. A scenario drives `speed_mph`,
`accel_pedal_pct`, `brake_pct` and the HVAC knobs and lets the load model
produce the current; one that sets `current_a` is adding to it (see
*Load and motion* above). `clear_scenario()` stops the timeline and leaves
the knobs where they are.

## Physical honesty

The model is an ordinary equivalent-circuit approximation, not a validated
battery model:

- SOC by coulomb counting: `dSOC = I·dt / capacity_ah`
- Terminal voltage: `V = 96 · OCV(soc) + I · R₀`, so it sags under load.
  (Corrected 2026-09-02: with the house sign convention — negative is
  discharging — a minus sign here would *raise* the pack voltage under
  load. The implementation adds.)
- Pack temperature: I²R heating relaxing toward ambient
- Cell voltages: pack mean plus a per-cell offset, spread widening with age
- Loads: a table of watts per consumer with provenance (`LOADS_W`); READY
  idle 150 W and the beam deltas are measured (kelvin shunt, mynissanleaf
  Lab Test thread), the lamps and blower are asserted, A/C and PTC are the
  owner's own reports, the motor and regen are asserted shape awaiting the
  driving capture. `state()` reports `loads_w`, `motor_kw`, `regen_kw`,
  `hvac_kw`, `wall_kw` (pack-side charge power ÷ `CHARGE_EFF`, L1/L2 measured)
- **One power identity** (added 2026-09-03): `LeafModel.power()` is the only
  place the budget is summed — `wall_kw → charger_kw → loads_kw → pack_kw =
  charger + regen − loads + extra` — and `current()` is `pack_kw / V_ocv` and
  nothing else. `state()` and `record()` carry it as `power`, plus `pack_kw`
  and `loads_total_w` at the top level. Accessories draw **while charging**
  too (HVAC on the plug, and a `base_charging` row), which is what the
  identity exists to keep honest. Every cross-influence the model does or does
  not implement is listed with its reason in `COUPLINGS` in
  `simulator/model.py`; see docs/SIMULATOR.md.
- **Temperatures: every `x_c` the state or record emits has an `x_f` twin**
  (the house rule — °F shown, °C alongside). Knobs stay in the car's own
  unit; the twin is derived, never a second input.

## The clock

Two things ask for compressed time and **exactly one wins**; they do not
multiply. (They did until 2026-09-02, and `--speed 120` on a scenario carrying
`clock_scale: 120` ran at 14400×.)

- an explicit `--speed X` → the effective multiplier is `X`, overriding the
  scenario's `clock_scale` knob;
- otherwise the `clock_scale` knob, which stays live and settable mid-run;
- otherwise 1.0.

Clamped to 0.01 – 3600×. The resolved number is reported by
`sim.time_scale()`, in `sim.state()["time_scale"]`, in `GET /sim/info` and on
the startup banner: a time scale must never be something a caller has to infer.

**The model must not care how time is chopped up.** `step(3600)` and 3600
calls to `step(1)` land in the same place: `step()` sub-steps internally
(`simulator.model.MAX_SUBSTEP_S`, 5 s) so no time scale can drive a forward
Euler integrator unstable. Tolerances are pinned in
`tests/test_sim_stability.py`.

## Physical honesty, continued

**A simulator verifies consistency, not truth.** A round trip
(model → encode → decode → compare) pins the decoders against an explicit
spec and makes refactors safe, but it cannot catch an error shared by encoder
and decoder — it would not have caught the ÷1024-vs-×0.001 scale bug. Say so
in the docs; never let a green simulator run be mistaken for verification
against a real car.

## Agent usability — a hard requirement, not a nice-to-have

- `sim.knob_schema()` and `hakake-sim --dump-schema` emit JSON so an agent
  discovers every knob without reading source.
- A control surface lets an agent change conditions **mid-run** and watch the
  dashboard react. That is the whole point.
- Bad knob names fail loudly with near-match suggestions.
- Same seed, same run: reproducible.
- Every value the simulator produces is labelled as simulated wherever it
  surfaces, so no one can mistake it for a real reading.
