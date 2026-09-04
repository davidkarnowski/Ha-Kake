# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vehicle profile — 2009 Mitsubishi Lancer ES.

The first non-Leaf profile, and deliberately the simplest possible one:
the Lancer speaks standard SAE J1979 mode 01, so every item is a plain
PID request to the engine ECU (0x7E0 -> 0x7E8) and nothing needed reverse
engineering. Verified live 2026-08-28 (idle logging session); the raw
capture is tests/fixtures/lancer_idle_raw_20260828.json.

No built-in dashboard tiles (those are Leaf SVGs); the default layout is
built from user signal tiles, which work for any profile.
"""

NAME = "lancer_2009"
TITLE = "2009 Mitsubishi Lancer ES"

TARGETS = {"pid": ("7E0", "7E8"), "pid_t": ("7E1", "7E9")}
KIND_ORDER = ("pid", "pid_t")

ITEMS = {
    "pid_rpm":      {"kind": "pid", "cmd": "010C", "period": 0,   "timeout": 4.0, "label": "engine RPM"},
    "pid_speed":    {"kind": "pid", "cmd": "010D", "period": 0,   "timeout": 4.0, "label": "vehicle speed"},
    "pid_coolant":  {"kind": "pid", "cmd": "0105", "period": 5,   "timeout": 4.0, "label": "coolant temp"},
    "pid_voltage":  {"kind": "pid", "cmd": "0142", "period": 5,   "timeout": 4.0, "label": "module voltage"},
    "pid_load":     {"kind": "pid", "cmd": "0104", "period": 2,   "timeout": 4.0, "label": "engine load"},
    "pid_throttle": {"kind": "pid", "cmd": "0111", "period": 2,   "timeout": 4.0, "label": "throttle"},
    "pid_maf":      {"kind": "pid", "cmd": "0110", "period": 2,   "timeout": 4.0, "label": "MAF"},
    "pid_map":      {"kind": "pid", "cmd": "010B", "period": 2,   "timeout": 4.0, "label": "manifold pressure"},
    "pid_timing":   {"kind": "pid", "cmd": "010E", "period": 5,   "timeout": 4.0, "label": "timing advance"},
    "pid_iat":      {"kind": "pid", "cmd": "010F", "period": 15,  "timeout": 4.0, "label": "intake air temp"},
    "pid_ambient":  {"kind": "pid", "cmd": "0146", "period": 30,  "timeout": 4.0, "label": "ambient temp"},
    "pid_fuel":     {"kind": "pid", "cmd": "012F", "period": 60,  "timeout": 4.0, "label": "fuel level"},
    "pid_fuelsys":  {"kind": "pid", "cmd": "0103", "period": 10,  "timeout": 4.0, "label": "fuel system status"},
    "pid_runtime":  {"kind": "pid", "cmd": "011F", "period": 30,  "timeout": 4.0, "label": "run time"},
    "pid_baro":     {"kind": "pid", "cmd": "0133", "period": 300, "timeout": 4.0, "label": "barometric pressure"},
    # DTC readout (read-only: modes 01/03/07 — never mode 04 / clearing)
    "pid_mil":      {"kind": "pid",   "cmd": "0101", "period": 60,  "timeout": 4.0, "label": "MIL / DTC count"},
    "pid_dtc":      {"kind": "pid",   "cmd": "03",   "period": 300, "timeout": 8.0, "est": 0.6, "label": "stored codes (engine)"},
    "pid_dtc_pend": {"kind": "pid",   "cmd": "07",   "period": 300, "timeout": 6.0, "label": "pending codes (engine)"},
    "pidt_dtc":     {"kind": "pid_t", "cmd": "03",   "period": 300, "timeout": 6.0, "label": "stored codes (trans)"},
}

FAST_ONLY = {"pid_rpm"}

TILES = []            # built-in tiles are Leaf SVGs; the Lancer uses signal tiles
DEFAULT_SPAN = {}
DEFAULT_TILES = [
    {"id": "u_coolant",  "kind": "signal", "signal": "coolant_temp_f", "type": "thermo",  "enabled": True, "span": 3},
    {"id": "u_voltage",  "kind": "signal", "signal": "module_v",       "type": "number",  "enabled": True, "span": 3},
    {"id": "u_rpm",      "kind": "signal", "signal": "rpm",            "type": "dial",    "enabled": True, "span": 3},
    {"id": "u_speed",    "kind": "signal", "signal": "speed_mph",      "type": "number",  "enabled": True, "span": 3},
    {"id": "u_load",     "kind": "signal", "signal": "engine_load_pct", "type": "bar",    "enabled": True, "span": 3},
    {"id": "u_throttle", "kind": "signal", "signal": "throttle_pct",   "type": "bar",     "enabled": True, "span": 3},
    {"id": "u_iat",      "kind": "signal", "signal": "iat_f",          "type": "number",  "enabled": True, "span": 3},
    {"id": "u_fuel",     "kind": "signal", "signal": "fuel_pct",       "type": "battery", "enabled": True, "span": 3},
    {"id": "u_mil",      "kind": "signal", "signal": "mil_on",         "type": "lamp",    "enabled": True, "span": 2},
    {"id": "u_dtc",      "kind": "signal", "signal": "dtc_stored",     "type": "text",    "enabled": True, "span": 6,
     "title": "Engine codes"},
]

ITEM_KEYS = {
    "pid_mil": ("mil_on", "dtc_count"),
    "pid_dtc": ("dtc_stored",),
    "pid_dtc_pend": ("dtc_pending",),
    "pidt_dtc": ("dtc_trans",),
    "pid_coolant": ("coolant_temp_c", "coolant_temp_f"),
    "pid_maf": ("maf_gs",),
    "pid_fuel": ("fuel_pct",),
}

WATCH = ("fuel_sys", "mil_on", "dtc_count")

# Record keys that get real, indexed columns in the store instead of riding in
# the `extra` JSON bag — without an entry here a value cannot be charted or
# aggregated. See the HISTORY_COLS contract in vehicles/__init__.py.
HISTORY_COLS = {
    "rpm":             {"kind": "real", "hist": "rpm", "round": 0, "daily_filter": True,
                        "daily": {"avg": "rpm", "max": "rpm_max"}},
    "speed_mph":       {"kind": "real", "hist": "speed_mph", "round": 1, "daily": {"max": "speed_max"}},
    "speed_kmh":       {"kind": "real"},
    "coolant_temp_c":  {"kind": "real", "hist": "coolant_temp", "round": 1,
                        "hist_f": "coolant_temp_f", "daily": {"avg": "coolant_temp_c", "max": "coolant_max_c"}},
    "module_v":        {"kind": "real", "hist": "module_v", "round": 2,
                        "daily": {"avg": "module_v", "min": "module_v_min"}},
    "engine_load_pct": {"kind": "real", "hist": "engine_load_pct", "round": 1},
    "throttle_pct":    {"kind": "real", "hist": "throttle_pct", "round": 1},
    "maf_gs":          {"kind": "real", "hist": "maf_gs", "round": 2},
    "map_kpa":         {"kind": "real", "hist": "map_kpa", "round": 0},
    "timing_deg":      {"kind": "real", "hist": "timing_deg", "round": 1},
    "iat_c":           {"kind": "real", "hist": "iat", "round": 1, "hist_f": "iat_f"},
    "ambient_c":       {"kind": "real", "hist": "ambient", "round": 1, "hist_f": "ambient_f"},
    "fuel_pct":        {"kind": "real", "hist": "fuel_pct", "round": 1, "daily": {"min": "fuel_min"}},
    "baro_kpa":        {"kind": "real", "round": 0},
    "runtime_s":       {"kind": "int"},
    "fuel_sys":        {"kind": "text"},
    "mil_on":          {"kind": "bool", "hist": "mil_on", "index": "idx_readings_mil"},
    "dtc_count":       {"kind": "int", "hist": "dtc_count", "round": 0},
}

EXTRA_SKIP = ()

SIGNALS = {
    "coolant_temp_f": {"label": "Coolant temp",   "unit": "°F", "min": 60, "max": 260, "dec": 0, "item": "pid_coolant", "hist": "coolant_temp_f", "color": "heat", "alt": "coolant_temp_c", "alt_unit": "°C"},
    "module_v":       {"label": "12 V system",    "unit": "V",  "min": 10, "max": 15.5, "dec": 2, "item": "pid_voltage", "hist": "module_v", "color": "band"},
    "rpm":            {"label": "Engine RPM",     "unit": "rpm", "min": 0, "max": 7000, "dec": 0, "item": "pid_rpm", "hist": "rpm", "color": "mono"},
    "speed_mph":      {"label": "Speed",          "unit": "mph", "min": 0, "max": 120, "dec": 0, "item": "pid_speed", "hist": "speed_mph", "color": "mono"},
    "engine_load_pct": {"label": "Engine load",   "unit": "%",  "min": 0,  "max": 100, "dec": 0, "item": "pid_load", "hist": "engine_load_pct", "color": "good-low"},
    "throttle_pct":   {"label": "Throttle",       "unit": "%",  "min": 0,  "max": 100, "dec": 0, "item": "pid_throttle", "hist": "throttle_pct", "color": "mono"},
    "maf_gs":         {"label": "MAF airflow",    "unit": "g/s", "min": 0, "max": 150, "dec": 1, "item": "pid_maf", "hist": "maf_gs", "color": "mono"},
    "map_kpa":        {"label": "Manifold press.", "unit": "kPa", "min": 10, "max": 110, "dec": 0, "item": "pid_map", "hist": "map_kpa", "color": "mono"},
    "timing_deg":     {"label": "Timing advance", "unit": "°",  "min": -20, "max": 50, "dec": 1, "item": "pid_timing", "hist": "timing_deg", "color": "mono"},
    "iat_f":          {"label": "Intake air",     "unit": "°F", "min": 20, "max": 160, "dec": 0, "item": "pid_iat", "hist": "iat_f", "color": "heat", "alt": "iat_c", "alt_unit": "°C"},
    "ambient_f":      {"label": "Ambient",        "unit": "°F", "min": -10, "max": 120, "dec": 0, "item": "pid_ambient", "hist": "ambient_f", "color": "heat", "alt": "ambient_c", "alt_unit": "°C"},
    "fuel_pct":       {"label": "Fuel level",     "unit": "%",  "min": 0,  "max": 100, "dec": 0, "item": "pid_fuel", "hist": "fuel_pct", "color": "good-high"},
    "baro_kpa":       {"label": "Barometric",     "unit": "kPa", "min": 80, "max": 105, "dec": 0, "item": "pid_baro", "color": "mono"},
    "runtime_s":      {"label": "Run time",       "unit": "s",  "min": 0,  "max": 36000, "dec": 0, "item": "pid_runtime", "color": "mono"},
    "fuel_sys":       {"label": "Fuel system",    "kind": "text", "item": "pid_fuelsys"},
    "mil_on":         {"label": "Check engine (MIL)", "kind": "bool", "item": "pid_mil"},
    "dtc_count":      {"label": "Stored codes",   "unit": "",   "min": 0,  "max": 20, "dec": 0, "item": "pid_mil", "hist": "dtc_count", "color": "good-low"},
    "dtc_stored":     {"label": "Engine codes",   "kind": "text", "item": "pid_dtc"},
    "dtc_pending":    {"label": "Pending codes",  "kind": "text", "item": "pid_dtc_pend"},
    "dtc_trans":      {"label": "Trans codes",    "kind": "text", "item": "pidt_dtc"},
}


def _c2f(c):
    return round(c * 9.0 / 5.0 + 32.0, 1)


def _parse_mode01(lines, pid):
    """Data bytes of one '41 <pid>' single-frame response ('7E8 04 41 05 6E')."""
    best = None
    for ln in lines:
        toks = ln.split()
        if len(toks) < 4:
            continue
        if toks[0].upper() in ("7E8", "7E9", "7EA"):
            hdr, toks = toks[0].upper(), toks[1:]
        else:
            hdr = ""
        try:
            data = [int(t, 16) for t in toks]
        except ValueError:
            continue
        if data and data[0] <= 0x07:      # single-frame PCI byte
            data = data[1:]
        if len(data) >= 2 and data[0] == 0x41 and data[1] == pid:
            if hdr == "7E8" or best is None:
                best = data[2:]
        if hdr == "7E8" and best is not None:
            break
    return best


FUEL_SYS = {1: "open loop (warm-up)", 2: "closed loop", 4: "open loop (load/decel)",
            8: "open loop (fault)", 16: "closed loop (fault)"}

# pid byte -> list of (key, decode)
_DECODERS = {
    0x05: [("coolant_temp_c", lambda b: b[0] - 40), ("coolant_temp_f", lambda b: _c2f(b[0] - 40))],
    0x42: [("module_v", lambda b: round((b[0] * 256 + b[1]) / 1000.0, 3))],
    0x0C: [("rpm", lambda b: round((b[0] * 256 + b[1]) / 4.0, 1))],
    0x0D: [("speed_kmh", lambda b: b[0]), ("speed_mph", lambda b: round(b[0] * 0.621371, 1))],
    0x04: [("engine_load_pct", lambda b: round(b[0] * 100.0 / 255.0, 1))],
    0x11: [("throttle_pct", lambda b: round(b[0] * 100.0 / 255.0, 1))],
    0x10: [("maf_gs", lambda b: round((b[0] * 256 + b[1]) / 100.0, 2))],
    0x0B: [("map_kpa", lambda b: b[0])],
    0x0E: [("timing_deg", lambda b: round(b[0] / 2.0 - 64.0, 1))],
    0x0F: [("iat_c", lambda b: b[0] - 40), ("iat_f", lambda b: _c2f(b[0] - 40))],
    0x46: [("ambient_c", lambda b: b[0] - 40), ("ambient_f", lambda b: _c2f(b[0] - 40))],
    0x2F: [("fuel_pct", lambda b: round(b[0] * 100.0 / 255.0, 1))],
    0x03: [("fuel_sys", lambda b: FUEL_SYS.get(b[0], f"raw {b[0]:#04x}"))],
    0x1F: [("runtime_s", lambda b: b[0] * 256 + b[1])],
    0x33: [("baro_kpa", lambda b: b[0])],
}


async def configure(elm):
    """Base ELM init; headers/filters are set by the reader's target switch.
    Flow-control data/mode are set once here so multi-frame answers (a long
    mode-03 code list) reassemble; the switch re-points ATFCSH per target."""
    await elm.send("ATZ", wait=1.5)
    for cmd in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6",
                "ATFCSH 7E0", "ATFCSD 30 00 20", "ATFCSM1"):
        await elm.send(cmd, wait=0)


def _dtc_codes(lines, rx, svc):
    """Reassemble one ISO-TP mode 03/07 answer -> list of code strings, or None.

    parse_isotp() strips PCI + the two response-echo bytes (service, count),
    so its output is already the DTC byte pairs — but that means an empty
    result is ambiguous (no codes vs. no answer). Detect the positive
    response byte (0x43/0x47) in the raw frames first."""
    from leaf_decoders import parse_isotp   # generic ISO-TP reassembly helper
    resp, seen = svc + 0x40, False
    for ln in lines or []:
        parts = ln.strip().split()
        if len(parts) < 2 or parts[0].upper() != rx.upper():
            continue
        try:
            hx = [int(b, 16) for b in parts[1:]]
        except ValueError:
            continue
        top = hx[0] & 0xF0
        if (top == 0x00 and len(hx) > 1 and hx[1] == resp) or            (top == 0x10 and len(hx) > 2 and hx[2] == resp):
            seen = True
    if not seen:
        return None
    body = parse_isotp(lines, rx)
    out = []
    for i in range(0, len(body) - 1, 2):
        a, b = body[i], body[i + 1]
        if a == 0 and b == 0:
            continue
        out.append(f"{'PCBU'[(a >> 6) & 3]}{(a >> 4) & 3}{a & 0xF:X}{(b >> 4) & 0xF:X}{b & 0xF:X}")
    return out


def decode(responses):
    """{item_id: raw lines} -> (flat record, engine_alive)."""
    rec, alive = {}, None
    for iid, lines in responses.items():
        it = ITEMS.get(iid)
        if not it:
            continue
        rx = TARGETS[it["kind"]][1]
        if it["cmd"] in ("03", "07"):                     # DTC list
            codes = _dtc_codes(lines, rx, int(it["cmd"], 16))
            if codes is None:
                alive = alive or False
                continue
            alive = True
            key = {"pid_dtc": "dtc_stored", "pid_dtc_pend": "dtc_pending",
                   "pidt_dtc": "dtc_trans"}.get(iid, iid)
            rec[key] = " ".join(codes) if codes else "none"
            continue
        pid = int(it["cmd"][2:], 16)
        data = _parse_mode01(lines or [], pid)
        if data is None:
            alive = alive or False
            continue
        alive = True
        if it["cmd"] == "0101" and data:                  # MIL + stored-count
            rec["mil_on"] = bool(data[0] & 0x80)
            rec["dtc_count"] = data[0] & 0x7F
            continue
        for key, dec in _DECODERS.get(pid, ()):
            try:
                rec[key] = dec(data)
            except Exception:
                pass
    return rec, alive
