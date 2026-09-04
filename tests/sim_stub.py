# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A minimal simulator core implementing docs/SIMULATOR_CONTRACT.md.

The transport, the CLI and the control API are built against the *contract*,
not against the core, so this stub exists to prove that: everything in
test_sim_transport.py and test_sim_control.py runs against an object with
nothing in it but the documented surface. If a test needs something this stub
does not have, the plumbing has grown a dependency on the core's internals and
that is the bug.

The real core (simulator/) is exercised too, wherever it is importable — see
the `real_core` tests. This is a stand-in, never a substitute.
"""

import difflib

KNOBS = {
    "soc":                  {"type": "float", "unit": "%",  "min": 0, "max": 100, "default": 80.0,
                             "help": "State of charge"},
    "capacity_ah":          {"type": "float", "unit": "Ah", "min": 1, "max": 100, "default": 66.0,
                             "help": "Usable pack capacity"},
    "pack_temp_c":          {"type": "float", "unit": "C",  "min": -30, "max": 70, "default": 22.0,
                             "help": "Pack temperature"},
    "current_a":            {"type": "float", "unit": "A",  "min": -400, "max": 200, "default": 0.0,
                             "help": "Pack current; negative is discharge"},
    "speed_mph":            {"type": "float", "unit": "mph", "min": 0, "max": 100, "default": 0.0,
                             "help": "Road speed"},
    "gear":                 {"type": "str",  "unit": "",   "min": None, "max": None, "default": "P",
                             "help": "Selected gear"},
    "ambient_c":            {"type": "float", "unit": "C",  "min": -40, "max": 55, "default": 20.0,
                             "help": "Outside air temperature"},
    "cell_spread_mv":       {"type": "float", "unit": "mV", "min": 0, "max": 500, "default": 12.0,
                             "help": "Spread between the highest and lowest cell"},
    "fault.cell_degraded":  {"type": "bool", "unit": "",   "min": None, "max": None, "default": False,
                             "help": "One cell drops well below the pack mean"},
    "fault.insulation_low": {"type": "bool", "unit": "",   "min": None, "max": None, "default": False,
                             "help": "Insulation resistance falls below the warning threshold"},
    "fault.car_asleep":     {"type": "bool", "unit": "",   "min": None, "max": None, "default": False,
                             "help": "No ECU answers — the car is off"},
}


class StubSim:
    """Coulomb counting and nothing else. Enough to exercise the plumbing."""

    def __init__(self, vehicle="leaf_ze0", knobs=None, seed=1, scenario=None):
        self.vehicle = vehicle
        self.seed = seed
        self.scenario = scenario or "idle"
        self.t = 0.0
        self._k = {k: v["default"] for k, v in KNOBS.items()}
        self.steps = 0
        if knobs:
            self.set(**knobs)

    # ── contract ─────────────────────────────────────────────────────────

    def step(self, dt):
        self.t += float(dt)
        self.steps += 1
        cap = max(1.0, float(self._k["capacity_ah"]))
        self._k["soc"] = max(0.0, min(100.0, self._k["soc"]
                                      + (self._k["current_a"] * (dt / 3600.0) / cap) * 100.0))

    def state(self):
        v = self.pack_voltage()
        return {"sim_time_s": round(self.t, 3),
                "soc": round(self._k["soc"], 3),
                "pack_voltage_v": round(v, 2),
                "current_a": round(self._k["current_a"], 2),
                "pack_temp_c": round(self._k["pack_temp_c"], 2),
                "cell_spread_mv": round(self.cell_spread(), 1),
                "speed_mph": round(self._k["speed_mph"], 1),
                "gear": self._k["gear"],
                "faults": self.faults()}

    def cell_spread(self):
        return self._k["cell_spread_mv"] + (180.0 if self._k["fault.cell_degraded"] else 0.0)

    def set(self, **knobs):
        unknown = [k for k in knobs if k not in KNOBS]
        if unknown:
            near = {k: difflib.get_close_matches(k, list(KNOBS), n=3, cutoff=0.3) for k in unknown}
            raise ValueError(f"unknown knob(s) {', '.join(unknown)}; did you mean: {near}")
        for k, v in knobs.items():
            if KNOBS[k]["type"] == "bool":
                v = bool(v)
            elif KNOBS[k]["type"] == "float":
                v = float(v)
            self._k[k] = v
        return {k: self._k[k] for k in knobs}

    def get_knobs(self):
        return dict(self._k)

    def knob_schema(self):
        return {k: dict(v) for k, v in KNOBS.items()}

    def faults(self):
        return {k.split(".", 1)[1]: True for k, v in self._k.items()
                if k.startswith("fault.") and v}

    def load_scenario(self, path_or_name):
        if path_or_name not in ("idle", "drive", "charge", "degraded_pack"):
            raise ValueError(f"no such scenario: {path_or_name}")
        self.scenario = path_or_name
        if path_or_name == "drive":
            self.set(gear="D", speed_mph=45, current_a=-80)
        elif path_or_name == "charge":
            self.set(gear="P", speed_mph=0, current_a=30)
        elif path_or_name == "degraded_pack":
            self.set(**{"fault.cell_degraded": True})
        return None

    # ── encoding: the round trip the simulator exists to make possible ───
    #
    # Enough of the Leaf's LBC groups 01 and 02 to drive the real decoders, so
    # the plumbing can be shown working end to end without the core. It is a
    # stand-in: the real model owns physical fidelity, this owns byte layout
    # only, and it deliberately mirrors docs/SIGNALS.md rather than
    # leaf_decoders.py so the two can disagree.

    @staticmethod
    def isotp(rx, service_echo, payload):
        """Payload → the ELM327 lines a real adapter prints for one response."""
        full = bytes(service_echo) + bytes(payload)
        rx = rx or "7BB"
        lines = [f"{rx} 10 {len(full):02X} " + " ".join(f"{b:02X}" for b in full[:6])]
        rest, seq = full[6:], 0x21
        while rest:
            chunk, rest = rest[:7], rest[7:]
            lines.append(f"{rx} {seq:02X} " + " ".join(f"{b:02X}" for b in chunk))
            seq = 0x21 + ((seq - 0x20) % 15)
        return lines

    def group01(self):
        p = bytearray(39)

        def put(off, val, n, signed=False):
            p[off:off + n] = int(round(val)).to_bytes(n, "big", signed=signed)

        amps = self._k["current_a"]
        put(0, amps * 1024, 4, signed=True)          # hv_current1_a
        put(6, amps * 1024, 4, signed=True)          # hv_current2_a
        put(18, self.pack_voltage() * 100, 2)        # pack_v
        put(20, 12.4 * 1024, 2)                      # lv_volts
        put(22, 12 if self._k["fault.insulation_low"] else 1200, 2)   # insulation_kohm
        put(26, 87.5 * 100, 2)                       # hx
        put(29, self._k["soc"] * 10000, 3)           # soc
        put(33, self._k["capacity_ah"] * 10000, 3)   # capacity_ah
        return p

    def group02(self):
        """96 cell-pair millivolts; the degraded cell sits well below the rest."""
        mean = self.pack_voltage() / 96.0 * 1000.0
        half = self.cell_spread() / 2.0
        cells = [mean + half * ((i % 2) * 2 - 1) for i in range(96)]
        if self._k["fault.cell_degraded"]:
            cells[42] = mean - self.cell_spread()
        out = bytearray()
        for mv in cells:
            out += int(round(max(0, min(65535, mv)))).to_bytes(2, "big")
        return out

    def pack_voltage(self):
        return 96 * (3.4 + 0.006 * self._k["soc"]) - self._k["current_a"] * 0.05

    def respond(self, cmd, tx, rx):
        if self._k["fault.car_asleep"]:
            return ["NO DATA"]
        if tx == "79B" and cmd == "2101":
            return self.isotp(rx, b"\x61\x01", self.group01())
        if tx == "79B" and cmd == "2102":
            return self.isotp(rx, b"\x61\x02", self.group02())
        return ["NO DATA"]

    def frames(self, can_id, secs):
        if self._k["fault.car_asleep"] or not can_id:
            return []
        n = max(1, int(secs * 4))
        return [f"{can_id} 08 00 {i:02X}" for i in range(n)]


def make_sim(vehicle="leaf_ze0", knobs=None, seed=1, scenario=None):
    return StubSim(vehicle=vehicle, knobs=knobs, seed=seed, scenario=scenario)
