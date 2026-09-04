# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Model and encoder — 2009 Mitsubishi Lancer ES (standard SAE J1979).

Nothing here was reverse engineered: every PID is public spec, and the
defaults below are the values this car actually read at idle on 2026-08-28
(tests/fixtures/lancer_idle_raw_20260828.json). The stored-code list is the
car's real fault set, which is what makes the multi-frame mode-03 answer worth
simulating — it is 12 codes, three ISO-TP frames past the first.

Read-only, like the rest of the project: mode 04 (clear codes) is not
implemented, and never will be.
"""

import random

from .knobs import KnobSet
from .encode import isotp, negative, u16, ZERO_PAD
from .model import substeps

FUEL_SYS_CODES = {"open loop (warm-up)": 1, "closed loop": 2, "open loop (load/decel)": 4,
                  "open loop (fault)": 8, "closed loop (fault)": 16}

# this car's real stored codes, 2026-08-28 (12 engine codes -> multi-frame 03)
REAL_ENGINE_DTCS = ("P0131 P0132 P0134 P2195 P0171 P0122 P0223 "
                    "P1233 P1234 P1235 P1590 P0868")
REAL_PENDING_DTCS = "P0131 P2195"
REAL_TRANS_DTCS = "P0868"


def build_knobs():
    K = KnobSet()
    a = K.add
    K.group("engine")
    a("rpm", "float", 719.0, "Engine speed (PID 010C)", "rpm", 0.0, 8000.0, label="Engine speed")
    a("speed_mph", "float", 0.0, "Vehicle speed (PID 010D, sent in km/h)", "mph", 0.0, 155.0, label="Speed")
    a("coolant_temp_c", "float", 99.0, "Coolant temperature (PID 0105)", "°C", -40.0, 215.0, label="Coolant temperature")
    a("load_pct", "float", 29.8, "Calculated engine load (PID 0104)", "%", 0.0, 100.0, label="Engine load")
    a("throttle_pct", "float", 15.7, "Throttle position (PID 0111)", "%", 0.0, 100.0, label="Throttle")
    a("maf_gs", "float", 3.08, "Mass air flow (PID 0110)", "g/s", 0.0, 655.0, label="Mass air flow")
    a("map_kpa", "float", 40.0, "Manifold absolute pressure (PID 010B)", "kPa", 0.0, 255.0, label="Manifold pressure")
    a("iat_c", "float", 49.0, "Intake air temperature (PID 010F)", "°C", -40.0, 215.0, label="Intake air temperature")
    a("ambient_c", "float", 49.0, "Ambient sensor (PID 0146) — heat-soaked on a parked car", "°C", -40.0, 215.0, label="Outside temperature")
    a("fuel_pct", "float", 98.8, "Fuel level (PID 012F)", "%", 0.0, 100.0, label="Fuel level")
    a("module_v", "float", 13.84, "Control-module voltage (PID 0142)", "V", 0.0, 65.0, label="Module voltage")
    a("timing_deg", "float", 10.0, "Timing advance (PID 010E)", "°", -64.0, 63.5, label="Timing advance")
    a("baro_kpa", "float", 100.0, "Barometric pressure (PID 0133)", "kPa", 0.0, 255.0, label="Barometric pressure")
    a("runtime_s", "int", 1378, "Run time since engine start (PID 011F)", "s", 0, 65535, label="Run time")
    a("fuel_sys", "text", "closed loop", "Fuel system status (PID 0103)", "",
      choices=tuple(FUEL_SYS_CODES), label="Fuel system status")
    a("engine_running", "bool", True, "Engine running; false parks rpm at 0 and stops the run timer", label="Engine running")
    K.group("diagnostics")
    a("mil_on", "bool", False, "Check-engine lamp (PID 0101 bit 7)", label="Check-engine lamp")
    a("dtc_stored", "text", "", "Stored engine codes, space separated (mode 03). "
                                "Set 'real' for this car's actual 12-code list", label="Stored codes")
    a("dtc_pending", "text", "", "Pending engine codes (mode 07); 'real' for the captured pair", label="Pending codes")
    a("dtc_trans", "text", "", "Stored transmission codes (mode 03 at 7E1); 'real' for P0868", label="Transmission codes")
    K.group("rig")
    a("noise", "float", 0.2, "Sensor jitter amplitude (rpm counts and equivalent); 0 = clean", "", 0.0, 20.0, label="Sensor noise")
    a("clock_scale", "float", 1.0,
      "Simulated seconds per real second. An explicit --speed OVERRIDES this "
      "rather than multiplying it; the effective figure is state()['time_scale']",
      "", 0.01, 3600.0, label="Clock speed")

    K.group("faults")
    a("fault.sensor_dropout", "bool", False, "Coolant and IAT report the 'sensor open' value (raw 0x00 = -40 °C)", label="Sensor dropout")
    a("fault.car_asleep", "bool", False, "Ignition off: neither ECU answers", label="Ignition off")
    a("fault.adapter_silent", "bool", False, "The adapter itself answers nothing at all", label="Adapter silent")
    a("fault.bus_noise", "bool", False, "Truncated frames and DATA ERROR lines appear in captures", label="Bus noise")
    a("fault.ecu_nrc", "bool", False, "Every request gets a negative response instead of data", label="ECU negative response")
    a("ecu_nrc_code", "int", 0x22, "The NRC returned while fault.ecu_nrc is set", "", 0x10, 0xFF, label="NRC code")
    return K


KNOBS = build_knobs()
INTEGRATED = ("coolant_temp_c", "runtime_s", "fuel_pct")

_REAL = {"dtc_stored": REAL_ENGINE_DTCS, "dtc_pending": REAL_PENDING_DTCS,
         "dtc_trans": REAL_TRANS_DTCS}


def dtc_bytes(code):
    """'P0171' -> [0x01, 0x71]. Exact inverse of lancer_2009._dtc_codes()."""
    c = code.strip().upper()
    if len(c) != 5 or c[0] not in "PCBU":
        raise ValueError(f"{code!r} is not a DTC like P0171")
    try:
        a = ("PCBU".index(c[0]) << 6) | (int(c[1]) << 4) | int(c[2], 16)
        b = (int(c[3], 16) << 4) | int(c[4], 16)
    except ValueError:
        raise ValueError(f"{code!r} is not a DTC like P0171") from None
    return [a, b]


def parse_codes(text):
    return [c for c in (text or "").replace(",", " ").split() if c]


class LancerModel:
    vehicle = "lancer_2009"
    knobs = KNOBS

    def __init__(self, seed=1):
        self.seed = int(seed)
        self.k = KNOBS.defaults()
        self.t = 0.0
        self._noise = random.Random(self.seed ^ 0x1A17)

    def on_set(self, name, value):
        if name in _REAL and value.strip().lower() == "real":
            self.k[name] = _REAL[name]
            return {name: _REAL[name]}
        if name in _REAL:
            for c in parse_codes(value):
                dtc_bytes(c)              # validate loudly, at set time
        return {}

    def step(self, dt):
        """Advance by dt seconds of *simulated* time.

        Clock scaling belongs to `Simulator.step()`; sub-stepping keeps the
        coolant and fuel integrators honest at any time scale. See the
        integration note in simulator/model.py.
        """
        for h in substeps(dt):
            self._integrate(h)

    def _integrate(self, dt):
        if dt <= 0:
            return
        k = self.k
        if k["engine_running"]:
            k["runtime_s"] = min(65535, int(k["runtime_s"] + dt))
            k["coolant_temp_c"] += (99.0 - k["coolant_temp_c"]) * min(1.0, dt / 240.0)
            burn = (0.4 + 2.5 * (k["load_pct"] / 100.0)) * dt / 3600.0    # ~% per hour
            k["fuel_pct"] = max(0.0, k["fuel_pct"] - burn)
        else:
            k["coolant_temp_c"] += (k["ambient_c"] - k["coolant_temp_c"]) * min(1.0, dt / 3600.0)
        self.t += dt

    def _jit(self, amp):
        return 0.0 if amp <= 0 else (self._noise.random() - 0.5) * 2.0 * amp

    def state(self):
        k = self.k
        run = bool(k["engine_running"])
        drop = bool(k["fault.sensor_dropout"])
        codes = parse_codes(k["dtc_stored"])
        st = {
            "simulated": True,
            "vehicle": self.vehicle,
            "seed": self.seed,
            "t": round(self.t, 3),
            "rpm": round(k["rpm"] + self._jit(k["noise"]), 1) if run else 0.0,
            "speed_mph": round(k["speed_mph"], 1),
            "speed_kmh": int(round(k["speed_mph"] / 0.621371)),
            "coolant_temp_c": -40 if drop else int(round(k["coolant_temp_c"])),
            "load_pct": round(k["load_pct"], 1),
            "throttle_pct": round(k["throttle_pct"], 1),
            "maf_gs": round(k["maf_gs"], 2),
            "map_kpa": int(round(k["map_kpa"])),
            "iat_c": -40 if drop else int(round(k["iat_c"])),
            "ambient_c": int(round(k["ambient_c"])),
            "fuel_pct": round(k["fuel_pct"], 1),
            "module_v": round(k["module_v"], 3),
            "timing_deg": round(k["timing_deg"], 1),
            "baro_kpa": int(round(k["baro_kpa"])),
            "runtime_s": int(k["runtime_s"]) if run else 0,
            "fuel_sys": k["fuel_sys"],
            "engine_running": run,
            "mil_on": bool(k["mil_on"]),
            "dtc_stored": codes,
            "dtc_pending": parse_codes(k["dtc_pending"]),
            "dtc_trans": parse_codes(k["dtc_trans"]),
            "dtc_count": len(codes),
        }
        return st


# ── mode-01 encoding ─────────────────────────────────────────────────────
#
# One entry per PID: the data bytes that follow "41 <pid>". Inverse of
# vehicles/lancer_2009._DECODERS.

def _pid_data(pid, st):
    if pid == 0x03:
        return [FUEL_SYS_CODES.get(st["fuel_sys"], 2), 0x00]
    if pid == 0x04:
        return [max(0, min(255, int(round(st["load_pct"] * 255.0 / 100.0))))]
    if pid == 0x05:
        return [max(0, min(255, int(round(st["coolant_temp_c"] + 40))))]
    if pid == 0x0B:
        return [max(0, min(255, st["map_kpa"]))]
    if pid == 0x0C:
        return u16(max(0, min(16383.75, st["rpm"])) * 4)
    if pid == 0x0D:
        return [max(0, min(255, st["speed_kmh"]))]
    if pid == 0x0E:
        return [max(0, min(255, int(round((st["timing_deg"] + 64.0) * 2))))]
    if pid == 0x0F:
        return [max(0, min(255, int(round(st["iat_c"] + 40))))]
    if pid == 0x10:
        return u16(max(0.0, min(655.35, st["maf_gs"])) * 100)
    if pid == 0x11:
        return [max(0, min(255, int(round(st["throttle_pct"] * 255.0 / 100.0))))]
    if pid == 0x1F:
        return u16(st["runtime_s"])
    if pid == 0x2F:
        return [max(0, min(255, int(round(st["fuel_pct"] * 255.0 / 100.0))))]
    if pid == 0x33:
        return [max(0, min(255, st["baro_kpa"]))]
    if pid == 0x42:
        return u16(max(0.0, min(65.535, st["module_v"])) * 1000)
    if pid == 0x46:
        return [max(0, min(255, int(round(st["ambient_c"] + 40))))]
    if pid == 0x01:
        # MIL + stored-code count, then the readiness bytes this car sends
        return [(0x80 if st["mil_on"] else 0x00) | (st["dtc_count"] & 0x7F), 0x07, 0xE5, 0x00]
    return None


ENGINE_PIDS = (0x01, 0x03, 0x04, 0x05, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10,
               0x11, 0x1F, 0x2F, 0x33, 0x42, 0x46)
TRANS_PIDS = (0x01, 0x05, 0x0C, 0x0D, 0x1F, 0x42)


def _supported_mask(pids, base):
    """PID 00/20/40 support bitmask: bit 31 of the word is PID base+1."""
    v = 0
    for p in pids:
        if base < p <= base + 0x20:
            v |= 1 << (32 - (p - base))
    if any(base + 0x20 < p for p in pids):
        v |= 1                                    # "next block supported"
    return [(v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]


def mode01_response(cmd, st, rx, pids):
    pid = int(cmd[2:], 16)
    if pid in (0x00, 0x20, 0x40):
        data = _supported_mask(pids, pid)
    elif pid in pids:
        data = _pid_data(pid, st)
    else:
        return None
    if data is None:
        return None
    return isotp(rx, [0x41, pid], data, ZERO_PAD)


def dtc_response(service, codes, rx):
    """Mode 03 / 07. The count byte rides in the 2-byte response echo, which is
    exactly what parse_isotp() strips, so the payload is the code pairs."""
    payload = []
    for c in codes:
        payload += dtc_bytes(c)
    return isotp(rx, [0x40 | service, len(codes)], payload, ZERO_PAD)


def respond(cmd, tx, rx, st, nrc=None):
    """One ELM327 answer for the Lancer. None = we do not model this request."""
    c = (cmd or "").strip().upper().replace(" ", "")
    tx = (tx or "7E0").upper()
    rx = (rx or ("7E9" if tx == "7E1" else "7E8")).upper()
    pids = TRANS_PIDS if tx == "7E1" else ENGINE_PIDS
    if c in ("03", "07"):
        if nrc is not None:
            return negative(rx, int(c, 16), nrc)
        svc = int(c, 16)
        if tx == "7E1":
            codes = st["dtc_trans"] if svc == 3 else []
        else:
            codes = st["dtc_stored"] if svc == 3 else st["dtc_pending"]
        return dtc_response(svc, codes, rx)
    if len(c) == 4 and c.startswith("01"):
        if nrc is not None:
            return negative(rx, 0x01, nrc)
        return mode01_response(c, st, rx, pids)
    return None
