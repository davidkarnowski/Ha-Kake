# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Car-CAN passive + HVAC decoders against tests/fixtures/probe_20260824_185139.json
(car in Park, READY, AC on, evening)."""
import json
import os

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)
import leaf_decoders as ld  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "probe_20260824_185139.json")


@pytest.fixture(scope="module")
def probe():
    with open(FIX) as f:
        return json.load(f)


def test_carcan(probe):
    v = ld.decode_carcan(probe["passive"])
    assert v["gear"] == "P" and v["gear_code"] == 0x08
    assert v["units_miles"] is True
    assert v["odometer_mi"] == 65545
    assert v["tpms_psi"] == [37.25, 35.75, 36.5, 36.5]
    assert v["soh_dash_pct"] == 35
    assert v["speed_mph"] == 0.0
    assert v["start_state_name"] == "ready"
    assert v["handbrake"] is True
    assert v["door_front"] is False and v["door_trunk"] is False
    assert v["gear_174"] == "P/N"


def test_gear_421_live_values():
    # transitions captured 2026-08-24 19:03 while shifting P→R→D→P
    assert ld.decode_carcan({"421": ["421 10 00 00"]})["gear"] == "R"
    assert ld.decode_carcan({"421": ["421 20 00 00"]})["gear"] == "D"
    assert ld.decode_carcan({"421": ["421 08 00 00"]})["gear"] == "P"
    assert ld.decode_carcan({"421": ["421 18 00 00"]})["gear"] == "N"     # 19:12:50
    assert ld.decode_carcan({"421": ["421 38 00 00"]})["gear"] == "Eco"   # 19:13:09
    assert ld.decode_carcan({"174": ["174 00 00 00 99 0A 00 00 00"]})["gear_174"] == "R"
    assert ld.decode_carcan({"174": ["174 00 00 00 BB 0B 00 00 00"]})["gear_174"] == "D/Eco"


def test_gear_zero_byte_is_not_a_gear():
    assert ld.decode_carcan({"421": ["421 08 00 00", "421 00 00 00"]})["gear"] == "P"
    assert "gear" not in ld.decode_carcan({"421": ["421 00 00 00"]})


def test_turn_signal_358():
    assert ld.decode_carcan({"358": ["358 00 0A 82 00 00 00 00 00"]})["turn_signal"] == "left"
    assert ld.decode_carcan({"358": ["358 00 0A 84 00 00 00 00 00"]})["turn_signal"] == "right"
    assert ld.decode_carcan({"358": ["358 00 0A 80 00 00 00 00 00"]})["turn_signal"] == "off"


def test_carcan_truncated_lines_ignored():
    caps = {"385": ["385 00 00 95", "385 00 00 95 8F 92 92 F0", "385 00 0"]}
    assert ld.decode_carcan(caps)["tpms_psi"] == [37.25, 35.75, 36.5, 36.5]


def test_carcan_empty():
    assert ld.decode_carcan({}) == {}
    assert ld.decode_carcan({"421": []}) == {}


def test_hvac(probe):
    h = ld.decode_hvac(probe["hvac"])
    assert h["cabin_temp_c"] == 21 and h["cabin_temp_f"] == 69.8
    assert h["hvac_ambient_c"] == 33 and h["hvac_ambient_f"] == 91.4
    assert h["hvac_evap_c"] == 1
    assert h["hvac_sunload"] == 2
    assert h["hvac_decode"] == "tentative"
    assert h["hvac_g10_raw"].startswith("493d2902")
    assert "hvac_g11_raw" in h and "hvac_g01_raw" in h
    # negative responses (1-byte payload 0x12) must not produce keys
    assert "hvac_g02_raw" not in h


def test_hvac_no_data():
    assert ld.decode_hvac({"2110": ["NO DATA"]}) == {}


def test_fan_speed_from_walk_fixture():
    """Fan walk 2026-08-24: group 10 byte 11 = 0x80 | blower volts (84 85 86 88 89 8B 8B for 1..7)."""
    import glob
    fx = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", "walk_fan_*.json")))[-1]
    with open(fx) as f:
        w = json.load(f)
    seen = {}
    for label, cap in zip(w["steps"], w["captures"]):
        d = bytes(cap["2110"][-1])
        volts = d[11] & 0x7F
        seen.setdefault(label, set()).add(volts)
    assert seen["fan 1"] == {4} and seen["fan 2"] == {5} and seen["fan 3"] == {6}
    assert seen["fan 4"] == {8} and seen["fan 5"] == {9}
    assert ld.fan_level_from_volts(4) == 1 and ld.fan_level_from_volts(9) == 5 and ld.fan_level_from_volts(12) == 7
    assert ld.fan_level_from_volts(0) == 0
    # decode path end to end on the fan-5 sample
    cap = w["captures"][4]["2110"][-1]
    line = "764 10 " + f"{len(cap)+2:02X} 61 10 " + " ".join(f"{b:02X}" for b in cap[:3])
    rest = cap[3:]
    lines = [line] + [f"764 2{(i+1)%16:X} " + " ".join(f"{b:02X}" for b in rest[i*7:(i+1)*7]) for i in range((len(rest)+6)//7)]
    out = ld.decode_hvac({"2110": lines})
    assert out["hvac_fan_on"] is True and out["hvac_blower_v"] == 9 and out["hvac_fan_speed"] == 5


def _lines_from_payload(payload, group="10"):
    """Build ELM327-style ISO-TP lines for a 0x764 response carrying `payload` for 21 <group>."""
    first = f"764 10 {len(payload)+2:02X} 61 {group} " + " ".join(f"{b:02X}" for b in payload[:3])
    rest = payload[3:]
    return [first] + [f"764 2{(i+1)%16:X} " + " ".join(f"{b:02X}" for b in rest[i*7:(i+1)*7]) for i in range((len(rest)+6)//7)]


def _walk_majority(name, step_index, group="2110"):
    import glob
    fx = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", f"walk_{name}_*.json")))[-1]
    with open(fx) as f:
        w = json.load(f)
    samples = w["captures"][step_index][group]
    return [max(set(s[i] for s in samples), key=[s[i] for s in samples].count) for i in range(len(samples[0]))]


def test_hvac_on_off_walk():
    on = ld.decode_hvac({"2110": _lines_from_payload(_walk_majority("hvac", 0))})
    off = ld.decode_hvac({"2110": _lines_from_payload(_walk_majority("hvac", 1))})
    assert on["hvac_on"] is True and on["hvac_fan_speed"] == 3 and on["hvac_blower_v"] == 6
    assert off["hvac_on"] is False and off["hvac_fan_speed"] == 0 and off["hvac_blower_v"] == 0


def test_ac_walk_flag_and_compressor():
    off = ld.decode_hvac({"2110": _lines_from_payload(_walk_majority("ac", 0))})
    on = ld.decode_hvac({"2110": _lines_from_payload(_walk_majority("ac", 1))})
    assert off["hvac_ac_on"] is False and off["hvac_compressor_rpm"] == 0
    assert on["hvac_ac_on"] is True and 1500 < on["hvac_compressor_rpm"] < 2600


def test_setpoint_walk_target_tracks_setpoint():
    import glob
    fx = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", "walk_setpoint_*.json")))[-1]
    with open(fx) as f:
        w = json.load(f)
    for idx, label in enumerate(w["steps"]):
        want = float(label.split()[0])
        out = ld.decode_hvac({"2110": _lines_from_payload(_walk_majority("setpoint", idx))})
        assert abs(out["hvac_target_f"] - want) <= 2.0, (label, out["hvac_target_f"])
    lo = ld.decode_hvac({"2110": _lines_from_payload(_walk_majority("setpoint", 0))})
    hi = ld.decode_hvac({"2110": _lines_from_payload(_walk_majority("setpoint", 6))})
    assert lo["hvac_heater_level"] == 0 and hi["hvac_heater_level"] > 20


def test_mode_and_auto_walks_are_flat():
    """Documented negative: the amp exposes neither the mode doors nor AUTO over service 21."""
    for name in ("mode", "auto"):
        vals = set()
        import glob
        fx = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", f"walk_{name}_*.json")))[-1]
        with open(fx) as f:
            w = json.load(f)
        for cap in w["captures"]:
            vals.add(bytes(cap["2100"][-1]))
        assert vals == {bytes([0x80, 0x01, 0x80, 0x00])}, name


def _walk_carcan_majority(name, step_index, cid):
    import glob
    fx = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", f"walk_{name}_*.json")))[-1]
    with open(fx) as f:
        w = json.load(f)
    frames = [s for s in w["captures"][step_index][cid] if len(s) >= 3]
    n = min(len(s) for s in frames)
    return [max(set(s[i] for s in frames), key=[s[i] for s in frames].count) for i in range(n)]


def test_door_walk_per_corner_bits():
    steps = ["all shut", "driver open", "driver shut", "pass open", "pass shut",
             "rl open", "rl shut", "rr open", "rr shut", "hatch open", "hatch shut", "all shut"]
    want = {  # step index → the door that should read open
        1: "door_driver", 3: "door_pass", 5: "door_rl", 7: "door_rr", 9: "door_hatch"}
    for idx, label in enumerate(steps):
        maj = _walk_carcan_majority("doors", idx, "60D")
        line = "60D " + " ".join(f"{x:02X}" for x in maj)
        out = ld.decode_carcan({"60D": [line]})
        opendoors = {k for k in ("door_driver", "door_pass", "door_rl", "door_rr", "door_hatch") if out[k]}
        if idx in want:
            assert opendoors == {want[idx]}, (label, opendoors)
        else:
            assert opendoors == set(), (label, opendoors)


def test_lock_walk():
    for idx, locked in enumerate([False, True, False, True, False]):
        maj = _walk_carcan_majority("locks", idx, "60D")
        out = ld.decode_carcan({"60D": ["60D " + " ".join(f"{x:02X}" for x in maj)]})
        assert out["locked"] is locked


def test_lights_walk():
    # 0x60D from the lights walk: b0 0x04 park / 0x02 low beam, b1 0x08 high / 0x01 fog
    def L(b0, b1):
        return ld.decode_carcan({"60D": [f"60D {b0:02X} {b1:02X} 00 00 00 00 00 00"]})
    off = L(0x00, 0x06)
    assert not (off["parking_lights"] or off["headlights"] or off["high_beam"] or off["fog_lights"])
    park = L(0x04, 0x06)
    assert park["parking_lights"] and not park["headlights"]
    head = L(0x06, 0x06)
    assert head["parking_lights"] and head["headlights"] and not head["high_beam"]
    high = L(0x06, 0x0E)
    assert high["headlights"] and high["high_beam"] and not high["fog_lights"]
    fog = L(0x06, 0x07)
    assert fog["fog_lights"] and fog["headlights"] and not fog["high_beam"]
    for s in (off, park, head, high, fog):        # start-state survives the extra light bits
        assert s["start_state_name"] == "ready"
