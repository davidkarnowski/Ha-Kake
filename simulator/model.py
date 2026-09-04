# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Physical model — 2012 Nissan Leaf (ZE0).

Pure Python, no I/O, deterministic for a given seed. State advances by
`step(dt)`; everything else is derived on demand.

This is an ordinary equivalent-circuit approximation, **not a validated
battery model**. It exists so conditions that cannot be produced on demand in
a real car (a degraded cell pair, low insulation, a sleeping ECU) can be put
in front of the dashboard and the decoders. Plausible ranges matter here;
precise physics does not.

Calibration
-----------
The OCV curve is fitted so the simulator lands on the owner's real car:
`tests/fixtures/lbc_raw_20260824.json` reads 383.87 V at 76.87 % SOC with
23.16 Ah of an original 66 Ah (35.1 % SOH), cell mean 3999 mV, spread 30 mV,
pack 34-36 °C. Feeding those numbers in as knobs reproduces that fixture to
within a few hundredths of a volt — see tests/test_simulator.py.

Sign convention (the house rule, docs/SIGNALS.md): current is NEGATIVE while
discharging, positive while charging/regenerating; power follows.

Where the current comes from
----------------------------
Pack current is a *sum*, not a knob: every switched-on consumer in LOADS_W
(READY electronics, lamps, blower, A/C, PTC heater, the motor) draws its watts
from the pack, charging and regen put watts back, and

    I = (P_charge + P_regen − Σ loads) / V_ocv  +  current_a

where `current_a` is an *extra* current on top of the modelled loads (default
0). `load_kw` > 0 is an absolute override that bypasses all of this, kept for
tests that want an exact number. See LOADS_W for what is measured and what is
asserted. The 2026-09-03 drive corroborated Eco coast regen; road load, the
pedal term and brake regen are still asserted — see the motor note for why a
13-minute drive over unknown terrain cannot settle them.

Temperatures
------------
Every temperature `state()` and `record()` emit carries its °F twin (`x_c`
and `x_f`), the house rule. Knobs stay in the unit the car's own byte uses.
"""

import random

from .knobs import KnobSet
from .units import c_to_f, f_to_c

NUM_CELLS = 96
NOMINAL_CAPACITY_AH = 66.0

# ── OCV per cell pair (V) against SOC (%) ────────────────────────────────
# Lithium-manganese, 96 pairs in series. Fitted to the owner's car: 76.87 %
# -> 4.000 V/pair -> 384.0 V open-circuit, 383.9 V under the 1.5 A idle draw.
OCV_TABLE = [
    (0.0, 3.300), (5.0, 3.550), (10.0, 3.650), (20.0, 3.750), (30.0, 3.830),
    (40.0, 3.880), (50.0, 3.915), (60.0, 3.950), (70.0, 3.980), (77.0, 4.000),
    (85.0, 4.030), (90.0, 4.052), (95.0, 4.082), (100.0, 4.115),
]

GEARS = ("P", "R", "N", "D", "Eco")
# The push-button start with the brake up: one push ACC, two ON, three OFF
# (2012 Owner's Manual p. 5-8). READY is only reachable with the brake down,
# and a press while READY switches the car off — both in `press_power()`.
POWER_CYCLE = {"off": "acc", "acc": "on", "on": "off"}
POWER_MESSAGES = {"acc": "Accessory", "on": "Ignition on", "off": "Power off"}
TURN = ("off", "left", "right", "hazards")
START_STATES = ("off", "acc", "on", "ready")
DOOR_NAMES = ("driver", "pass", "rl", "rr", "hatch")
TPMS_ORDER = ("fl", "fr", "rr", "rl")          # the order 0x385 carries them

# 0x60D byte 0 door bits (verified 2026-08-25 door walk). The encoder and the
# record builder both read this, which is why it lives here and not in encode.
DOOR_BITS = {"driver": 0x08, "pass": 0x10, "rl": 0x20, "rr": 0x40, "hatch": 0x80}

# Blower motor volts per fan speed, from the 2026-08-24 fan walk (byte 11 of
# HVAC group 10 read 84 85 86 88 89 8B 8B for speeds 1-7). 6 and 7 both read
# 11 V on this car, which is why the tile says "6-7". The encoder puts this on
# the wire; the load table turns it into watts.
FAN_VOLTS = {0: 0, 1: 4, 2: 5, 3: 6, 4: 8, 5: 9, 6: 11, 7: 11}

MPH_TO_MS = 0.44704
KMH_PER_MPH = 1.0 / 0.621371

# thermal: pack heat capacity as °C per joule, and the relaxation time to ambient
THERMAL_C_PER_J = 1.0 / 150_000.0
THERMAL_TAU_S = 1800.0

# ── integration ──────────────────────────────────────────────────────────
#
# Every integrated quantity here is advanced with forward Euler, which is only
# stable while the step is small against the fastest time constant in the
# system.  The fastest one is the evaporator's 60 s, then the cabin's 90 s at
# full fan, then the pack's 1800 s; the charge taper adds a nonlinear coupling
# between SOC and current on top.
#
# A single `step(3600)` used to be taken literally, and at
# `dt / THERMAL_TAU_S > 2` the pack-temperature relaxation term flips sign and
# grows: that is how a 3.3 kW Level 2 charge reached 80 °C when the clock was
# running at 14400x.  It was never a clamp and never a plausible number — it
# was Euler exploding.
#
# So `step()` never integrates more than MAX_SUBSTEP_S at once: it chops the
# interval and loops.  5 s is 1/12 of the fastest constant, which keeps the
# per-substep truncation error small, and costs about 3 us per substep — a
# 180-day `--generate` run pays a few seconds for it, which is the right
# trade.  The consequence that matters: NO time scale can make the model
# diverge, because no time scale changes the size of the integration step,
# only how many of them a wall-clock second buys.
MAX_SUBSTEP_S = 5.0


def substeps(dt, cap=MAX_SUBSTEP_S):
    """`dt` chopped into chunks of at most `cap` seconds, summing to `dt`.

    Equal-sized chunks rather than `cap` repeated with a short remainder, so
    the trajectory does not depend on where the caller happened to break its
    own loop.
    """
    dt = float(dt)
    if dt <= 0:
        return []
    n = int(dt / float(cap)) + (1 if dt % float(cap) else 0)
    n = max(1, n)
    h = dt / n
    return [h] * n

# ── charging ─────────────────────────────────────────────────────────────
#
# What is MODELLED (a shape, checked against the owner's own captures) and
# what is ASSERTED (a plausible number nobody here has measured):
#
# MODELLED — the shape.  A charge holds roughly constant power while the
#   cells are below their voltage limit (the CC phase seen from the pack side)
#   and then tapers as they approach it, current falling smoothly to a trickle
#   at 100 %.  The taper is  P = P_max · (1 − x)^n,  x = (soc − s0)/(100 − s0).
#   n ≈ 1.8 was fitted to two real Level-1/2 sessions in the owner's database
#   (2794 charging rows, `current_a > 1`): at s0 = 69 % those sessions ran
#   3.2 kW flat, then 2.2 kW at 75 %, 1.2 kW at 80 %, 0.9 kW at 85 %, 0.5 kW
#   at 90 %.  (1 − x)^1.8 reproduces that to within the sample-to-sample
#   scatter, which is itself ±30 %.
#
# MODELLED — the taper starting earlier on a tired pack.  Those sessions
#   start tapering near 69 %, not 85 %, because the pack is at 35 % SOH: less
#   capacity and more resistance means the cells hit their voltage ceiling at
#   a lower SOC.  SOH_TAPER_SHIFT slides s0 down linearly with SOH; 25 points
#   of shift puts a 35 %-SOH pack's knee at 85 − 0.65·25 ≈ 69 %, which is
#   where the real data puts it.  The linearity is a convenience, not physics.
#
# ASSERTED — everything about DC fast charge and the derates.  Nobody has ever
#   put this car on a CHAdeMO post with a logger attached.  44 kW peak, a knee
#   near 60 % and a hard fall above 80 % are the ZE0's published/folklore
#   behaviour; the thermal and cold derates are the right *shape* (fast charge
#   gets slower as the pack heats, and a freezing pack charges slowly) with
#   invented slopes.  Do not quote these numbers as measurements.
#
# ASSERTED — charge heating.  A fraction of charge power lands in the pack as
#   loss.  5 % gives ~1 °C/h on Level 2, which matches the +2 °C seen across a
#   2.2 h real session; the same 5 % on DC fast gives ~20 °C/h, which is the
#   right order for a CHAdeMO session but is not measured.

CHARGERS = {
    #          kW    knee %  exp  trickle kW  °C limit
    "l1":     (1.4,  88.0,   1.8, 0.15,       45.0),
    "l2":     (3.3,  85.0,   1.8, 0.20,       45.0),
    "l2_66":  (6.6,  84.0,   1.9, 0.30,       45.0),
    "dcfc":   (44.0, 62.0,   2.6, 1.00,       40.0),
}
CHARGER_NAMES = tuple(CHARGERS) + ("custom",)

# Wall-to-pack efficiency, so `state()["wall_kw"]` can say what the meter at
# the wall would read for the pack-side `charge_kw` this model works in.
#   l1, l2  MEASURED — mynissanleaf "Lab Test" thread (kelvin shunt at the cell
#           interconnects, user Ingineer): Level 1 77.5-78.3 %, Level 2 90.9 %
#           (3.756 kW at the wall -> 3.414 kW into the pack).
#   l2_66   ASSERTED — same on-board charger family as l2; nobody measured the
#           6.6 kW unit on this car.
#   dcfc    ASSERTED — an off-board DC supply skips the on-board charger; a
#           mid-90s figure is the usual claim, not a reading.
#   custom  ASSERTED — a round middle number for whatever the user typed in.
CHARGE_EFF = {"l1": 0.78, "l2": 0.91, "l2_66": 0.91, "dcfc": 0.95, "custom": 0.90}

# SOC points the taper knee slides down by, per 100 points of lost SOH.
SOH_TAPER_SHIFT = 25.0

# MODELLED — where the trickle stops.  The taper's floor used to be scaled by
# the same (1 - x) head as the curve above it, which made the charge an
# asymptote: power fell in proportion to the charge still to go, so the last
# point of SOC took as long as the one before it, forever.  Nothing does that;
# a charger holds its trickle and then terminates.  So the floor is flat until
# TRICKLE_END_SOC and is retired linearly over the last couple of points,
# which keeps the curve continuous (no step for a chart to draw) and lets a
# charge actually finish.  This surfaced when sub-stepping removed the Euler
# error that had been papering over it.
TRICKLE_END_SOC = 98.0

# ── the load table ───────────────────────────────────────────────────────
#
# Watts each consumer takes from the HV pack when it is on.  Every row says
# whether it was MEASURED (a number somebody read off an instrument, source
# named) or ASSERTED (a plausible figure nobody here has measured).  Change a
# number, change its comment.  No row here is a reading from the owner's car.
#
# The measured rows come from the mynissanleaf "Lab Test" thread, where user
# Ingineer put a kelvin shunt on the cell interconnects of a 2011 and read the
# pack-side draw directly.
#
# One observation from the owner's car, 2026-09-03, recorded but NOT folded in:
# stationary in READY with the lights and blower off, the pack drew a median
# 0.50 kW across 57 rows — over three times `base_ready`. It is not a clean
# base figure, because the DC-DC was recharging the 12 V battery throughout
# (13.6-14.0 V) and the draw decayed from ~0.55 kW early to ~0.37 kW late, so
# an unknown part of it is that recharge and not standing load. Splitting the
# two needs a long stationary READY soak, not a drive.
LOADS_W = {
    # base draw by start_state — the DC-DC converter, the LBC, the VCM, the
    # inverter's standby electronics and the 12 V system they feed
    "base_ready": 150.0,   # MEASURED — READY, everything off: 140-160 W (Lab Test thread)
    "base_on": 60.0,       # ASSERTED — ignition ON without READY: dash and ECUs up, no inverter
    "base_acc": 40.0,      # ASSERTED — accessory position: radio and a few modules on the DC-DC
    # ASSERTED — plugged in and charging. Contactors closed (that is how the
    # pack is being fed), so the DC-DC, LBC, coolant pump and the charger's
    # control electronics are all awake: the base_on class of draw, not the
    # 3 W of a sleeping car. The charger's *conversion* loss is not here —
    # that is CHARGE_EFF, on the wall side.
    "base_charging": 90.0,
    # ASSERTED — car off: the HV contactors are open, so the pack sees only
    # the LBC's own quiescent draw and the periodic 12 V top-ups. A few
    # watts; 3 W is ~0.8 %/day on this 24 Ah pack, the "about 1 %/day sitting"
    # owners report and what history.py assumed before the load table existed.
    "base_off": 3.0,
    # lamps
    "low_beam": 70.0,      # MEASURED — +LED low beams: 160 -> 230 W (Lab Test thread)
    # MEASURED — +halogen high beams: 160 -> 360 W (Lab Test thread). The step
    # was measured from all-off with the low beams lit underneath, so this row
    # already contains the low beams: while the high beam is on, low_beam
    # reads 0 rather than double-counting its 70 W (both on = 350 W, 360 measured).
    "high_beam": 200.0,
    "position": 15.0,      # ASSERTED — position/parking lamps + tail + plate, LED and small bulbs
    "fog": 110.0,          # ASSERTED — two 55 W halogen fog lamps
    "turn": 21.0,          # ASSERTED — one 21 W bulb per side at ~50 % blink duty, averaged (hazards: both sides)
    "brake_lamps": 42.0,   # ASSERTED — two 21 W stop lamps, lit while brake_pct > 2
    "reverse_lamps": 36.0, # ASSERTED — two 18 W reversing lamps, lit in R
    # climate — only while READY, because the HVAC amp and the compressor
    # inverter do not run otherwise
    "blower_max": 300.0,   # ASSERTED — blower at 11 V; scaled as (V/11)^2 from FAN_VOLTS
    "ac_min": 1500.0,      # OWNER REPORT — A/C at a mild cabin, car stopped: ~1.5 kW
    "ac_max": 3000.0,      # OWNER REPORT — A/C, hot cabin and hot ambient, driving: up to ~3 kW
    "ptc_max": 5000.0,     # OWNER REPORT — PTC heater flat out: 4.5-5.5 kW; linear in heater_level/40
}
# the rows `loads()` reports, in the order the dashboard should list them
LOAD_NAMES = ("base", "low_beam", "high_beam", "position", "fog", "turn",
              "brake_lamps", "reverse_lamps", "blower", "ac", "ptc", "motor", "regen")

# ── the couplings audit ──────────────────────────────────────────────────
#
# A dashboard is only as honest as the relations behind it. Every quantity
# this model publishes either derives from shared state where a physical
# relation exists, or it is named here as something deliberately left alone,
# with the reason. Three verdicts and no fourth:
#
#   implemented — the relation is in the code below; a test pins it.
#   kept        — the relation was already here before this audit; still true.
#   not modelled— deliberately not coupled, for the reason given. Never
#                 "we forgot": if it is here, someone decided.
#
# The audit that produced this table was prompted by a real report: on a
# Level 2 charge, turning the A/C to full blast changed the reported pack
# power by nothing at all. `loads()` was reading the `base_off` row while
# `charging` and gating HVAC on `start_state == "ready"`, so a plugged-in car
# had no accessories. That is the first row below.
COUPLINGS = {
    "charging -> accessories": ("implemented",
        "While `charging`, HVAC draws whenever `hvac_on` regardless of start_state (a Leaf "
        "runs its climate control on the plug), and the base row is `base_charging` — the "
        "DC-DC, LBC, pump and charger electronics — not the 3 W `base_off` of a sleeping car. "
        "Loads come off the pack AFTER the SOC taper, so pack_kw < charger_kw by the accessory "
        "draw. This is the bug the audit was called for."),
    "power budget -> current": ("implemented",
        "`power()` is the single identity — wall -> charger -> loads -> pack — and `current()` "
        "is nothing but pack_kw / V. Charge power, regen, every load row and the `current_a` "
        "extra meet in exactly one place."),
    "a/c demand <- cabin - setpoint, ambient": ("kept",
        "`ac_w()`: the compressor works harder the further the cabin sits above the setpoint "
        "and the hotter the condenser air is; the two share the headroom between ac_min and "
        "ac_max (OWNER REPORT figures)."),
    "compressor rpm <- a/c demand": ("implemented",
        "Was `1500 + 130 * fan_speed` — the blower is not what drives the compressor. Now it "
        "rides the same demand fraction `ac_w()` uses, over the 1200-3000 rpm band the real "
        "car's HVAC group 10 was seen in (1600/1730/1976/2425 rpm with A/C on)."),
    "blower -> cabin approach rate, watts": ("kept",
        "Blower watts go as V^2 from the FAN_VOLTS walk; the cabin's approach time constant "
        "shortens with fan speed in `_integrate`."),
    "hvac power -> cabin": ("implemented",
        "The cabin only moves toward the setpoint when the system is actually POWERED (READY "
        "or charging) and has a heat engine running (A/C compressor or PTC). A blower alone "
        "ventilates: it pulls the cabin toward ambient, fast, which is what a fan does."),
    "ptc -> cabin rise and draw": ("kept",
        "`ptc_w()` is linear in the amp's own heater_level byte; the heater is what lets the "
        "cabin rise toward a setpoint above ambient."),
    "evaporator <- compressor and airflow": ("implemented",
        "The coil is pulled toward EVAP_MIN_C by the compressor and pushed back up by airflow "
        "over it: more fan, warmer coil. Previously a flat 2 degC target whenever A/C was on, "
        "and it ran even with the car asleep."),
    "charging -> pack heat, charger loss": ("kept",
        "`charge_heat_frac` of charge power lands in the pack (~1 degC/h on L2); CHARGE_EFF is "
        "the wall-to-pack loss and is what `wall_kw` reports."),
    "lv_volts <- converter, lamps, time": ("implemented",
        "NOT as a forced 14.2-14.6 V DC-DC bus: the owner's own 2026-08-24 capture reads "
        "12.677 V on this byte with the car READY, and a model that contradicts the only "
        "measurement of a value is worse than one that leaves it alone. What IS coupled is "
        "the part nothing measured contradicts — the 12 V battery drains while the car is OFF "
        "with lamps burning (leave the headlights on and the charge_12v lamp eventually "
        "lights) and recovers toward its resting figure while the converter runs. The "
        "lv_battery_weak fault still sags it under load."),
    "soc -> taper knee, regen limit": ("kept",
        "The taper is a function of SOC (knee slid down by SOH); regen fades out from "
        "REGEN_SOC_FADE and stops at REGEN_SOC_STOP because a full pack will not take it."),
    "pack temp -> charge derate, output_avail": ("kept",
        "`charge_derate()` for hot and freezing packs; `output_avail()` for the power bubbles "
        "and the turtle lamp. Both read the same pack temperature the thermal integrator moves."),
    "motor <- speed, pedal, gear": ("kept",
        "Road load plus a pedal term inside a motor's power envelope, only in D/Eco/R while "
        "READY — and never while the charge connector is latched. Both terms are still "
        "ASSERTED after the 2026-09-03 drive: road grade is unmeasured, so speed-vs-power "
        "cannot be separated from the route's hills, and the accelerator is never logged."),
    "regen <- gear, speed, brake, soc": ("kept",
        "Coast regen in D/Eco (Eco harder), brake blending on top, faded by speed and SOC. "
        "The 2026-09-03 drive corroborated the Eco coast figure (median +3.60 kW observed at "
        "10-20 mph against 4.0 kW modelled); D coast and the brake term are still asserted."),
    "range <- soc, capacity, pack voltage, accessories": ("implemented",
        "Usable energy is SOC x capacity x pack voltage — the same three numbers the health "
        "tiles show — divided by a consumption figure that now INCLUDES the accessory draw. "
        "The A/C shortening the guess-o-meter is the single most visible coupling in the car "
        "and the model had it decoupled."),
    "headlights while off -> 12 V drain": ("implemented",
        "See lv_volts above. ASSERTED rate: a 70 W lamp load flattens a 45 Ah 12 V battery in "
        "a handful of hours, which is the order the real thing manages."),
    "hx <- soh": ("not modelled",
        "Two data points, no law: the owner's 35 %-SOH pack reports HX 17.96, and the "
        "scenarios' healthy 91 % pack 92. Nothing linear, quadratic or exponential fits both "
        "within a useful margin, and HX is read straight off group 01 on the real car — so it "
        "stays an independently settable knob rather than being invented from SOH."),
    "internal resistance <- soh": ("not modelled",
        "R0 has never been measured on this car; it exists to make the pack sag under load. "
        "Deriving it from SOH would make the fixture the OCV curve is calibrated against "
        "depend on an invented law, and would take the knob away from the fault the "
        "simulator exists to stage."),
    "lamps while off <- 12 V battery, not the pack": ("not modelled",
        "With the contactors open the lamps really run off the 12 V battery, not the HV pack. "
        "`loads()` keeps charging them to the pack (through the periodic DC-DC top-ups the "
        "base_off row describes) so that every row of the load table stays observable in "
        "every ignition state, which is what the dashboard is for. The 12 V drain above is "
        "the honest half of this relation."),
    "door -> interior lamp": ("not modelled",
        "A few watts on the 12 V side, below the resolution of anything the dashboard reads, "
        "and it would need a load row that no capture could ever confirm."),
}
COUPLING_VERDICTS = ("implemented", "kept", "not modelled")

# ── motor and regen ──────────────────────────────────────────────────────
#
# The 2026-09-03 drive (13 min, 3.5 mi, peak 41.4 mph, pack current to −66 A)
# was the capture these numbers were waiting for. It settled one of them and
# refused to settle the rest, for reasons worth writing down.
#
# ROAD LOAD — still ASSERTED. P = (150·v + 0.38·v³) / 0.85 W with v in m/s:
# 150 N of rolling resistance and 0.38 kg/m of drag (½ρ·Cd·A for Cd 0.29,
# A 2.27 m²) are the usual ZE0 figures and 0.85 is a round inverter+motor
# efficiency, giving ≈5.7 kW at 40 mph, ≈11 at 55 and ≈19 at 70.
#
# The drive CANNOT calibrate this and it would be dishonest to pretend
# otherwise. Road grade is unmeasured — no GPS, no altitude, no inclinometer,
# and 0x1D5 torque / 0x260 power limits are not polled — so a fit of power
# against speed would bake the route's hills into the model as though they
# were rolling resistance. The contamination is not subtle: across steady
# rows (speed within 5 mph over three samples) the 35–40 mph bin spans
# −15.07 to −3.72 kW at n = 12, and one "steady" 32 mph row was *regenerating*
# at +0.72 kW. Nor can grade be cancelled: the speed profile's mirror
# correlation is −0.05, so this was not an out-and-back over the same roads.
# Even the per-bin minimum draw — a lower bound on level-ground load — comes
# out non-monotonic (−3.72 kW at 35–40 mph, −2.01 kW at 40–45), which is what
# a descent looks like. All that can honestly be said is that the asserted
# curve lies inside the observed envelope, toward its low-draw edge.
#
# What would settle it: research/driving_capture_plan.md R1's steady-speed
# ladder run out-and-back over the same stretch so grade cancels, or R7's
# deliberate hill climb and descent. And nothing above 41.4 mph can be
# calibrated by any 41 mph drive, whatever else is captured.
#
# THE PEDAL TERM — still ASSERTED, and unmeasurable from this drive: the
# accelerator position is not on Car-CAN and was never logged, so there is no
# input side to fit against. The pedal adds up to MOTOR_PEAK_KW, shaped like
# a motor's power envelope: torque-limited below MOTOR_BASE_MPH (power grows
# with speed — a floored pedal at a standstill is all torque and no watts),
# then falling off linearly to nothing at MOTOR_ZERO_MPH. The largest
# traction draw the drive ever saw was −23.8 kW at 26.6 mph, which bounds
# MOTOR_PEAK_KW from below and nothing more.
ROAD_ROLL_W_PER_MS = 150.0
ROAD_DRAG_W_PER_MS3 = 0.38
DRIVE_EFF = 0.85
MOTOR_PEAK_KW = 80.0
MOTOR_BASE_MPH = 25.0
MOTOR_ZERO_MPH = 120.0
# the pedal "carries" the road load: below PEDAL_HOLD_PCT the cruise term
# ramps in, so lifting the pedal to 0 means coasting, not a 6 kW draw
PEDAL_HOLD_PCT = 10.0
# Regen: lift-off in D and Eco (Eco regens harder — the one difference the
# shifter position makes on this car), plus the brake pedal blending in up to
# REGEN_BRAKE_KW. Fades to nothing below REGEN_FULL_MPH (a motor cannot regen
# at a standstill) and above REGEN_SOC_FADE .. REGEN_SOC_STOP (a full pack
# will not take it; the real car shows fewer regen bubbles there).
#
# Eco coast: CORROBORATED by the 2026-09-03 drive, and it is the one term
# grade cannot fake — lift-off regen is a commanded torque, so a descent
# changes how fast the car slows, not how many watts come back at a given
# speed. Rows in Eco with the brake released and the car giving power back:
# median +3.60 kW in the 10–20 mph band (n = 9), peak +7.68 kW. 4.0 kW sits
# in the middle of that, so it stays. Not called MEASURED: the accelerator is
# not logged, so "brake released" is not proof the pedal was fully lifted,
# and each row averages 5–6 s of a decelerating car.
#
# D coast: ASSERTED still. The drive spent almost all of itself in Eco and
# produced exactly one usable D coast sample (+4.10 kW at 8.4 mph). One
# sample calibrates nothing, and it happens to sit closer to the Eco figure
# than to this one — a D-heavy capture is what would settle it.
#
# REGEN_BRAKE_KW: ASSERTED still. The brake pedal never went past 8.6 % on
# this drive, so only the bottom tenth of the range was exercised, and there
# the model over-predicts (at 8.6 % / 27.1 mph it wants ≈6.6 kW and the car
# gave +2.17 kW) — but those rows are all decelerations through the speed
# fade, so they do not cleanly indict the constant either. A capture with
# firm braking from a steady speed is what would fix it.
REGEN_COAST_KW = {"D": 2.0, "Eco": 4.0}
REGEN_BRAKE_KW = 30.0
REGEN_MIN_MPH = 3.0
REGEN_FULL_MPH = 15.0
REGEN_SOC_FADE = 90.0
REGEN_SOC_STOP = 95.0
DRIVE_GEARS = ("D", "Eco", "R")

# ── climate and 12 V couplings — ASSERTED shapes ─────────────────────────
#
# Compressor speed rides the A/C demand, not the blower. The band is the one
# the real car was seen in: HVAC group 10 read 1600, 1730, 1976 and 2425 rpm
# with the compressor engaged (docs/SIGNALS.md, leaf_decoders.py) and 0 with
# it off, so idle demand sits near the bottom of that and full demand a little
# above the top of it.
COMPRESSOR_IDLE_RPM = 1200.0
COMPRESSOR_MAX_RPM = 3000.0
# The evaporator coil: the compressor pulls it toward EVAP_MIN_C, airflow over
# it pushes it back up (more fan, warmer coil).
EVAP_MIN_C = 2.0
EVAP_FAN_RISE_C = 0.6            # per fan speed step
# 12 V battery. The knob is the *reported* figure (LBC group 01 bytes 20-21);
# LV_REST_V is where a healthy one settles, and is the default and the value
# the owner's own capture reads. ASSERTED: ~45 Ah of usable 12 V battery, so a
# 70 W lamp load takes it from 12.7 V to the 11.8 V warning in a few hours.
LV_REST_V = 12.68
LV_FLOOR_V = 9.0
LV_BATTERY_WH = 540.0            # 45 Ah x 12 V, ASSERTED
LV_SPAN_V = 3.0                  # volts of usable droop across that energy
LV_RECOVER_TAU_S = 900.0         # recovery toward rest while the converter runs
# Range. The guess-o-meter divides usable energy by a consumption figure; the
# accessories ride on top of it at a nominal average speed, which is why the
# A/C shortens the range on the real dash too. ASSERTED, both numbers.
RANGE_KWH_PER_KM = 0.17
RANGE_NOMINAL_KMH = 45.0

# ── the cluster — indicators the model cannot drive ──────────────────────
#
# Real ZE0 lamps (2012 Owner's Manual §2 p. 2-12) with no driver in this
# model: they are always False and listed so a cluster can draw them dim
# with an honest tooltip instead of pretending. Never faked.
LAMPS_UNMODELLED = ("abs", "vsp", "brake_yellow", "brake_red", "ps", "shift_control",
                    "vdc", "vdc_off", "seatbelt", "airbag", "passenger_airbag")
LAMP_NAMES = ("ready", "turn_left", "turn_right", "hazards", "low_beam", "high_beam",
              "position", "fog", "parking_brake", "door_ajar", "plug_in", "charge_12v",
              "ev_system", "power_limit", "low_battery", "tpms", "headlight_warning",
              "security", "eco", "master_red", "master_yellow")
LV_WARN_V = 11.8
TPMS_WARN_PSI = 30.0
TPMS_SENSOR_MIN_PSI = 5.0        # below this the sensor is missing, not the air
EV_SYSTEM_FAULTS = ("fault.cell_degraded", "fault.insulation_low",
                    "fault.sensor_dropout", "fault.ecu_nrc")


def ocv(soc_pct):
    """Open-circuit voltage of one cell pair at this state of charge."""
    s = min(100.0, max(0.0, soc_pct))
    lo = OCV_TABLE[0]
    for hi in OCV_TABLE[1:]:
        if s <= hi[0]:
            span = hi[0] - lo[0]
            f = 0.0 if span <= 0 else (s - lo[0]) / span
            return lo[1] + f * (hi[1] - lo[1])
        lo = hi
    return OCV_TABLE[-1][1]


def cruise_kw(mph):
    """Road load at a steady speed, kW. ASSERTED — see the motor note above.
    ≈5.7 kW at 40 mph. The 2026-09-03 drive could not calibrate this — road
    grade is unmeasured and dominates at these speeds — so it stays a shape."""
    v = max(0.0, float(mph)) * MPH_TO_MS
    return (ROAD_ROLL_W_PER_MS * v + ROAD_DRAG_W_PER_MS3 * v ** 3) / DRIVE_EFF / 1000.0


def _clamp01(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# ── knob registry ────────────────────────────────────────────────────────

def build_knobs():
    K = KnobSet()
    a = K.add

    K.group("battery")
    a("soc", "float", 85.0, "State of charge, %", "%", 0.0, 100.0, label="State of charge")
    a("capacity_ah", "float", 23.16, "Usable pack capacity (LBC group 01)", "Ah", 0.0, 66.0,
      label="Usable capacity")
    a("soh", "float", 35.09, "State of health; coupled to capacity_ah (soh = capacity/66)", "%", 0.0, 100.0,
      label="State of health")
    a("hx", "float", 17.96, "Nissan's HX conductance figure", "", 0.0, 200.0, label="HX conductance")
    a("pack_temp_c", "float", 22.0, "Pack temperature the four sensors sit around", "°C", -30.0, 70.0,
      label="Pack temperature")
    a("cell_spread_mv", "float", 30.0, "Spread between the highest and lowest cell pair", "mV", 0.0, 800.0,
      label="Cell spread")
    a("weak_cell_index", "int", 55, "Which cell pair carries weak_cell_offset_mv", "", 0, 95,
      label="Weak cell pair")
    a("weak_cell_offset_mv", "float", 0.0, "Offset applied to that one pair (negative = weak)", "mV", -1200.0, 200.0,
      label="Weak cell offset")
    a("internal_resistance_ohm", "float", 0.08, "Pack series resistance R0 — sets voltage sag under load", "ohm", 0.0, 2.0,
      label="Pack resistance")
    a("lv_volts", "float", 12.68, "12 V accessory battery", "V", 0.0, 16.0, label="12 V battery")
    a("insulation_kohm", "float", 885.0, "HV isolation resistance", "kohm", 0.0, 5000.0,
      label="HV insulation")

    K.group("load")
    a("current_a", "float", 0.0,
      "EXTRA pack current added on top of the modelled loads (READY electronics, lamps, "
      "HVAC, motor — see LOADS_W); NEGATIVE = discharging (house rule). 0 = the model alone",
      "A", -400.0, 400.0, label="Extra pack current")
    a("load_kw", "float", 0.0, "Absolute load in kW; when non-zero it OVERRIDES the whole load model "
                               "and current_a (still a discharge). For tests that want an exact number",
      "kW", 0.0, 100.0, label="Load override")
    a("speed_mph", "float", 0.0, "Road speed; sets the motor's road load in D/Eco/R and the coast regen",
      "mph", 0.0, 120.0, label="Speed")
    a("gear", "text", "P", "Shifter position", "", choices=GEARS, label="Shifter")
    a("accel_pedal_pct", "float", 0.0, "Accelerator position; the motor draws only while it is pressed",
      "%", 0.0, 100.0, label="Accelerator")
    a("brake_pct", "float", 0.0, "Brake pedal position; blends in regen and lights the stop lamps",
      "%", 0.0, 100.0, label="Brake pedal")
    a("charging", "bool", False, "Charging: current goes positive and follows the charge curve", "",
      label="Plugged in and charging")
    a("plugged_in", "bool", False,
      "Charge connector latched. A car can be plugged in without drawing (finished, or "
      "waiting on a timer), so this is separate from `charging` — which implies it. "
      "While it is latched the car will not go READY (Owner's Manual p. 2-19)", "",
      label="Charge connector")
    a("charger", "text", "l2",
      "Charger type; setting it re-seats charge_kw and the taper knobs "
      "(l1 1.4 kW, l2 3.3 kW, l2_66 6.6 kW, dcfc 44 kW CHAdeMO). Set it BEFORE "
      "charge_kw if you want to override the power", "", choices=CHARGER_NAMES, label="Charger")
    a("charge_kw", "float", 3.3, "Peak charge power (pack side) — held until the taper knee, then rolled off",
      "kW", 0.0, 50.0, label="Charge power")
    a("charge_taper_start_soc", "float", 85.0,
      "SOC the taper begins at on a healthy pack; slides down as SOH falls", "%", 0.0, 100.0,
      label="Taper knee")
    a("charge_taper_exp", "float", 1.8, "Taper exponent n in P = Pmax*(1-x)^n", "", 0.5, 8.0,
      label="Taper exponent")
    a("charge_trickle_kw", "float", 0.2, "Floor the taper falls to before the charge finishes", "kW", 0.0, 10.0,
      label="Trickle power")
    a("charge_heat_frac", "float", 0.05, "Fraction of charge power that lands in the pack as heat", "", 0.0, 0.5,
      label="Charge heating fraction")
    a("charge_temp_limit_c", "float", 45.0, "Pack temperature above which charge power is derated", "°C", 0.0, 80.0,
      label="Charge derate temperature")

    K.group("climate")
    a("hvac_on", "bool", False, "HVAC blower running (byte 11 bit 7)", "", label="Climate on")
    a("hvac_ac_on", "bool", False, "A/C compressor engaged (byte 10 bit 7)", "", label="A/C compressor")
    a("hvac_fan_speed", "int", 0, "Blower speed 0-7 (encoded as motor volts)", "", 0, 7, label="Fan speed")
    a("hvac_setpoint_f", "float", 72.0, "Temperature setpoint", "°F", 60.0, 90.0, label="Setpoint")
    a("cabin_temp_c", "float", 22.0, "In-car temperature", "°C", -30.0, 70.0, label="Cabin temperature")
    a("ambient_c", "float", 22.0, "Outside air temperature", "°C", -40.0, 60.0, label="Outside temperature")
    a("evap_c", "float", 22.0, "Evaporator / intake temperature", "°C", -20.0, 70.0,
      label="Evaporator temperature")
    a("heater_level", "int", 0, "PTC heater demand (group 10 byte 36); 40 = flat out, ~5 kW", "", 0, 40,
      label="Heater demand")
    a("sunload", "int", 0, "Sunload sensor (group 10 byte 3)", "", 0, 255, label="Sunload")

    K.group("body")
    a("doors", "text", "none", "Convenience setter: comma list of open doors "
                               "(driver,pass,rl,rr,hatch) or 'none'/'all'", label="Open doors")
    door_labels = {"driver": "Driver door", "pass": "Passenger door", "rl": "Rear-left door",
                   "rr": "Rear-right door", "hatch": "Hatch"}
    for d in DOOR_NAMES:
        a(f"door_{d}", "bool", False, f"{d} door open", "", label=door_labels[d])
    a("locked", "bool", True, "Doors locked (0x60D byte 2 = 0x18)", "", label="Locked")
    a("headlights", "bool", False, "Low beam", "", label="Low beam")
    a("high_beam", "bool", False, "High beam", "", label="High beam")
    a("parking_lights", "bool", False, "Position / parking lamps", "", label="Parking lamps")
    a("fog_lights", "bool", False, "Fog lamps", "", label="Fog lamps")
    a("turn_signal", "text", "off", "Turn indicator state", "", choices=TURN, label="Turn signal")
    a("handbrake", "bool", True, "Parking brake set", "", label="Parking brake")
    a("start_state", "text", "ready", "Ignition state (0x60D byte 1 bits 1-2); HVAC and motor draw only in READY",
      "", choices=START_STATES, label="Ignition")
    a("odometer_mi", "float", 65545.0, "Odometer; integrates from speed_mph", "mi", 0.0, 999999.0,
      label="Odometer")
    a("units_miles", "bool", True, "Dash units (0x355 byte 6 bit 5)", "", label="Dash in miles")
    a("tpms_psi", "float", 35.0, "Convenience setter: all four tyres to this pressure", "psi", 0.0, 60.0,
      label="All tyres")
    tyre_labels = {"fl": "Tyre front-left", "fr": "Tyre front-right",
                   "rr": "Tyre rear-right", "rl": "Tyre rear-left"}
    for t in TPMS_ORDER:
        a(f"tpms_{t}", "float", 35.0, f"{t.upper()} tyre pressure", "psi", 0.0, 60.0, label=tyre_labels[t])

    K.group("rig")
    a("noise", "float", 0.2, "Sensor jitter amplitude (mV on cells, scaled elsewhere); 0 = perfectly clean", "", 0.0, 20.0,
      label="Sensor noise")
    a("clock_scale", "float", 1.0,
      "Simulated seconds per real second. An explicit --speed OVERRIDES this "
      "rather than multiplying it; the effective figure is state()['time_scale']",
      "", 0.01, 3600.0, label="Clock speed")

    K.group("faults")
    a("fault.cell_degraded", "bool", False,
      "One cell pair collapses (-350 mV at weak_cell_index) and the spread widens", label="Degraded cell pair")
    a("fault.insulation_low", "bool", False, "Isolation resistance falls to ~18 kohm", label="Low insulation")
    a("fault.lv_battery_weak", "bool", False, "12 V battery sags to ~11.1 V and droops further under load",
      label="Weak 12 V battery")
    a("fault.sensor_dropout", "bool", False,
      "Pack temperature sensor 3 and current sensor 1 report nonsense (0x7FFF / -1)", label="Sensor dropout")
    a("fault.car_asleep", "bool", False, "LBC and HVAC stop answering; Car-CAN goes quiet", label="Car asleep")
    a("fault.adapter_silent", "bool", False, "The adapter itself answers nothing at all", label="Adapter silent")
    a("fault.bus_noise", "bool", False, "Truncated frames and DATA ERROR lines appear in passive captures",
      label="Bus noise")
    a("fault.ecu_nrc", "bool", False, "Every service-21 request gets a negative response instead of data",
      label="ECU negative response")
    a("ecu_nrc_code", "int", 0x22, "The NRC returned while fault.ecu_nrc is set (0x22 = conditionsNotCorrect)", "", 0x10, 0xFF,
      label="NRC code")
    return K


KNOBS = build_knobs()

# knobs whose value the model integrates; setting one re-seats the integrator
INTEGRATED = ("soc", "pack_temp_c", "cabin_temp_c", "evap_c", "odometer_mi", "lv_volts")


def _clamp(value, knob_name):
    """Hold an integrated value inside its own knob's declared domain.

    This is a **domain guard, not the stability fix** — sub-stepping is the
    fix, and in any ordinary regime this function never changes anything. It
    exists because the knobs can be set to combinations no car can be in
    (400 A through a 2 ohm pack is 320 kW of ohmic loss) and a state variable
    that leaves the range its own schema advertises is a lie to every reader
    of `state()`. If it is doing work, the knobs are the fiction, not the
    integrator.
    """
    kn = KNOBS[knob_name]
    v = float(value)
    if kn.min is not None:
        v = max(kn.min, v)
    if kn.max is not None:
        v = min(kn.max, v)
    return v

_DOOR_ALIASES = {"driver": "driver", "d": "driver", "passenger": "pass", "pass": "pass",
                 "p": "pass", "rl": "rl", "rear_left": "rl", "rr": "rr",
                 "rear_right": "rr", "hatch": "hatch", "trunk": "hatch"}


class LeafModel:
    """The ZE0 pack, body and climate system as a bag of numbers."""

    vehicle = "leaf_ze0"
    knobs = KNOBS

    def __init__(self, seed=1):
        self.seed = int(seed)
        self.k = KNOBS.defaults()
        self.t = 0.0
        rng = random.Random(self.seed)
        # Fixed per-cell shape, normalised so (max - min) == 1.0 and scaled by
        # cell_spread_mv at read time, so the knob means exactly what it says.
        # A random walk rather than independent draws: neighbouring pairs sit
        # in the same module and see the same cooling, so the real pack's
        # within-segment spread is a fraction of its end-to-end spread — which
        # is what makes group 05's segment deltas small.
        raw, v = [], 0.0
        for _ in range(NUM_CELLS):
            v += rng.gauss(0.0, 1.0)
            raw.append(v)
        lo, hi = min(raw), max(raw)
        span = (hi - lo) or 1.0
        mid = (hi + lo) / 2.0
        self.cell_shape = [(v - mid) / span for v in raw]
        self.temp_offsets = [rng.randint(0, 2) for _ in range(4)]
        self._noise = random.Random(self.seed ^ 0x5EED)

    # ── knob plumbing ────────────────────────────────────────────────────

    def on_set(self, name, value):
        """Side effects of setting a knob. Returns extra {name: value} applied."""
        extra = {}
        if name == "soh":
            # soh and capacity_ah are the same fact twice (soh = capacity / 66),
            # so each moves the other — but only when they actually disagree,
            # or a get_knobs()/set() round trip would drift on the rounding
            if round(self.k["capacity_ah"] / NOMINAL_CAPACITY_AH * 100.0, 2) != round(value, 2):
                self.k["capacity_ah"] = round(value / 100.0 * NOMINAL_CAPACITY_AH, 4)
                extra["capacity_ah"] = self.k["capacity_ah"]
        elif name == "capacity_ah":
            if round(value / NOMINAL_CAPACITY_AH * 100.0, 2) != round(self.k["soh"], 2):
                self.k["soh"] = round(value / NOMINAL_CAPACITY_AH * 100.0, 2)
                extra["soh"] = self.k["soh"]
        elif name == "charging":
            # charging implies the connector is latched; unplugging stops the
            # charge. The two knobs cannot disagree in a way no car can be in.
            if value and not self.k["plugged_in"]:
                self.k["plugged_in"] = True
                extra["plugged_in"] = True
        elif name == "plugged_in":
            if not value and self.k["charging"]:
                self.k["charging"] = False
                extra["charging"] = False
        elif name == "charger":
            spec = CHARGERS.get(value)
            if spec:
                kw, knee, exp, trickle, tlimit = spec
                for n, v in (("charge_kw", kw), ("charge_taper_start_soc", knee),
                             ("charge_taper_exp", exp), ("charge_trickle_kw", trickle),
                             ("charge_temp_limit_c", tlimit)):
                    self.k[n] = v
                    extra[n] = v
        elif name == "doors":
            open_ = self._parse_doors(value)
            for d in DOOR_NAMES:
                self.k[f"door_{d}"] = d in open_
                extra[f"door_{d}"] = self.k[f"door_{d}"]
        elif name == "tpms_psi":
            for t in TPMS_ORDER:
                self.k[f"tpms_{t}"] = value
                extra[f"tpms_{t}"] = value
        return extra

    @staticmethod
    def _parse_doors(value):
        s = (value or "").strip().lower()
        if s in ("", "none", "shut", "closed"):
            return set()
        if s == "all":
            return set(DOOR_NAMES)
        out = set()
        for part in s.replace("+", ",").split(","):
            p = part.strip()
            if not p:
                continue
            if p not in _DOOR_ALIASES:
                raise ValueError(f"knob 'doors': {p!r} is not a door "
                                 f"({', '.join(DOOR_NAMES)}, 'none' or 'all')")
            out.add(_DOOR_ALIASES[p])
        return out

    # ── physics ──────────────────────────────────────────────────────────

    def taper_start_soc(self):
        """The SOC the taper actually begins at, after the SOH slide."""
        soh = self.k["capacity_ah"] / NOMINAL_CAPACITY_AH * 100.0
        s0 = self.k["charge_taper_start_soc"] - SOH_TAPER_SHIFT * (1.0 - soh / 100.0)
        return max(10.0, min(99.0, s0))

    def charge_derate(self):
        """Thermal and cold limits on charge power. ASSERTED slopes — see the
        CHARGERS comment. Hot: falls off above charge_temp_limit_c. Cold: a
        pack below 5 °C accepts less, and nothing below -10 °C accepts much."""
        t = self.k["pack_temp_c"]
        lim = self.k["charge_temp_limit_c"]
        f = 1.0
        if t > lim:
            f *= max(0.25, 1.0 - (t - lim) / 25.0)
        if t < 5.0:
            f *= max(0.25, (t + 10.0) / 15.0)
        return f

    def charge_power_kw(self):
        """Charge power at the present SOC, kW (pack side). Zero unless `charging`.

        Constant `charge_kw` below the knee; P = Pmax*(1-x)^n above it, where
        x runs 0 -> 1 from the knee to 100 % SOC; a trickle floor that itself
        decays to zero at 100 %, so the curve ends smoothly rather than
        stopping at a step.
        """
        k = self.k
        if not k["charging"]:
            return 0.0
        pmax = max(0.0, float(k["charge_kw"]))
        soc = min(100.0, max(0.0, k["soc"]))
        if soc >= 100.0 or pmax <= 0.0:
            return 0.0
        s0 = self.taper_start_soc()
        p = pmax
        if soc > s0:
            x = (soc - s0) / max(1e-6, 100.0 - s0)
            head = max(0.0, 1.0 - x)
            end = min(1.0, max(0.0, (100.0 - soc) / (100.0 - TRICKLE_END_SOC)))
            p = max(pmax * head ** float(k["charge_taper_exp"]),
                    min(float(k["charge_trickle_kw"]), pmax) * end)
        return p * self.charge_derate()

    def charge_eff(self):
        return CHARGE_EFF.get(self.k["charger"], CHARGE_EFF["custom"])

    def wall_kw(self):
        """What a meter at the wall would read for the pack-side charge power."""
        p = self.charge_power_kw()
        return p / self.charge_eff() if p > 0 else 0.0

    # ── the load model ───────────────────────────────────────────────────

    def is_ready(self):
        return self.k["start_state"] == "ready"

    def _driving(self):
        # never while the connector is latched: the car will not move, so the
        # motor and regen have nothing to do (Owner's Manual p. 2-19)
        return self.is_ready() and not self.k["charging"] and self.k["gear"] in DRIVE_GEARS

    def hvac_powered(self):
        """Is the climate system live?

        READY, or plugged in and charging. The second half is the whole point
        of this audit: a Leaf runs its climate control on the plug — that is
        what pre-conditioning IS — and the model used to give a charging car
        no accessories at all, so A/C at full blast changed the reported pack
        power by exactly nothing.
        """
        return self.is_ready() or bool(self.k["charging"])

    def hvac_setpoint_c(self):
        return (self.k["hvac_setpoint_f"] - 32.0) * 5.0 / 9.0

    def blower_w(self):
        """ASSERTED — a DC blower's power goes as V², 300 W at its 11 V top."""
        k = self.k
        if not (self.hvac_powered() and k["hvac_on"]):
            return 0.0
        v = FAN_VOLTS.get(int(k["hvac_fan_speed"]), 0)
        return LOADS_W["blower_max"] * (v / 11.0) ** 2

    def ac_demand(self):
        """How hard the compressor is being asked to work, 0..1.

        The one driver of both the A/C watts and the compressor speed: the
        further the cabin sits above the setpoint (an 8 °C gap is 'hot') and
        the hotter the condenser's air is, the harder it works; the two share
        the headroom. 0 when the compressor is not running at all.
        """
        k = self.k
        if not (self.hvac_powered() and k["hvac_on"] and k["hvac_ac_on"]):
            return 0.0
        gap = _clamp01((k["cabin_temp_c"] - self.hvac_setpoint_c()) / 8.0, 0.3, 1.0)
        hot = _clamp01((k["ambient_c"] - 25.0) / 15.0)
        return _clamp01(0.5 * (gap - 0.3) / 0.7 + 0.5 * hot)

    def ac_w(self):
        """OWNER REPORT — ~1.5 kW with a mild cabin and the car stopped, up to
        ~3 kW with a hot cabin in hot weather, over `ac_demand()`."""
        if not (self.hvac_powered() and self.k["hvac_on"] and self.k["hvac_ac_on"]):
            return 0.0
        return LOADS_W["ac_min"] + (LOADS_W["ac_max"] - LOADS_W["ac_min"]) * self.ac_demand()

    def ptc_w(self):
        """OWNER REPORT — the resistive heater is 4.5-5.5 kW flat out; taken
        as linear in the amp's own demand byte, 40 = all of it."""
        k = self.k
        if not (self.hvac_powered() and k["hvac_on"]):
            return 0.0
        return LOADS_W["ptc_max"] * _clamp01(k["heater_level"] / 40.0)

    def motor_kw(self):
        """Traction draw, kW (positive = drawing). ASSERTED — see the note by
        cruise_kw(). Only in D/Eco/R while READY, and only while the pedal is
        pressed: lifting off is coasting, which is regen's job."""
        k = self.k
        if not self._driving():
            return 0.0
        pedal = _clamp01(k["accel_pedal_pct"] / 100.0)
        if pedal <= 0.0:
            return 0.0
        mph = max(0.0, k["speed_mph"])
        hold = _clamp01(k["accel_pedal_pct"] / PEDAL_HOLD_PCT)
        envelope = _clamp01(mph / MOTOR_BASE_MPH) * _clamp01(1.0 - mph / MOTOR_ZERO_MPH)
        return cruise_kw(mph) * hold + pedal * MOTOR_PEAK_KW * envelope

    def regen_kw(self):
        """Energy going back in, kW (positive = into the pack). ASSERTED."""
        k = self.k
        if not self._driving() or k["gear"] not in REGEN_COAST_KW:
            return 0.0
        mph = max(0.0, k["speed_mph"])
        if mph <= 0.0:
            return 0.0
        coast = REGEN_COAST_KW[k["gear"]] if (k["accel_pedal_pct"] <= 0.0 and mph > REGEN_MIN_MPH) else 0.0
        brake = _clamp01(k["brake_pct"] / 100.0) * REGEN_BRAKE_KW
        speed_f = _clamp01(mph / REGEN_FULL_MPH)
        soc_f = _clamp01((REGEN_SOC_STOP - k["soc"]) / (REGEN_SOC_STOP - REGEN_SOC_FADE))
        return (coast + brake) * speed_f * soc_f

    def loads(self):
        """Every consumer's draw right now, watts, keyed by LOAD_NAMES.

        `regen` is listed for the dashboard's benefit but is a *source*, not
        a sink: `load_total_w()` leaves it out.
        """
        k = self.k
        # A Leaf will not go to READY with the charge connector latched, so the
        # start_state knob does not decide the base draw during a charge — but
        # the car is anything but asleep: contactors closed, DC-DC up, pump
        # running, charger electronics awake. That is `base_charging`, and it
        # is why this used to be wrong (see COUPLINGS): reading `base_off`
        # here, with HVAC gated on READY, made a charging car draw nothing.
        ss = "charging" if k["charging"] else k["start_state"]
        high = bool(k["high_beam"])
        turn = k["turn_signal"]
        return {
            "base": LOADS_W.get(f"base_{ss}", LOADS_W["base_off"]),
            "low_beam": 0.0 if high else (LOADS_W["low_beam"] if k["headlights"] else 0.0),
            "high_beam": LOADS_W["high_beam"] if high else 0.0,
            "position": LOADS_W["position"] if k["parking_lights"] else 0.0,
            "fog": LOADS_W["fog"] if k["fog_lights"] else 0.0,
            "turn": (LOADS_W["turn"] * (2.0 if turn == "hazards" else 1.0)) if turn != "off" else 0.0,
            "brake_lamps": LOADS_W["brake_lamps"] if k["brake_pct"] > 2.0 else 0.0,
            "reverse_lamps": LOADS_W["reverse_lamps"] if k["gear"] == "R" else 0.0,
            "blower": self.blower_w(),
            "ac": self.ac_w(),
            "ptc": self.ptc_w(),
            "motor": self.motor_kw() * 1000.0,
            "regen": self.regen_kw() * 1000.0,
        }

    def load_total_w(self):
        """Σ of every sink in `loads()` — everything but regen."""
        L = self.loads()
        return sum(v for n, v in L.items() if n != "regen")

    def hvac_kw(self):
        return (self.blower_w() + self.ac_w() + self.ptc_w()) / 1000.0

    def power(self):
        """The whole car's power budget, kW, in one place.

        Positive is into the pack, the house sign. Follow it left to right and
        it is the sentence a charging Leaf's owner wants to read:

            wall_kw  ── the meter at the wall (charger_kw / CHARGE_EFF)
              -> charger_kw  ── DC out of the on-board charger, after the
                                taper and the thermal derate
              -> loads_kw    ── everything switched on right now, the same
                                Σ the `loads_w` breakdown shows
              -> pack_kw = charger_kw + regen_kw − loads_kw + extra_kw

        `extra_kw` is the `current_a` knob's extra current expressed as power;
        it is 0 unless somebody asked for it. `power_kw` in `state()` is the
        same number at the *terminals* rather than at the OCV, so the two
        differ by the I²R the pack's own resistance eats — a few tens of watts.

        `load_kw` > 0 (and not charging) is still the absolute override it has
        always been: it replaces the whole budget with itself.

        This is the only arithmetic; `current()` is this divided by volts.
        """
        k = self.k
        v = max(1.0, self.ocv_pack())
        override = k["load_kw"] > 0 and not k["charging"]
        if override:
            charger_kw = regen = extra_kw = 0.0
            loads_kw = float(k["load_kw"])
        else:
            charger_kw = self.charge_power_kw()
            regen = self.regen_kw()
            loads_kw = self.load_total_w() / 1000.0
            extra_kw = float(k["current_a"]) * v / 1000.0
        pack_kw = charger_kw + regen - loads_kw + extra_kw
        return {
            "wall_kw": round(charger_kw / self.charge_eff(), 3) if charger_kw > 0 else 0.0,
            "charger_kw": round(charger_kw, 3),
            "loads_kw": round(loads_kw, 3),
            "loads_total_w": round(loads_kw * 1000.0, 1),
            "regen_kw": round(regen, 3),
            "extra_kw": round(extra_kw, 3),
            "pack_kw": round(pack_kw, 3),
            "hvac_kw": round(self.hvac_kw(), 3),
            "motor_kw": round(self.motor_kw(), 3),
            "charge_eff": self.charge_eff(),
            "load_override": override,
        }

    def current(self):
        """Effective pack current, A. Negative = discharging.

        Nothing but `power()`'s pack_kw over the open-circuit voltage — one
        identity, one place. (It used to be its own copy of the arithmetic,
        which is how the charge path came to ignore the load table.)
        """
        return round(self.power()["pack_kw"] * 1000.0 / max(1.0, self.ocv_pack()), 3)

    def ocv_pack(self):
        return NUM_CELLS * ocv(self.k["soc"])

    def step(self, dt):
        """Advance by dt seconds of *simulated* time.

        The clock scaling lives one level up, in `Simulator.step()`, which is
        the single authority on the effective time multiplier (see
        `Simulator.time_scale`). This method integrates whatever it is given,
        in chunks of at most MAX_SUBSTEP_S, so `step(3600)` and 3600 calls to
        `step(1)` land in the same place.
        """
        for h in substeps(dt):
            self._integrate(h)

    def _integrate(self, dt):
        """One bounded Euler step. dt <= MAX_SUBSTEP_S; see substeps()."""
        if dt <= 0:
            return
        k = self.k
        i = self.current()

        # SOC by coulomb counting: dSOC = I dt / capacity
        cap = max(0.1, k["capacity_ah"])
        k["soc"] = min(100.0, max(0.0, k["soc"] + (i * dt / 3600.0) / cap * 100.0))

        # pack temperature: I^2 R heating relaxing toward ambient
        r = max(0.0, k["internal_resistance_ohm"])
        # I^2 R, plus the charger's own losses landing in the pack (ASSERTED
        # fraction). Level 2 works out at ~1 °C/h, DC fast at ~20 °C/h.
        watts = i * i * r + self.charge_power_kw() * 1000.0 * max(0.0, k["charge_heat_frac"])
        heat = watts * dt * THERMAL_C_PER_J
        k["pack_temp_c"] = _clamp(
            k["pack_temp_c"] + heat - (k["pack_temp_c"] - k["ambient_c"]) * (dt / THERMAL_TAU_S),
            "pack_temp_c")

        # Cabin. It only reaches a setpoint if the system is POWERED and has a
        # heat engine running — the compressor or the PTC heater. A blower on
        # its own is ventilation: it drags the cabin toward outside air, fast.
        # (This used to head for the setpoint whenever `hvac_on` was set, even
        # with the car asleep and the blower drawing zero watts.)
        live = self.hvac_powered() and k["hvac_on"]
        fan_rate = dt / max(30.0, 300.0 - 30.0 * k["hvac_fan_speed"])
        if live and (self.ac_w() > 0 or self.ptc_w() > 0):
            target, rate = self.hvac_setpoint_c(), fan_rate
        elif live:
            target, rate = k["ambient_c"], fan_rate
        else:
            target, rate = k["ambient_c"], dt / 900.0
        k["cabin_temp_c"] = _clamp(
            k["cabin_temp_c"] + (target - k["cabin_temp_c"]) * min(1.0, rate), "cabin_temp_c")

        # Evaporator: the compressor pulls the coil toward EVAP_MIN_C, airflow
        # over it pushes it back up — more fan, warmer coil. With no compressor
        # running it follows the cabin.
        if self.ac_w() > 0:
            ev_target = EVAP_MIN_C + EVAP_FAN_RISE_C * int(k["hvac_fan_speed"])
        else:
            ev_target = k["cabin_temp_c"]
        k["evap_c"] = _clamp(k["evap_c"] + (ev_target - k["evap_c"]) * min(1.0, dt / 60.0),
                             "evap_c")

        # 12 V battery. While the DC-DC runs (READY, ON, or charging) it is
        # held up and recovers toward rest; with the car OFF the lamps come out
        # of it and it goes down. See the lv_volts row in COUPLINGS for why
        # there is no forced 14 V bus here.
        self._integrate_lv(dt)

        k["odometer_mi"] = min(999999.0, k["odometer_mi"] + k["speed_mph"] * dt / 3600.0)
        self.t += dt

    # ── derived values ───────────────────────────────────────────────────

    def _jitter(self, amp):
        if amp <= 0:
            return 0.0
        return (self._noise.random() - 0.5) * 2.0 * amp

    def cells(self):
        """96 cell-pair voltages in mV."""
        k = self.k
        i = self.current()
        # V = 96*OCV + I*R0.  I is negative while discharging, so the pack sags
        # under load and recovers at rest, which is what the contract means.
        pack_v = self.ocv_pack() + i * k["internal_resistance_ohm"]
        mean_mv = pack_v * 1000.0 / NUM_CELLS
        spread = k["cell_spread_mv"]
        weak_off = k["weak_cell_offset_mv"]
        if k["fault.cell_degraded"]:
            weak_off = min(weak_off, 0.0) - 350.0
            spread = max(spread, 40.0)
        idx = int(k["weak_cell_index"])
        amp = k["noise"]
        out = []
        for n in range(NUM_CELLS):
            mv = mean_mv + self.cell_shape[n] * spread + self._jitter(amp)
            if n == idx:
                mv += weak_off
            out.append(int(round(min(4999.0, max(1.0, mv)))))
        return out

    def temps_c(self):
        """The four pack sensors, °C (integers — the wire format is s8)."""
        base = int(round(self.k["pack_temp_c"]))
        t = [max(-40, min(80, base + o)) for o in self.temp_offsets]
        if self.k["fault.sensor_dropout"]:
            t[2] = -1
        return t

    def lamp_w(self):
        """Watts the exterior lamps are taking — the 12 V side's own load."""
        L = self.loads()
        return sum(L[n] for n in ("low_beam", "high_beam", "position", "fog",
                                  "turn", "brake_lamps", "reverse_lamps"))

    def _integrate_lv(self, dt):
        """The 12 V battery over time. ASSERTED, and see COUPLINGS.

        Converter running (READY, ON, or charging): held up, recovering toward
        LV_REST_V. Otherwise the lamps and the ACC modules come straight out of
        it, and a car left with its headlights on goes flat — which is the one
        12 V behaviour every owner has met.
        """
        k = self.k
        if k["start_state"] in ("ready", "on") or k["charging"]:
            v = k["lv_volts"] + (LV_REST_V - k["lv_volts"]) * min(1.0, dt / LV_RECOVER_TAU_S)
        else:
            w = self.lamp_w() + (LOADS_W["base_acc"] if k["start_state"] == "acc" else 0.0)
            v = k["lv_volts"] - w * (dt / 3600.0) / LV_BATTERY_WH * LV_SPAN_V
        k["lv_volts"] = _clamp(max(LV_FLOOR_V, v), "lv_volts")

    def lv_volts(self):
        v = self.k["lv_volts"]
        if self.k["fault.lv_battery_weak"]:
            v = min(v, 11.1)
            if self.k["hvac_on"] or self.k["start_state"] == "ready":
                v -= 0.4
        return round(max(0.0, v + self._jitter(self.k["noise"] * 0.005)), 3)

    def insulation_kohm(self):
        if self.k["fault.insulation_low"]:
            return 18
        return int(round(self.k["insulation_kohm"]))

    def range_km(self):
        """Guess-o-meter: usable energy over what the car is actually spending.

        Energy is the same three numbers the health tiles show — SOC ×
        capacity × pack voltage. Consumption is the ASSERTED 0.17 kWh/km of
        driving PLUS the accessories, charged at a nominal average speed: 3 kW
        of A/C at 45 km/h is another 0.067 kWh/km, and knocks about a quarter
        off the range. The real dash does exactly this, and the model used to
        leave the two sides unconnected (see COUPLINGS).
        """
        k = self.k
        kwh = k["capacity_ah"] * self.ocv_pack() * (k["soc"] / 100.0) / 1000.0
        aux_kwh_per_km = self.hvac_kw() / RANGE_NOMINAL_KMH
        return round(min(819.0, max(0.0, kwh / (RANGE_KWH_PER_KM + aux_kwh_per_km))), 1)

    def balancing(self):
        """Two bits per cell pair. Balancing only happens near the top of a
        charge, which is exactly why this group is still marked tentative."""
        cells = self.cells()
        if not (self.k["charging"] and self.k["soc"] > 80):
            return [0] * NUM_CELLS
        hi = max(cells)
        return [min(3, max(0, (hi - mv < 8) * 2 + (hi - mv < 3))) for mv in cells]

    def output_avail(self):
        """Fraction of full motor output the pack can give: fewer power
        bubbles when hot, cold or nearly empty. The same curve the v1 panel
        drew, moved here so the turtle lamp and the cluster agree."""
        temps = self.temps_c()
        pt = sum(temps) / len(temps)
        soc = self.k["soc"]
        avail = 1.0
        if pt > 45:
            avail = max(0.35, 1.0 - (pt - 45) / 30.0)
        if pt < 5:
            avail = min(avail, max(0.35, (pt + 15) / 20.0))
        if soc < 15:
            avail = min(avail, max(0.3, soc / 15.0))
        return round(avail, 3)

    # ── the cluster ──────────────────────────────────────────────────────

    def lamps(self):
        """Every indicator on the ZE0 cluster the model can drive, as booleans.

        The ones it cannot drive are in LAMPS_UNMODELLED and are always False
        — see `lamps_unmodelled()`. Aggregates: `master_red` is the red
        master warning (EV-system fault, or a door / parking brake while
        moving), `master_yellow` the yellow one (anything a driver should look
        at before the next stop).
        """
        k = self.k
        ss = k["start_state"]
        ready = ss == "ready"
        turn = k["turn_signal"]
        doors = any(k[f"door_{d}"] for d in DOOR_NAMES)
        moving = k["speed_mph"] > 3.0 and k["gear"] in DRIVE_GEARS
        charge_12v = self.lv_volts() < LV_WARN_V or bool(k["fault.lv_battery_weak"])
        ev_system = any(bool(k[f]) for f in EV_SYSTEM_FAULTS)
        power_limit = self.output_avail() < 0.6 or k["soc"] < 8.0
        low_battery = k["soc"] <= 10.0
        tpms = any(TPMS_SENSOR_MIN_PSI <= k[f"tpms_{t}"] < TPMS_WARN_PSI for t in TPMS_ORDER)
        headlight_warning = bool(k["headlights"]) and ss == "off"
        L = {
            "ready": ready,
            "turn_left": turn in ("left", "hazards"),
            "turn_right": turn in ("right", "hazards"),
            "hazards": turn == "hazards",
            "low_beam": bool(k["headlights"]),
            "high_beam": bool(k["high_beam"]),
            "position": bool(k["parking_lights"] or k["headlights"]),
            "fog": bool(k["fog_lights"]),
            "parking_brake": bool(k["handbrake"]),
            "door_ajar": doors,
            "plug_in": bool(k["charging"] or k["plugged_in"]),
            "charge_12v": charge_12v,
            "ev_system": ev_system,
            "power_limit": power_limit,
            "low_battery": low_battery,
            "tpms": tpms,
            "headlight_warning": headlight_warning,
            "security": bool(k["locked"]) and ss == "off",
            "eco": k["gear"] == "Eco",
        }
        L["master_red"] = ev_system or ((doors or bool(k["handbrake"])) and moving)
        L["master_yellow"] = (charge_12v or tpms or low_battery or power_limit
                              or headlight_warning)
        return L

    @staticmethod
    def lamps_unmodelled():
        return {n: False for n in LAMPS_UNMODELLED}

    def messages(self):
        """The dot-matrix line (Owner's Manual p. 2-23), in display order."""
        L = self.lamps()
        k = self.k
        out = []
        if L["low_battery"]:
            out.append("Battery level is low")
        if L["power_limit"]:
            out.append("Motor power is limited")
        if L["tpms"]:
            out.append("Check tire pressure")
        if L["door_ajar"]:
            out.append("Door open")
        if k["handbrake"] and k["gear"] != "P":
            out.append("Parking brake on")
        if k["charging"] or k["plugged_in"]:
            out.append("Charge connector connected")
        return out

    # ── the power switch ─────────────────────────────────────────────────

    def press_power(self, brake=None, hold=False):
        """One push of the push-button start. Returns what the car did.

        The ZE0's switch, from the 2012 Owner's Manual (pp. 5-7..5-13):

          * **No brake** — the switch cycles OFF -> ACC -> ON -> OFF (p. 5-8).
            One push from OFF is ACC, two is ON, three is back to OFF.
          * **Brake depressed** — the car goes READY "with the power switch in
            any position" (5-8, 5-11), but only with the selector in P or N
            (5-11: the EV will not operate otherwise). Anywhere else the push
            is refused. The wording of that refusal is ASSERTED.
          * **Pressed while READY** — the car switches OFF and the selector
            goes to P by itself (5-11 step 4; the 5-13 NOTE: "the vehicle
            automatically applies the P position when the power switch is in
            the OFF position"). It is not refused for being in gear.
          * **Charge connector connected** — no READY (p. 2-19). ACC and ON
            are still available; the message is ASSERTED wording.
          * **While moving** (ASSERTED, from 5-14 and the 5-9 emergency
            procedure): an ordinary push is refused. `hold=True` is the press
            held past two seconds, which is the emergency shut-off and the
            only way OFF is reached with the car rolling. The 5-9 procedure is
            a way to stop the EV system *while driving*, so it presupposes
            READY: a car that is OFF or ACC with a nonzero `speed_mph` (the
            knobs are independent, so an API caller can set that) gets the
            ordinary no-brake cycle, not an emergency stop it is not in a
            state to perform.

        `brake` defaults to reading the brake_pct knob, so a cockpit that
        drives the pedal needs no second control; pass True/False to override.
        LOCK (5-13, after OFF once a door is opened) is treated as OFF and is
        not modelled.
        """
        k = self.k
        if brake is None:
            brake = k["brake_pct"] > 0.0
        brake = bool(brake)
        ss = k["start_state"]
        moving = k["speed_mph"] > 1.0
        plugged = bool(k["charging"] or k["plugged_in"])

        def done(msg, accepted=True):
            return {"start_state": k["start_state"], "gear": k["gear"],
                    "message": msg, "accepted": bool(accepted)}

        if ss == "ready":
            if hold and moving:
                k["start_state"] = "off"
                return done("Emergency shut off")
            if moving:
                return done("Stop vehicle", False)
            k["start_state"] = "off"
            k["gear"] = "P"              # the car parks itself at OFF (5-13)
            return done("Power off")
        if brake:
            if plugged:
                return done("Remove charge connector", False)
            if k["gear"] not in ("P", "N"):
                return done("Shift to P or N", False)
            k["start_state"] = "ready"
            return done("Ready to drive")
        k["start_state"] = POWER_CYCLE.get(ss, "acc")
        return done(POWER_MESSAGES[k["start_state"]])

    # ── the machine-readable state ───────────────────────────────────────

    def state(self):
        k = self.k
        cells = self.cells()
        pack_v = round(sum(cells) / 1000.0, 2)
        i = round(self.current(), 3)
        temps = self.temps_c()
        temp_avg = sum(temps) / len(temps)
        seg = self.segments(cells)
        loads = {n: round(v, 1) for n, v in self.loads().items()}
        power = self.power()
        cabin = int(round(k["cabin_temp_c"]))
        ambient = int(round(k["ambient_c"]))
        evap = int(round(k["evap_c"]))
        st = {
            "simulated": True,
            "vehicle": self.vehicle,
            "seed": self.seed,
            "t": round(self.t, 3),
            "faults": sorted(n for n in k if n.startswith("fault.") and k[n]),
            # battery
            "soc": round(k["soc"], 2),
            "capacity_ah": round(k["capacity_ah"], 4),
            "soh": round(k["capacity_ah"] / NOMINAL_CAPACITY_AH * 100.0, 1),
            "hx": round(k["hx"], 2),
            "pack_v": pack_v,
            "ocv_pack_v": round(self.ocv_pack(), 2),
            "current_a": i,
            "power_kw": round(pack_v * i / 1000.0, 3),
            "discharging": i < 0,
            "charging": bool(k["charging"]),
            "plugged_in": bool(k["plugged_in"] or k["charging"]),
            "charger": k["charger"],
            "charge_power_kw": round(self.charge_power_kw(), 3),
            "charge_eff": self.charge_eff(),
            "wall_kw": round(self.wall_kw(), 3),
            "charge_taper_soc": round(self.taper_start_soc(), 2),
            "charge_temp_limit_c": round(k["charge_temp_limit_c"], 1),
            "charge_temp_limit_f": c_to_f(k["charge_temp_limit_c"]),
            "lv_volts": self.lv_volts(),
            "insulation_kohm": self.insulation_kohm(),
            "cells": cells,
            "cell_min": min(cells), "cell_max": max(cells),
            "cell_avg": round(sum(cells) / len(cells)),
            "cell_spread": max(cells) - min(cells),
            "segment_deltas": seg["deltas"],
            "cell_groups": seg["groups"],
            "balancing": self.balancing(),
            "temps_c": temps,
            "temps_f": [c_to_f(t) for t in temps],
            "temp_avg_c": round(temp_avg, 1),
            "temp_avg_f": c_to_f(temp_avg),
            "pack_temp_c": round(k["pack_temp_c"], 2),
            "pack_temp_f": c_to_f(k["pack_temp_c"]),
            "output_avail": self.output_avail(),
            # the load model
            "loads_w": loads,
            "load_total_w": round(sum(v for n, v in loads.items() if n != "regen"), 1),
            "loads_total_w": round(sum(v for n, v in loads.items() if n != "regen"), 1),
            # the whole budget in one place: wall -> charger -> loads -> pack
            "power": power,
            "pack_kw": power["pack_kw"],
            "motor_kw": round(self.motor_kw(), 3),
            "regen_kw": round(self.regen_kw(), 3),
            "hvac_kw": round(self.hvac_kw(), 3),
            # motion / body
            "speed_mph": round(k["speed_mph"], 1),
            "speed_kmh": round(k["speed_mph"] * KMH_PER_MPH, 1),
            "gear": k["gear"],
            "brake_pct": round(k["brake_pct"], 1),
            "accel_pedal_pct": round(k["accel_pedal_pct"], 1),
            "odometer_mi": int(round(k["odometer_mi"])),
            "range_km": self.range_km(),
            "handbrake": bool(k["handbrake"]),
            "locked": bool(k["locked"]),
            "doors_open": [d for d in DOOR_NAMES if k[f"door_{d}"]],
            "headlights": bool(k["headlights"]),
            "high_beam": bool(k["high_beam"]),
            "parking_lights": bool(k["parking_lights"]),
            "fog_lights": bool(k["fog_lights"]),
            "turn_signal": k["turn_signal"],
            "start_state": k["start_state"],
            "units_miles": bool(k["units_miles"]),
            "tpms_psi": [round(k[f"tpms_{t}"], 2) for t in TPMS_ORDER],
            # climate
            "hvac_on": bool(k["hvac_on"]),
            "hvac_ac_on": bool(k["hvac_ac_on"]),
            "hvac_fan_speed": int(k["hvac_fan_speed"]),
            "hvac_setpoint_f": round(k["hvac_setpoint_f"], 1),
            "hvac_setpoint_c": f_to_c(k["hvac_setpoint_f"]),
            "cabin_temp_c": cabin, "cabin_temp_f": c_to_f(cabin),
            "ambient_c": ambient, "ambient_f": c_to_f(ambient),
            "evap_c": evap, "evap_f": c_to_f(evap),
            "heater_level": int(k["heater_level"]),
            "sunload": int(k["sunload"]),
            "compressor_rpm": self.compressor_rpm(),
            # the cluster
            "lamps": self.lamps(),
            "lamps_unmodelled": self.lamps_unmodelled(),
            "messages": self.messages(),
        }
        return st

    def compressor_rpm(self):
        """Compressor speed, rpm — driven by A/C demand, not by the blower.

        It used to read `1500 + 130 * fan_speed`, which had the compressor
        speeding up because somebody turned the fan up in a cabin that was
        already cold. It rides `ac_demand()` now, over the band the real car
        was seen in (1600-2425 rpm engaged, 0 off).
        """
        if self.ac_w() <= 0:
            return 0
        span = COMPRESSOR_MAX_RPM - COMPRESSOR_IDLE_RPM
        return int(round(COMPRESSOR_IDLE_RPM + span * self.ac_demand()))

    def record(self, cells=True):
        """The dashboard's own vocabulary — what `vehicles/leaf_ze0.decode()`
        would produce if this state went over the wire. See record_from_state."""
        return record_from_state(self.state(), cells=cells)

    @staticmethod
    def segments(cells):
        """The LBC reports ten per-segment figures. The absolute scale of both
        is unverified (docs/SIGNALS.md marks group 05 bytes 26-65 verified as
        *positions*, not as units); the simulator picks a self-consistent one:
        deltas are the segment spread in mV, group voltages the segment sum in
        ~40 mV counts, which reproduces the real fixture's 71 / 957 magnitudes."""
        n = len(cells)
        deltas, groups = [], []
        for s in range(10):
            chunk = cells[s * n // 10:(s + 1) * n // 10] or [0]
            deltas.append(min(0xFFFE, (max(chunk) - min(chunk)) * 10))
            mean = sum(chunk) / len(chunk)
            groups.append(min(4999, int(round(mean * (n / 10.0) / 40.1))))
        return {"deltas": deltas, "groups": groups}


# ── the record: model state in the decoders' vocabulary ──────────────────
#
# `state()` speaks the model's language (`doors_open`, `ambient_c`,
# `hvac_setpoint_f`); the dashboard's tiles read what leaf_decoders.py
# produces (`door_driver`, `hvac_ambient_c/f`, `hvac_target_f`). This is the
# one mapping between them, and it is held to a hard standard: for every key
# the real decoders also produce, `record_from_state(st)` must equal
# encode(st) -> decode(). tests/test_sim_record.py enforces it, which is why a
# few values below are quantised the way the wire quantises them (byte 12 of
# HVAC group 10 carries the setpoint at ~0.48 °F; 0x385 carries tyre pressure
# in quarter-psi; 0x5A9 carries range in fifths of a km).

def _setpoint_as_decoded_f(f):
    """hvac_setpoint_f after a trip through byte 12 (encode.setpoint_raw and
    leaf_decoders.decode_hvac, both from docs/SIGNALS.md)."""
    raw = max(0, min(255, int(round(111.0 + (f - 60.0) * 62.0 / 30.0))))
    tf = 60.0 + (raw - 111) * 30.0 / 62.0
    return round(min(95.0, max(55.0, tf)), 1)


def record_from_state(st, cells=True):
    """A reader-shaped record from one Leaf state dict (see the note above)."""
    k = st
    temps_c = list(k["temps_c"])
    temp_avg = sum(temps_c) / len(temps_c)
    cell_list = list(k["cells"])
    mn, mx = min(cell_list), max(cell_list)
    i = k["current_a"]
    doors = set(k.get("doors_open") or ())
    door = {d: (d in doors) for d in DOOR_NAMES}
    doors_raw = 0
    for d in doors:
        doors_raw |= DOOR_BITS.get(d, 0)
    if k["parking_lights"]:
        doors_raw |= 0x04
    if k["headlights"]:
        doors_raw |= 0x02
    # 0x5C5 carries the odometer in the dash's units, as a u24
    if k["units_miles"]:
        odo_raw = int(k["odometer_mi"])
        odo_mi, odo_km = odo_raw, round(odo_raw * 1.609344)
    else:
        odo_raw = int(round(k["odometer_mi"] / 0.621371))
        odo_mi, odo_km = round(odo_raw * 0.621371), odo_raw
    range_raw = max(0, min(0xFFF, int(round(k["range_km"] * 5))))
    psi = [max(0, min(255, int(round(p * 4)))) / 4.0 for p in k["tpms_psi"]]
    brake_raw = max(0, min(255, int(round(k["brake_pct"] * 1.39))))
    target_f = _setpoint_as_decoded_f(k["hvac_setpoint_f"])
    on = bool(k["hvac_on"])
    volts = FAN_VOLTS.get(int(k["hvac_fan_speed"]), 0) if on else 0
    rec = {
        # LBC group 01
        "soc": k["soc"],
        "pack_v": k["pack_v"],
        "current_a": i,
        "power_kw": k["power_kw"],
        "discharging": k["discharging"],
        "capacity_ah": k["capacity_ah"],
        "soh": k["soh"],
        "hx": k["hx"],
        "lv_volts": k["lv_volts"],
        "insulation_kohm": k["insulation_kohm"],
        # sensor 1 is the coarse ±0.5 A one on this car (and the one the
        # dropout fault turns to nonsense); group 05 saturates at ±32 A
        "hv_current1_a": (round(0x7FFFFFFF / 1024.0, 3)
                          if "fault.sensor_dropout" in (k.get("faults") or ()) else round(i * 2.0) / 2.0),
        "hv_current2_a": i,
        "g05_current_a": round(max(-32768, min(32767, int(round(i * 1024)))) / 1024.0, 3),
        # LBC group 04
        "temps": temps_c,
        "temps_c": temps_c,
        "temps_f": [c_to_f(t) for t in temps_c],
        "temp_avg_c": round(temp_avg, 1),
        "temp_avg_f": c_to_f(temp_avg),
        # LBC groups 02 / 06
        "cell_count": len(cell_list),
        "cell_min": mn, "cell_max": mx,
        "cell_avg": round(sum(cell_list) / len(cell_list)), "cell_spread": mx - mn,
        "cell_min_idx": cell_list.index(mn), "cell_max_idx": cell_list.index(mx),
        "balancing_active": sum(1 for b in k["balancing"] if b),
        # HVAC amp
        "cabin_temp_c": k["cabin_temp_c"], "cabin_temp_f": c_to_f(k["cabin_temp_c"]),
        "hvac_ambient_c": k["ambient_c"], "hvac_ambient_f": c_to_f(k["ambient_c"]),
        "hvac_evap_c": k["evap_c"], "hvac_evap_f": c_to_f(k["evap_c"]),
        "hvac_sunload": k["sunload"],
        "hvac_decode": "tentative",
        "hvac_on": on,
        "hvac_ac_on": on and bool(k["hvac_ac_on"]),
        "hvac_fan_on": on and volts > 0,
        "hvac_fan_speed": int(k["hvac_fan_speed"]) if on else 0,
        "hvac_blower_v": volts,
        "hvac_compressor_rpm": k["compressor_rpm"],
        "hvac_target_f": target_f,
        "hvac_target_c": round((target_f - 32) * 5 / 9, 1),
        "hvac_heater_level": k["heater_level"],
        # Car-CAN body / motion
        "gear": k["gear"],
        "start_state_name": k["start_state"],
        "speed_mph": k["speed_mph"],
        "speed_kmh": k["speed_kmh"],
        "odometer_mi": odo_mi,
        "odometer_km": odo_km,
        "range_km": range_raw / 5.0,
        "range_mi": round(range_raw / 5.0 * 0.621371, 1),
        "soh_dash_pct": max(0, min(127, int(round(k["soh"])))),
        "handbrake": k["handbrake"],
        "units_miles": k["units_miles"],
        "doors_raw": doors_raw,
        "door_driver": door["driver"], "door_pass": door["pass"],
        "door_rl": door["rl"], "door_rr": door["rr"], "door_hatch": door["hatch"],
        "door_front": door["driver"] or door["pass"],
        "door_rear": door["rl"] or door["rr"],
        "door_trunk": door["hatch"],
        "door_any": bool(doors),
        "headlights": k["headlights"],
        "high_beam": k["high_beam"],
        "parking_lights": k["parking_lights"],
        "fog_lights": k["fog_lights"],
        "turn_signal": k["turn_signal"],
        "brake_on": brake_raw > 0,
        "brake_pct": k["brake_pct"],
        "locked": k["locked"],
        "tpms_psi": psi,
        "tpms_kpa": [round(p * 6.89476, 1) for p in psi],
        # the cluster and the load model, for the sim page
        "lamps": dict(k["lamps"]),
        "lamps_unmodelled": dict(k["lamps_unmodelled"]),
        "messages": list(k["messages"]),
        "loads_w": dict(k["loads_w"]),
        # the power budget, so the cockpit can show wall -> charger -> loads ->
        # pack without doing the arithmetic a second time
        "power": dict(k["power"]),
        "pack_kw": k["pack_kw"],
        "loads_total_w": k["loads_total_w"],
        "charging": k["charging"],
        "plugged_in": k["plugged_in"],
        # the stamp, in every single record
        "simulated": True,
    }
    if cells:
        rec["cells"] = cell_list
    return rec
