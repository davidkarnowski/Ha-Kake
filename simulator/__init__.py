# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ha-Kake vehicle simulator — a car made of arithmetic.

    from simulator import make_sim, KNOBS

    sim = make_sim(vehicle="leaf_ze0", knobs={"soc": 42.0}, seed=1)
    sim.step(1.0)
    sim.respond("2101", "79B", "7BB")     -> real-looking ELM327 lines
    sim.frames("60D", 0.2)                -> passive monitor lines
    sim.set(**{"fault.cell_degraded": True})
    sim.record()                          -> the dashboard's decoder vocabulary

This is **not** `ReplayELM`. Replay serves recorded fixtures; this generates
signals from a physical model, so conditions that cannot be produced on demand
in a real car — a collapsed cell pair, low isolation resistance, a sleeping
ECU, a negative response code — become testable.

Every value it produces is simulated. `state()["simulated"]` is True and stays
True; a green simulator run is never evidence about a real car.
"""

import json
import os

from . import encode, lancer, model
from .knobs import KnobSet   # noqa: F401  (re-export for tooling)

__all__ = ["make_sim", "Simulator", "KNOBS", "VEHICLES", "knob_schema",
           "SCENARIO_DIR", "scenario_names", "TIME_SCALE_MIN", "TIME_SCALE_MAX"]

VEHICLES = {"leaf_ze0": model.LeafModel, "lancer_2009": lancer.LancerModel}

# The module-level knob registry. Knobs are per vehicle — a Leaf has no `rpm`
# and a Lancer has no `cell_spread_mv` — so this is keyed by profile name.
KNOBS = {"leaf_ze0": model.KNOBS, "lancer_2009": lancer.KNOBS}

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")

DEFAULT_VEHICLE = "leaf_ze0"

# ELM327 identity the simulated adapter reports
ADAPTER_ID = "ELM327 v1.5"

# ── the clock, and the one place it is decided ───────────────────────────
#
# There are two ways to ask for compressed time and they used to MULTIPLY:
# `--speed 120` on the command line against a scenario carrying
# `clock_scale: 120` gave 14400x, which put eight seconds of startup thirty-two
# simulated hours into a charge that had already finished. Silent, and wrong.
#
# The rule now, and it is the only rule:
#
#   * `--speed X` given explicitly  ->  the effective multiplier IS X.
#     It OVERRIDES the scenario's `clock_scale`; it does not multiply it.
#     The command line is the human in the room and wins.
#   * no `--speed`                  ->  the scenario's `clock_scale` knob,
#     which `POST /sim/knobs` can still change mid-run.
#   * neither                       ->  1.0, real time.
#
# The result is clamped to [TIME_SCALE_MIN, TIME_SCALE_MAX] and is reported
# everywhere it could surprise anyone: on the startup banner, in `/sim/info`,
# in `state()["time_scale"]`, and on the control panel's readout.
TIME_SCALE_MIN = 0.01
TIME_SCALE_MAX = 3600.0

# The transport in elm327.py (SimELM) applies its own `speed` multiplier to dt
# before it ever reaches `Simulator.step()`. That multiplier is the same
# `--speed`, arriving by a second road, and it is exactly half of what made the
# old behaviour multiply. `outer_scale` is how the rig tells the core "this
# much has already been applied to the dt you are about to get", so the core
# can divide it back out and the product is the resolved scale and nothing
# else. `--adapter sim` reaches SimELM through HAKAKE_SIM_SPEED without passing
# through the rig at all, so the core reads that variable itself.
SIM_SPEED_ENV = "HAKAKE_SIM_SPEED"


def _env_speed():
    """`--speed` as web/reader.py passes it to the sim transport, or None."""
    raw = os.environ.get(SIM_SPEED_ENV)
    if not raw:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 and v != 1.0 else None


def knob_schema(vehicle=DEFAULT_VEHICLE):
    """JSON-ready description of every knob of one profile."""
    if vehicle not in KNOBS:
        raise ValueError(f"unknown vehicle {vehicle!r} (have: {', '.join(KNOBS)})")
    return KNOBS[vehicle].schema()


def scenario_names():
    if not os.path.isdir(SCENARIO_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SCENARIO_DIR) if f.endswith(".json"))


class Simulator:
    """A whole car: physical model + wire encoder + knobs + scenario clock."""

    simulated = True

    def __init__(self, vehicle=None, knobs=None, seed=1, scenario=None, speed=None):
        self.seed = int(seed)
        self.warnings = []
        self.notes = []
        self.speed = None          # explicit --speed override, or None
        self._outer_scale = 1.0    # multiplier a transport already applied
        self._at_tx = None
        self._at_rx = None
        self._timeline = []
        self._fired = 0
        self.scenario = None
        self._bind(vehicle or DEFAULT_VEHICLE)
        env = _env_speed()
        if speed is not None:
            self.set_speed(speed)
        elif env is not None:
            # `--adapter sim --speed X` arrives here and nowhere else: reader.py
            # puts it in the environment and elm327's SimELM multiplies dt by it
            # before we see it, so it is both the override and the outer scale.
            self.set_speed(env, outer=env)
        if scenario:
            self.load_scenario(scenario)
        if knobs:
            self.set(**dict(knobs))

    # ── construction ─────────────────────────────────────────────────────

    def _bind(self, vehicle):
        if vehicle not in VEHICLES:
            raise ValueError(f"unknown vehicle {vehicle!r} (have: {', '.join(VEHICLES)})")
        self.vehicle = vehicle
        self.model = VEHICLES[vehicle](self.seed)

    # ── knobs ────────────────────────────────────────────────────────────

    def set(self, _knobs=None, **kw):
        """Apply knobs. Returns {name: applied value} — including anything a
        knob set as a side effect (soh moves capacity_ah; doors moves the five
        per-corner flags). Unknown name -> ValueError with near matches."""
        want = dict(_knobs or {})
        want.update(kw)
        applied = {}
        for name, value in want.items():
            knob = self.model.knobs.resolve(name)
            coerced, warn = knob.coerce(value)
            if warn:
                self.warnings.append(warn)
            self.model.k[name] = coerced
            applied[name] = coerced
            applied.update(self.model.on_set(name, coerced))
        return applied

    def get_knobs(self):
        return dict(self.model.k)

    def knob_schema(self):
        return self.model.knobs.schema()

    def faults(self):
        """Currently active faults, keyed by the knob name that turns them on."""
        return {n: True for n in self.model.k
                if n.startswith("fault.") and self.model.k[n]}

    # ── time ─────────────────────────────────────────────────────────────

    def set_speed(self, speed, outer=1.0):
        """Install an explicit `--speed` override (None removes it).

        `outer` is the multiplier the caller has already applied, or will
        apply, to the dt it hands `step()` — see the SIM_SPEED_ENV note above.
        """
        self.speed = None if speed is None else max(TIME_SCALE_MIN,
                                                    min(TIME_SCALE_MAX, float(speed)))
        self._outer_scale = max(TIME_SCALE_MIN, float(outer or 1.0))
        self._note_shadowed_clock()
        return self.time_scale()

    def _note_shadowed_clock(self):
        """Say it when --speed is shadowing a scenario's clock_scale.

        Silence is what made the 14400x bug expensive; a note costs nothing.
        """
        clock = float(self.model.k.get("clock_scale", 1.0) or 1.0)
        if self.speed is None or clock == 1.0 or clock == self.speed:
            return
        note = (f"--speed {self.speed} overrides clock_scale {clock}"
                + (f" from scenario {self.scenario!r}" if self.scenario else "")
                + f"; effective time scale {self.time_scale()}x")
        if note not in self.notes:
            self.notes.append(note)

    def time_scale(self):
        """The effective multiplier: simulated seconds per real second.

        One number, resolved in one place, clamped to [0.01, 3600]. An
        explicit --speed overrides the scenario's clock_scale knob rather than
        multiplying it.
        """
        raw = self.speed if self.speed is not None else float(
            self.model.k.get("clock_scale", 1.0) or 1.0)
        return max(TIME_SCALE_MIN, min(TIME_SCALE_MAX, float(raw)))

    def time_scale_info(self):
        """Where the effective multiplier came from, for humans and for the
        control panel — so it can never be a surprise again."""
        clock = float(self.model.k.get("clock_scale", 1.0) or 1.0)
        eff = self.time_scale()
        if self.speed is not None:
            src = "--speed (overrides the scenario's clock_scale)"
        elif clock != 1.0:
            src = "clock_scale knob"
        else:
            src = "real time"
        return {"time_scale": round(eff, 4), "source": src,
                "speed_override": self.speed, "clock_scale": clock,
                "min": TIME_SCALE_MIN, "max": TIME_SCALE_MAX,
                "clamped": eff != (self.speed if self.speed is not None else clock)}

    def step(self, dt):
        """Advance the model by `dt` seconds of real time.

        `dt * time_scale()` seconds of simulated time pass. The interval is
        walked in bounded sub-steps (simulator.model.MAX_SUBSTEP_S) so the
        integrators cannot blow up at a large time scale, and so a scenario
        timeline entry fires at its own point on the way through rather than
        all of them firing up front.
        """
        total = float(dt) * self.time_scale() / self._outer_scale
        if total <= 0:
            return
        for h in model.substeps(total):
            self._fire(self.model.t + h)
            self.model.step(h)

    def _fire(self, t):
        while self._fired < len(self._timeline) and self._timeline[self._fired][0] <= t + 1e-9:
            _, sets = self._timeline[self._fired]
            self._fired += 1
            self.set(**sets)

    def state(self):
        st = self.model.state()
        st["faults"] = sorted(self.faults())
        st["scenario"] = self.scenario
        # the effective clock, carried with the state so nothing that reads
        # the model can be surprised by how fast it is running (see the
        # TIME_SCALE note at the top of this module)
        st["time_scale"] = round(self.time_scale(), 4)
        st["time_scale_source"] = self.time_scale_info()["source"]
        return st

    def record(self, cells=True):
        """The model's state in the dashboard's own vocabulary — the keys
        `vehicles/<profile>.decode()` produces, plus the sim-only extras
        (`lamps`, `loads_w`, `messages`) and the marker fields.

        Optional per core: a model without `record()` (the Lancer, today)
        raises NotImplementedError, which is how a control server answers
        501 and a page knows not to draw that profile's tiles.
        """
        fn = getattr(self.model, "record", None)
        if fn is None:
            raise NotImplementedError(f"{self.vehicle} has no record(); "
                                      f"read state() instead")
        rec = fn(cells=cells)
        rec.update({"simulated": True, "vehicle": self.vehicle, "seed": self.seed,
                    "scenario": self.scenario, "sim_t": round(self.model.t, 3),
                    "time_scale": round(self.time_scale(), 4),
                    "faults": sorted(self.faults())})
        return rec

    def press_power(self, brake=None, hold=False):
        """One push of the car's power switch — see `LeafModel.press_power`.

        Optional per core, like `record()`: a model without a power switch
        (the Lancer, which has a key) raises NotImplementedError, which is how
        the control API answers 501 and the cockpit knows not to draw a button.
        """
        fn = getattr(self.model, "press_power", None)
        if fn is None:
            raise NotImplementedError(f"{self.vehicle} has no press_power(); "
                                      f"set the ignition knob instead")
        out = dict(fn(brake=brake, hold=hold))
        out.update({"simulated": True, "vehicle": self.vehicle,
                    "messages": list(self.model.messages())
                    if hasattr(self.model, "messages") else []})
        return out

    def can_press_power(self):
        return callable(getattr(self.model, "press_power", None))

    def can_record(self):
        return callable(getattr(self.model, "record", None))

    # ── scenarios ────────────────────────────────────────────────────────

    def clear_scenario(self):
        """Drop the timeline and forget the scenario name; every knob stays
        exactly where it is (free-running from here)."""
        self._timeline = []
        self._fired = 0
        self.scenario = None

    def load_scenario(self, path_or_name):
        """Apply a scenario: a JSON object with optional `vehicle`, `seed`,
        `knobs` and a `timeline` of {t, set} entries (t = seconds of simulated
        time from now). Entries at t <= 0 fire immediately."""
        data = load_scenario_file(path_or_name)
        veh = data.get("vehicle")
        seed = data.get("seed")
        if seed is not None:
            self.seed = int(seed)
        if (veh and veh != self.vehicle) or seed is not None:
            if veh and veh != self.vehicle:
                self.notes.append(f"scenario {data.get('name', path_or_name)!r} "
                                  f"switched the vehicle to {veh}")
            self._bind(veh or self.vehicle)
        self.scenario = data.get("name") or str(path_or_name)
        self.model.t = 0.0
        self._timeline = sorted(
            ((float(e.get("t", 0.0)), dict(e.get("set") or {})) for e in data.get("timeline") or []),
            key=lambda e: e[0])
        self._fired = 0
        if data.get("knobs"):
            self.set(**dict(data["knobs"]))
        self._note_shadowed_clock()
        self._fire(0.0)

    # ── the wire ─────────────────────────────────────────────────────────

    def respond(self, cmd, tx=None, rx=None):
        """ELM327 response lines for one command at the given header pair.

        Unknown request -> ["NO DATA"]; never invented data. AT commands are
        answered the way a real adapter answers them, and ATSH/ATCRA are
        remembered so a caller that only sends AT+ATMA still works.
        """
        cmd = (cmd or "").strip()
        up = cmd.upper()
        if not cmd:
            return []
        if up.startswith("AT"):
            return self._at(up[2:].strip())
        if self.model.k.get("fault.adapter_silent"):
            return []            # a dead adapter says nothing at all, not "NO DATA"

        tx = (tx or self._at_tx or "").upper().replace(" ", "")
        rx = (rx or self._at_rx or "").upper().replace(" ", "")
        st = self.model.state()
        nrc = int(self.model.k["ecu_nrc_code"]) if self.model.k.get("fault.ecu_nrc") else None

        if self.vehicle == "lancer_2009":
            if self.model.k.get("fault.car_asleep"):
                return ["NO DATA"]
            lines = lancer.respond(cmd, tx or "7E0", rx, st, nrc)
            return lines if lines is not None else ["NO DATA"]

        # Leaf: the LBC at 79B/7BB and the HVAC amp at 744/764
        if tx in ("744", "745") or rx == "764":
            if self.model.k.get("fault.car_asleep"):
                return ["NO DATA"]
            r = rx or "764"
            if nrc is not None:
                return encode.negative(r, 0x21, nrc)
            lines = encode.hvac_response(up, st, r)
            # the real amp answers NRC 0x12 for every group it does not implement
            return lines if lines is not None else encode.negative(r, 0x21, 0x12)

        if tx in ("", "79B") or rx == "7BB":
            if self.model.k.get("fault.car_asleep"):
                return ["NO DATA"]
            r = rx or "7BB"
            if nrc is not None:
                return encode.negative(r, 0x21, nrc)
            lines = encode.lbc_response(up, st, r,
                                        bool(self.model.k.get("fault.sensor_dropout")))
            return lines if lines is not None else ["NO DATA"]

        return ["NO DATA"]

    def _at(self, body):
        if body in ("Z", "I", "@1", "WS"):
            return [ADAPTER_ID]
        if body.startswith("SH"):
            self._at_tx = body[2:].strip().replace(" ", "") or None
            return ["OK"]
        if body.startswith("CRA"):
            self._at_rx = body[3:].strip().replace(" ", "") or None
            return ["OK"]
        if body == "AR":
            self._at_rx = None
            return ["OK"]
        if body == "MA":
            return self.frames(self._at_rx, 0.2) if self._at_rx else []
        return ["OK"]

    def frames(self, can_id, secs=0.2):
        """Passive monitor lines for one CAN id over `secs` of simulated time.

        The state does not advance during the window — a real 0.2 s capture of
        a broadcast id is a run of near-identical frames, which is what the
        fixtures in tests/fixtures/ look like.
        """
        if can_id is None:
            return []
        cid = str(can_id).upper().replace("0X", "").replace(" ", "")
        if self.model.k.get("fault.adapter_silent"):
            return []
        if self.vehicle != "leaf_ze0":
            return []
        if self.model.k.get("fault.car_asleep"):
            return []
        st = self.model.state()
        one = encode.frame_line(cid, st)
        if one is None:
            return []
        n = encode.frame_count(cid, secs)
        out = []
        noisy = bool(self.model.k.get("fault.bus_noise"))
        for i in range(n):
            if noisy and i % 3 == 1:
                # what ATCAF1 does to raw frames, plus a line cut short by BLE
                out.append("<DATA ERROR")
                out.append(" ".join(one.split()[:3]))
            out.append(one)
        return out


def load_scenario_file(path_or_name):
    """A shipped scenario by name, or any JSON file by path."""
    p = str(path_or_name)
    if os.path.sep in p or p.endswith(".json"):
        path = p
    else:
        path = os.path.join(SCENARIO_DIR, p + ".json")
    if not os.path.exists(path):
        have = ", ".join(scenario_names())
        raise ValueError(f"no scenario {path_or_name!r} (shipped: {have})")
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a scenario must be a JSON object")
    for e in data.get("timeline") or []:
        if not isinstance(e, dict) or "set" not in e:
            raise ValueError(f"{path}: every timeline entry needs 't' and 'set', got {e!r}")
    return data


def make_sim(vehicle=None, knobs=None, seed=1, scenario=None, speed=None):
    """Build a simulator. See the module docstring and docs/SIMULATOR.md."""
    return Simulator(vehicle=vehicle, knobs=knobs, seed=seed, scenario=scenario,
                     speed=speed)
