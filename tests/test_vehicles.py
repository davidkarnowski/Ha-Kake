# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vehicle-profile seam tests — no hardware.

* contract validation for every shipped profile
* the Lancer profile decoding a real idle capture (2026-08-28, BLE)
* reader.set_vehicle() rebinding the module surface and the signal registry
"""
import json
import os

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import vehicles  # noqa: E402
from vehicles import available, get_vehicle, history_cols, validate_profile  # noqa: E402

# every profile in vehicles/ is held to the contract — dropping in
# civic_2006.py is enough to get it tested.
PROFILES = available()
SHIPPED = ("leaf_ze0", "lancer_2009")


def test_available_profiles():
    assert set(SHIPPED) <= set(PROFILES)


@pytest.fixture(autouse=True)
def _restore_active():
    """get_vehicle() records the active profile for the store; don't leak it."""
    before = vehicles._active
    yield
    vehicles._active = before


def test_unknown_profile_raises():
    with pytest.raises(ValueError):
        get_vehicle("delorean_1985")


@pytest.mark.parametrize("name", PROFILES, ids=PROFILES)
def test_profile_validates(name):
    """validate_profile() is the contract; it must agree the shipped ones are ok."""
    assert validate_profile(get_vehicle(name)) == []


@pytest.mark.parametrize("name", PROFILES, ids=PROFILES)
def test_profile_contract(name):
    v = get_vehicle(name)
    assert v.NAME == name and v.TITLE
    for t in v.TILES:
        for i in t["items"]:
            assert i in v.ITEMS, (t["id"], i)
        assert t["id"] in v.DEFAULT_SPAN
    for k, s in v.SIGNALS.items():
        assert s["item"] in v.ITEMS, k
        assert s["kind"] in ("number", "bool", "text"), k
        if s["kind"] == "number":
            assert "unit" in s and "min" in s and "max" in s, k
    for i, it in v.ITEMS.items():
        assert it["kind"] in v.TARGETS, i
        assert it["period"] >= 0 and it["label"], i
        if v.TARGETS[it["kind"]] is None:
            assert "id" in it and "secs" in it, i
        else:
            assert "cmd" in it, i
    assert set(v.FAST_ONLY) <= set(v.ITEMS)
    assert isinstance(v.WATCH, tuple)
    builtin = {t["id"] for t in v.TILES}
    for t in v.DEFAULT_TILES:
        if t["id"] not in builtin:      # signal tile defaults must name real signals
            assert t.get("signal") in v.SIGNALS, t["id"]


@pytest.mark.parametrize("name", PROFILES, ids=PROFILES)
def test_temps_follow_the_f_with_c_convention(name):
    v = get_vehicle(name)
    for k, s in v.SIGNALS.items():
        if s.get("unit") == "°F":
            assert s.get("alt") and s.get("alt_unit") == "°C", k


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "lancer_idle_raw_20260828.json")


def _lancer_record():
    v = get_vehicle("lancer_2009")
    with open(FIXTURE) as f:
        raw = json.load(f)["responses"]     # keyed by command, e.g. "0105"
    by_cmd = {it["cmd"]: iid for iid, it in v.ITEMS.items()}
    responses = {by_cmd[c]: lines for c, lines in raw.items() if c in by_cmd}
    return v.decode(responses)


def test_lancer_decodes_real_idle_capture():
    rec, alive = _lancer_record()
    assert alive is True
    # warm idle in a hot driveway: the values must be physical, not exact
    assert 85 <= rec["coolant_temp_c"] <= 115
    assert 12.5 <= rec["module_v"] <= 15.0          # alternator charging
    assert 500 <= rec["rpm"] <= 1200                # idling
    assert rec["speed_kmh"] == 0 and rec["speed_mph"] == 0
    assert rec["fuel_pct"] > 90
    assert rec["coolant_temp_f"] == pytest.approx(rec["coolant_temp_c"] * 9 / 5 + 32, abs=0.11)
    assert rec["fuel_sys"] == "closed loop"
    assert rec["baro_kpa"] in range(95, 106)


def test_lancer_alive_flags():
    v = get_vehicle("lancer_2009")
    assert v.decode({"pid_rpm": ["NO DATA"]}) == ({}, False)
    assert v.decode({}) == ({}, None)


def test_reader_set_vehicle_rebinds(tmp_path, monkeypatch):
    import reader as rd
    import signals
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    try:
        rd.set_vehicle("lancer_2009")
        assert "pid_rpm" in rd.ITEMS and "lbc01" not in rd.ITEMS
        assert rd.TILES == []
        assert "coolant_temp_f" in signals.SIGNALS and "soc" not in signals.SIGNALS
        assert rd.enabled_items(rd.load_tiles(), fast_only=True) == {"pid_rpm"}
        # default layout is signal tiles; they resolve to real items
        items = rd.enabled_items(rd.load_tiles())
        assert "pid_coolant" in items and "pid_voltage" in items
    finally:
        rd.set_vehicle("leaf_ze0")
        assert "lbc01" in rd.ITEMS and "soc" in signals.SIGNALS


DTC_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "lancer_dtc_raw_20260828.json")


def test_lancer_dtc_readout_from_real_capture():
    """Real capture 2026-08-28: MIL on, 12 stored engine codes, CVT P0868."""
    v = get_vehicle("lancer_2009")
    with open(DTC_FIXTURE) as f:
        raw = json.load(f)["responses"]
    rec, alive = v.decode({
        "pid_mil": raw["engine:0101"],
        "pid_dtc": raw["engine:03"],
        "pid_dtc_pend": raw["engine:07"],
        "pidt_dtc": raw["trans:03"],
    })
    assert alive is True
    assert rec["mil_on"] is True and rec["dtc_count"] == 12
    stored = rec["dtc_stored"].split()
    assert len(stored) == 12
    assert {"P0171", "P0134", "P0131", "P2195", "P0132", "P0122",
            "P0223", "P1233", "P1234", "P1235", "P0145", "P1590"} == set(stored)
    assert rec["dtc_pending"] == "P0131 P2195"
    assert rec["dtc_trans"] == "P0868"


def test_lancer_dtc_no_codes_and_no_answer():
    v = get_vehicle("lancer_2009")
    rec, alive = v.decode({"pidt_dtc": ["7E9 02 47 00"]})   # pending fmt guard
    assert alive is False and "dtc_trans" not in rec        # 47 != 43: not this svc
    rec, alive = v.decode({"pid_dtc_pend": ["7E8 02 47 00"]})
    assert alive is True and rec["dtc_pending"] == "none"
    rec, alive = v.decode({"pid_dtc": ["NO DATA"]})
    assert alive is False


# ── history-column declaration (the store's schema comes from this) ──────

@pytest.mark.parametrize("name", PROFILES, ids=PROFILES)
def test_history_cols_declare_what_they_store(name):
    v = get_vehicle(name)
    cols = history_cols(v)
    assert cols, f"{name} declares no HISTORY_COLS — nothing it reads could be graphed"
    for col, s in cols.items():
        assert s["type"] in ("REAL", "INTEGER", "TEXT"), col
        assert s["kind"] in ("real", "int", "bool", "text"), col
        assert callable(s["key"]) or isinstance(s["key"], str), col
    # every signal that claims a history key must have a column producing it
    produced = {s.get("hist") for s in cols.values()} | {s.get("hist_f") for s in cols.values()}
    for k, sig in v.SIGNALS.items():
        if sig.get("hist"):
            assert sig["hist"] in produced, (name, k, sig["hist"])


def test_leaf_history_cols_match_the_shipped_schema():
    """The Leaf's columns are now a profile declaration, but they must still be
    exactly the set (and SQL types) existing databases were built with."""
    cols = history_cols(get_vehicle("leaf_ze0"))
    expect = {
        "soc": "REAL", "pack_v": "REAL", "current_a": "REAL", "power_kw": "REAL",
        "discharging": "INTEGER", "capacity_ah": "REAL", "soh": "REAL", "hx": "REAL",
        "lv_volts": "REAL", "insulation_kohm": "INTEGER", "temp1_c": "REAL", "temp2_c": "REAL",
        "temp3_c": "REAL", "temp4_c": "REAL", "temp_avg_c": "REAL", "cell_min": "INTEGER",
        "cell_max": "INTEGER", "cell_avg": "INTEGER", "cell_spread": "INTEGER",
        "cell_min_idx": "INTEGER", "cell_max_idx": "INTEGER", "balancing_active": "INTEGER",
        "hvac_ac_on": "INTEGER", "hvac_compressor_rpm": "INTEGER", "hvac_on": "INTEGER",
        "hvac_fan_on": "INTEGER", "hvac_fan_speed": "INTEGER", "hvac_heater_level": "INTEGER",
        "cabin_temp_c": "REAL", "hvac_ambient_c": "REAL", "hvac_evap_c": "REAL",
        "gear": "TEXT", "speed_mph": "REAL",
    }
    assert {c: s["type"] for c, s in cols.items()} == expect
    assert list(cols) == list(expect)          # column order, too


def test_lancer_declares_its_own_columns():
    cols = history_cols(get_vehicle("lancer_2009"))
    assert {"coolant_temp_c", "rpm", "engine_load_pct", "module_v", "throttle_pct"} <= set(cols)
    assert cols["coolant_temp_c"]["hist_f"] == "coolant_temp_f"   # °C stored, °F charted too


# ── validate_profile() itself ────────────────────────────────────────────

def _fake(**over):
    """A minimal valid profile module, so each test can break exactly one thing."""
    import types

    async def configure(elm):
        pass

    m = types.ModuleType("vehicles.fake_1999")
    m.NAME = "fake_1999"
    m.TITLE = "1999 Fake"
    m.TARGETS = {"pid": ("7E0", "7E8"), "mon": None}
    m.KIND_ORDER = ("pid", "mon")
    m.ITEMS = {"a": {"kind": "pid", "cmd": "0105", "period": 0, "label": "a"},
               "b": {"kind": "mon", "id": "421", "secs": 0.2, "period": 1, "label": "b"}}
    m.TILES = []
    m.DEFAULT_SPAN = {}
    m.DEFAULT_TILES = [{"id": "u_x", "kind": "signal", "signal": "x", "enabled": True}]
    m.ITEM_KEYS = {"a": ("x",)}
    m.WATCH = ("x",)
    m.FAST_ONLY = {"a"}
    m.SIGNALS = {"x": {"label": "X", "unit": "V", "min": 0, "max": 10, "item": "a"}}
    m.HISTORY_COLS = {"x": {"kind": "real", "hist": "x"}}
    m.configure = configure
    m.decode = lambda responses: ({}, None)
    for k, v in over.items():
        setattr(m, k, v)
    return m


def test_validate_accepts_a_minimal_profile():
    assert validate_profile(_fake()) == []


def _problem(**over):
    probs = validate_profile(_fake(**over))
    assert probs, f"expected a problem for {list(over)}"
    return " | ".join(probs)


def test_validate_reports_missing_attributes():
    m = _fake()
    del m.SIGNALS
    del m.WATCH
    probs = validate_profile(m)
    assert len(probs) == 1 and "SIGNALS" in probs[0] and "WATCH" in probs[0]


def test_validate_catches_the_things_the_tests_used_to_enforce():
    assert "filename" in _problem(NAME="wrong")
    assert "TITLE" in _problem(TITLE="")
    assert "not a key of TARGETS" in _problem(
        ITEMS={"a": {"kind": "nope", "cmd": "01", "period": 0, "label": "a"}})
    assert "numeric period" in _problem(
        ITEMS={"a": {"kind": "pid", "cmd": "0105", "period": -1, "label": "a"}})
    assert "needs a 'cmd'" in _problem(
        ITEMS={"a": {"kind": "pid", "period": 0, "label": "a"}})
    assert "'secs'" in _problem(
        ITEMS={"b": {"kind": "mon", "period": 0, "label": "b"}})
    assert "FAST_ONLY names unknown item" in _problem(FAST_ONLY={"zzz"})
    assert "WATCH must be a tuple" in _problem(WATCH=["x"])
    assert "unknown item" in _problem(
        SIGNALS={"x": {"label": "X", "unit": "V", "min": 0, "max": 10, "item": "zzz"}})
    assert "expected number, bool or text" in _problem(
        SIGNALS={"x": {"label": "X", "kind": "gauge", "item": "a"}})
    assert "is missing max" in _problem(
        SIGNALS={"x": {"label": "X", "unit": "V", "min": 0, "item": "a"}})
    assert "°C twin" in _problem(
        SIGNALS={"x": {"label": "X", "unit": "°F", "min": 0, "max": 10, "item": "a"}})


def test_validate_catches_tile_problems():
    assert "unknown item" in _problem(TILES=[{"id": "t", "name": "T", "items": ["zzz"]}],
                                      DEFAULT_SPAN={"t": 4})
    assert "no DEFAULT_SPAN" in _problem(TILES=[{"id": "t", "name": "T", "items": ["a"]}])
    assert "not in SIGNALS" in _problem(
        DEFAULT_TILES=[{"id": "u_y", "kind": "signal", "signal": "y"}])


def test_validate_catches_history_column_problems():
    assert "kind" in _problem(HISTORY_COLS={"x": {"kind": "decimal"}})
    assert "built-in column" in _problem(HISTORY_COLS={"x": {"kind": "real", "hist": "x"},
                                                      "extra": {"kind": "text"}})
    assert "history name" in _problem(HISTORY_COLS={"x": {"kind": "real", "hist": "x"},
                                                    "y": {"kind": "real", "hist": "x"}})
    assert "daily" in _problem(HISTORY_COLS={"x": {"kind": "real", "hist": "x",
                                                   "daily": {"median": "x"}}})
    assert "daily_filter" in _problem(HISTORY_COLS={"x": {"kind": "real", "hist": "x", "daily_filter": True},
                                                    "y": {"kind": "real", "daily_filter": True}})
    assert "hist_f" in _problem(HISTORY_COLS={"x": {"kind": "real", "hist": "x"},
                                              "y": {"kind": "real", "hist_f": "y_f"}})
    assert "no HISTORY_COLS entry produces" in _problem(
        SIGNALS={"x": {"label": "X", "unit": "V", "min": 0, "max": 10, "item": "a", "hist": "nope"}})


def test_validate_catches_bad_callables():
    assert "async" in _problem(configure=lambda elm: None)
    assert "decode" in _problem(decode="nope")


def test_get_vehicle_raises_with_the_problem_list(monkeypatch):
    import vehicles as vh
    bad = _fake(NAME="wrong")
    monkeypatch.setattr(vh.importlib, "import_module", lambda n: bad)
    monkeypatch.setitem(vh._cache, "fake_1999", None)
    vh._cache.pop("fake_1999")
    with pytest.raises(ValueError) as e:
        vh.get_vehicle("fake_1999")
    assert "filename" in str(e.value)


def test_active_vehicle_follows_get_vehicle():
    import vehicles as vh
    assert vh.active_vehicle("lancer_2009").NAME == "lancer_2009"
    assert vh.active_vehicle().NAME == "lancer_2009"        # sticky for the store
    assert vh.active_vehicle(get_vehicle("leaf_ze0")).NAME == "leaf_ze0"
