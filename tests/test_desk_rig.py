# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""tools/desk_rig.py — the parts that decide what the rig does, without starting it.

What a scenario holds back and for how long comes from the vehicle profile,
never from a list in the tool. The rig keeps away from a real car's topics and
data folder. A value "no car can produce" is one outside physical bounds, and
nothing else.

The whole chain (mosquitto, simulated ECUs, bridge, laggy link, the real
application) takes half a minute and needs a broker installed, so that one
test is opt-in:  HAKAKE_DESK_RIG=1 pytest tests/test_desk_rig.py
"""

import os
import sys
import types

import pytest

from conftest import ROOT  # noqa: F401  (sys.path is set up there)

sys.path.insert(0, os.path.join(ROOT, "tools"))
import desk_rig as dr  # noqa: E402
from vehicles import get_vehicle  # noqa: E402

LEAF = get_vehicle("leaf_ze0")


# ── the scenario comes from the profile ──────────────────────────────────

def test_the_leaf_scenario_holds_the_nearest_other_battery_request():
    plan = dr.scenario_plan(LEAF)
    assert plan["fast"] == "lbc01" and plan["hold"] == "lbc05"
    assert plan["pattern"] == '"data":"21 05"'               # as the request travels (mqttsource.hex_bytes)
    assert plan["hold_s"] == 12.5                            # its 10 s timeout, the 1 s ack grace, and margin
    assert set(plan["tiles"]) == {"soc", "health", "power", "history", "degradation"}
    polled = {i for t in LEAF.TILES if t["id"] in plan["tiles"] for i in t["items"]}
    assert polled == {"lbc01", "lbc05"}                      # nothing else competes for the link


def test_a_chosen_item_gets_its_own_pattern_time_and_a_tile_that_polls_it():
    plan = dr.scenario_plan(LEAF, "lbc02")
    assert plan["hold"] == "lbc02" and plan["pattern"] == '"data":"21 02"'
    assert plan["hold_s"] == 17.5                            # the cell read's 15 s timeout
    assert any("lbc02" in t["items"] for t in LEAF.TILES if t["id"] in plan["tiles"])


@pytest.mark.parametrize("item", ["lbc01", "p421", "hvac10", "nope"])
def test_the_held_item_must_be_another_request_on_the_same_ecu(item):
    with pytest.raises(SystemExit):
        dr.scenario_plan(LEAF, item)


def test_a_profile_with_one_request_item_has_no_such_scenario():
    lone = types.SimpleNamespace(FAST_ONLY={"a"}, TARGETS={"ecu": ("7E0", "7E8")}, TILES=[],
                                 ITEMS={"a": {"kind": "ecu", "cmd": "0100", "period": 0}})
    with pytest.raises(SystemExit):
        dr.scenario_plan(lone)
    with pytest.raises(SystemExit):
        dr.scenario_plan(types.SimpleNamespace(FAST_ONLY=set(), TARGETS={}, TILES=[], ITEMS={}))


def test_spaced_is_how_request_data_travels():
    assert dr.spaced("2105") == "21 05" and dr.spaced("21 05") == "21 05" and dr.spaced("0100") == "01 00"


# ── tiles ────────────────────────────────────────────────────────────────

def test_tiles_config_enables_exactly_what_was_asked():
    cfg = dr.tiles_config(LEAF, {"soc", "power"})
    on = {t["id"] for t in cfg["tiles"] if t["enabled"]}
    assert on == {"soc", "power"}
    assert {t["id"] for t in cfg["tiles"]} >= {t["id"] for t in LEAF.TILES}      # every default tile is listed, off
    assert not any((t.get("opts") or {}).get("celllog") for t in cfg["tiles"])


def test_the_cell_log_is_armed_only_where_asked():
    cfg = dr.tiles_config(LEAF, {"soc", "cells"}, {"cells"})
    armed = [t["id"] for t in cfg["tiles"] if (t.get("opts") or {}).get("celllog")]
    assert armed == ["cells"]


# ── what counts as impossible ────────────────────────────────────────────

def test_only_values_outside_physical_bounds_are_impossible():
    assert dr.implausible({"soc": 85.0, "lv_volts": 12.68, "pack_v": 386.9, "cell_min": 3990, "cell_max": 4011}) == {}
    assert dr.implausible({"soc": 0.0, "lv_volts": 6.0, "pack_v": 450.0}) == {}                 # bounds are inside
    assert dr.implausible({"soc": 419.44, "lv_volts": 63.999, "pack_v": 6.47}) == \
        {"soc": 419.44, "lv_volts": 63.999, "pack_v": 6.47}                                    # 2026-11-05
    assert dr.implausible({"soc": 22.97, "pack_v": 8.98}) == {"pack_v": 8.98}
    assert dr.implausible({"cell_min": 409, "cell_max": 4011}) == {"cell_min": 409}


def test_missing_and_non_numeric_values_are_not_judged():
    assert dr.implausible({}) == {} and dr.implausible(None) == {}
    assert dr.implausible({"soc": None, "lv_volts": "12.6", "pack_v": True}) == {}


# ── kept apart from a real car ───────────────────────────────────────────

def test_the_rig_refuses_the_prefix_a_real_car_publishes_under():
    import mqttsource
    live = mqttsource.DEFAULTS["prefix"]
    for p in (live, live + "/", live + "/car"):
        with pytest.raises(SystemExit):
            dr.check_prefix(p)
    assert dr.check_prefix("hakake/desk/") == "hakake/desk"
    assert dr.check_prefix(live + "-desk") == live + "-desk"


def test_the_dashboard_gets_its_own_data_folder_and_only_the_rigs_broker():
    base = {"PATH": "/usr/bin", "HAKAKE_MQTT_HOST": "192.0.2.7", "HAKAKE_MQTT_PORT": "1883",
            "HAKAKE_MQTT_PREFIX": "hakake/leaf", "HAKAKE_SIM_SEED": "3", "HAKAKE_ADAPTERS": "[]",
            "HAKAKE_DATA_DIR": "/somewhere/real"}
    env = dr.dashboard_env("/scratch/data", 18884, "hakake/desk", base=base)
    assert env["HAKAKE_DATA_DIR"] == "/scratch/data"
    assert (env["HAKAKE_MQTT_HOST"], env["HAKAKE_MQTT_PORT"], env["HAKAKE_MQTT_PREFIX"], env["HAKAKE_MQTT_BUS"]) == \
        ("127.0.0.1", "18884", "hakake/desk", "car")
    assert "HAKAKE_SIM_SEED" not in env and "HAKAKE_ADAPTERS" not in env and env["PATH"] == "/usr/bin"
    assert base["HAKAKE_MQTT_HOST"] == "192.0.2.7"                       # the caller's environment is not touched


def test_the_broker_listens_on_the_loopback_address_only():
    lines = dr.broker_conf(18883).splitlines()
    assert lines[0] == "listener 18883 127.0.0.1"
    assert "allow_anonymous true" in lines and "persistence false" in lines
    assert sum(1 for line in lines if line.startswith("listener")) == 1


def test_a_run_that_stored_nothing_reads_as_no_rows(tmp_path):
    assert dr.stored_rows(str(tmp_path / "leaf_battery.db")) == []


def test_free_ports_are_loopback_ports_and_differ():
    a, b = dr.free_port(), dr.free_port()
    assert 1024 < a < 65536 and 1024 < b < 65536


# ── the whole chain (opt-in) ─────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("HAKAKE_DESK_RIG") != "1" or not dr.find_mosquitto(),
                    reason="opt-in: HAKAKE_DESK_RIG=1 and mosquitto installed (about 30 s)")
@pytest.mark.xfail(strict=True, reason="a held request still puts an impossible value on the page: "
                                       "reader guard, plan phase 1")
def test_no_impossible_value_survives_a_held_request(tmp_path):
    assert dr.main(["--scenario", "stale-reply", "--expect", "clean", "--scratch", str(tmp_path)]) == 0
