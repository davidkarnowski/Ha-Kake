# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tile-driven scheduler tests — no car, no adapter."""
import json
import os

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import reader as rd  # noqa: E402
from store import Store  # noqa: E402


@pytest.fixture
def r(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    monkeypatch.setattr(rd, "PAUSE_FILE", str(tmp_path / "reader.pause"))
    store = Store(str(tmp_path / "t.db"))
    reader = rd.Reader(interval=0, adapter_pref=None, store=store, budget=1.5)
    reader.refresh_items()
    yield reader
    store.close()


def test_every_tile_maps_to_known_items():
    for t in rd.TILES:
        for i in t["items"]:
            assert i in rd.ITEMS, (t["id"], i)


def test_default_enables_everything(r):
    assert r._items == set(rd.ITEMS)


def test_disabling_tiles_removes_only_their_items(r, tmp_path):
    cfg = {"tiles": [{"id": t["id"], "enabled": t["id"] not in ("tires", "climate", "cells")} for t in rd.TILES]}
    (tmp_path / "tiles.json").write_text(json.dumps(cfg))
    r.cache.update({"tpms_psi": [1, 2, 3, 4], "cells": [1], "cabin_temp_f": 70, "soc": 50})
    r.refresh_items()
    assert "p385" not in r._items and "hvac10" not in r._items and "lbc02" not in r._items
    assert "lbc01" in r._items and "p421" in r._items
    # stale values from disabled items are dropped, others kept
    assert "tpms_psi" not in r.cache and "cells" not in r.cache and "cabin_temp_f" not in r.cache
    assert r.cache["soc"] == 50


def test_shared_item_survives_when_one_tile_disabled(r, tmp_path):
    cfg = {"tiles": [{"id": t["id"], "enabled": t["id"] != "soc"} for t in rd.TILES]}
    (tmp_path / "tiles.json").write_text(json.dumps(cfg))
    r.refresh_items()
    assert "lbc01" in r._items          # still needed by health / power / history


def test_first_plan_runs_fast_lane_and_overdue_within_budget(r):
    plan = r.plan(now=1000.0)
    fast = {i for i in rd.ITEMS if rd.ITEMS[i]["period"] == 0}
    assert fast <= set(plan)
    slow = [i for i in plan if i not in fast]
    assert slow                                          # something overdue was picked
    assert sum(r.estimate(i) for i in slow) <= r.budget + max(r.estimate(i) for i in slow)
    # ordered to minimise ECU switches: all lbc, then hvac, then passive
    kinds = [rd.ITEMS[i]["kind"] for i in plan]
    assert kinds == sorted(kinds, key=lambda k: {"lbc": 0, "hvac": 1, "passive": 2}[k])


def test_slow_items_wait_for_their_period(r):
    now = 1000.0
    for i in rd.ITEMS:
        r.item_last[i] = now
    plan = r.plan(now + 0.5)   # < shortest slow period, so only the fast lane is due
    assert set(plan) == {i for i in rd.ITEMS if rd.ITEMS[i]["period"] == 0}
    # simulate consecutive ~1 s cycles from t+21: the 20 s items get their turn
    # within a few cycles (budget-limited), the 60 s item never does
    ran = set()
    t = now + 21.0
    for _ in range(8):
        plan = r.plan(t)
        for i in plan:
            r.item_last[i] = t
        ran.update(plan)
        t += 1.0
    assert "lbc02" in ran and "p385" in ran
    assert "p5B3" not in ran


def test_most_overdue_first(r):
    now = 1000.0
    for i in rd.ITEMS:
        r.item_last[i] = now
    r.item_last["p385"] = now - 200                      # 10× overdue
    r.item_last["lbc04"] = now - 16                      # just over 1×
    plan = r.plan(now + 1)
    assert "p385" in plan
    assert plan.index("p385") > plan.index("lbc01")      # passive after lbc regardless of urgency


def test_fast_only_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    assert rd.enabled_items(rd.load_tiles(), fast_only=True) == {"lbc01"}


def test_save_tiles_roundtrip_and_unknown_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    out = rd.save_tiles({"tiles": [{"id": "cells", "enabled": False}, {"id": "bogus", "enabled": True}, {"id": "soc"}]})
    ids = [t["id"] for t in out["tiles"]]
    assert ids[:2] == ["cells", "soc"] and "bogus" not in ids
    assert len(ids) == len(rd.TILES)
    first = rd.load_tiles()["tiles"][0]
    assert first["id"] == "cells" and first["enabled"] is False and first["kind"] == "builtin"


# ── tiles v2: user signal tiles ──

def test_user_signal_tile_adds_its_item(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    cfg = {"tiles": [{"id": t["id"], "enabled": False} for t in rd.TILES]
           + [{"id": "u1", "kind": "signal", "signal": "cabin_temp_f", "type": "arc", "span": 3, "opts": {"color": "heat"}}]}
    saved = rd.save_tiles(cfg)
    u = [t for t in saved["tiles"] if t["id"] == "u1"][0]
    assert u["kind"] == "signal" and u["type"] == "arc" and u["span"] == 3 and u["opts"] == {"color": "heat"}
    assert rd.enabled_items(rd.load_tiles()) == {"hvac10"}


def test_user_tile_with_unknown_signal_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    saved = rd.save_tiles({"tiles": [{"id": "u9", "kind": "signal", "signal": "nope"}]})
    assert all(t["id"] != "u9" for t in saved["tiles"])


def test_builtin_keeps_span_and_opts(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    saved = rd.save_tiles({"tiles": [{"id": "soc", "span": 6, "opts": {"color": "mono"}, "enabled": True}]})
    soc = saved["tiles"][0]
    assert soc["id"] == "soc" and soc["span"] == 6 and soc["opts"] == {"color": "mono"} and soc["kind"] == "builtin"
    assert rd.load_tiles()["tiles"][0]["span"] == 6


def test_span_is_clamped(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    saved = rd.save_tiles({"tiles": [{"id": "soc", "span": 40}, {"id": "u1", "kind": "signal", "signal": "soc", "span": "x"}]})
    assert saved["tiles"][0]["span"] == 12
    assert [t for t in saved["tiles"] if t["id"] == "u1"][0]["span"] == 3


def test_signal_registry_consistency():
    import signals
    for k, v in signals.SIGNALS.items():
        assert v["item"] in rd.ITEMS, k
        assert v["color"] in signals.COLOR_SCALES if v["kind"] == "number" else True, k
    assert signals.get_value({"tpms_psi": [1, 2, 3, 4]}, "tpms_psi.2") == 3
    assert signals.get_value({"tpms_psi": [1]}, "tpms_psi.2") is None
    assert signals.get_value({"soc": 5}, "soc") == 5


def test_next_due_and_empty_cycles(r, tmp_path):
    cfg = {"tiles": [{"id": t["id"], "enabled": False} for t in rd.TILES]
           + [{"id": "u1", "kind": "signal", "signal": "tpms_psi.0"}]}     # only p385, period 20
    (tmp_path / "tiles.json").write_text(json.dumps(cfg))
    r.refresh_items()
    assert r._items == {"p385"}
    assert r.next_due(1000.0) == 0.0            # never run → due now
    r.item_last["p385"] = 1000.0
    assert r.plan(1005.0) == []                  # nothing due → empty cycle
    assert 14.9 < r.next_due(1005.0) <= 15.0     # 20 s period, 5 s elapsed
    assert r.plan(1021.0) == ["p385"]


def test_current_policy_clamps_offset_noise_while_discharging(r):
    r.cache.update({"current_a": 0.21, "pack_v": 381.3, "discharging": True})
    r.apply_policy()
    assert r.cache["current_a"] == 0.0 and r.cache["power_kw"] == 0.0
    assert r.cache["current_raw_a"] == 0.21
    r.cache.update({"current_a": -1.5})
    r.apply_policy()
    assert r.cache["current_a"] == -1.5 and r.cache["power_kw"] < 0


def test_current_policy_without_flag_uses_sign(r):
    r.cache.clear()
    r.cache.update({"current_a": 2.0, "pack_v": 380.0})
    r.apply_policy()
    assert r.cache["discharging"] is False and r.cache["power_kw"] > 0


def test_current_offset_calibration(r, tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "CALIB_FILE", str(tmp_path / "calibration.json"))
    rd.save_calibration({"current_offset_a": 0.3})
    r.cache.update({"current_a": 0.33, "pack_v": 381.0})
    r.apply_policy()
    assert r.cache["current_offset_a"] == 0.3
    assert r.cache["current_a"] == pytest.approx(0.03)
    assert r.cache["current_raw_a"] == 0.33
    rd.save_calibration({})
    r.cache.update({"current_a": 0.33})
    r.apply_policy()
    assert r.cache["current_a"] == 0.33


def test_sensor_fusion_learns_group05_offset(r):
    # cycle with both: sensor2 reads 0.3, group 05 says -0.96 → learn offset -1.26
    r.cache.update({"hv_current2_a": 0.3, "g05_current_a": -0.96, "current_a": 0.3, "pack_v": 380.0, "discharging": True})
    r.apply_policy()
    assert r.cache["s2_offset_a"] == pytest.approx(-1.26)
    assert r.cache["current_a"] == pytest.approx(-0.96)
    assert r.cache["power_kw"] < 0
    # next cycle: only sensor 2 refreshed (group 05 stale) → offset carried
    r.cache.update({"hv_current2_a": -1.5, "current_a": -1.5})
    r.apply_policy()
    assert r.cache["current_a"] == pytest.approx(-2.76)
    # fresh group 05 agreeing with sensor 2 under load pulls the offset back toward 0
    r.cache.update({"hv_current2_a": -1.5, "g05_current_a": -1.55, "current_a": -1.5})
    r.apply_policy()
    assert -1.26 < r.cache["s2_offset_a"] < 0


def test_layout_fields_roundtrip_and_clamp(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    saved = rd.save_tiles({"tiles": [{"id": "soc", "span": 4, "x": 10, "y": 3, "h": 7},
                                     {"id": "u1", "kind": "signal", "signal": "soc", "span": 3, "x": "bad", "h": 1}]})
    soc = saved["tiles"][0]
    assert (soc["x"], soc["y"], soc["h"]) == (8, 3, 7)      # x clamped so x+span <= 12
    u1 = [t for t in saved["tiles"] if t["id"] == "u1"][0]
    assert "x" not in u1 and u1["h"] == 2
    assert rd.load_tiles()["tiles"][0]["h"] == 7


def test_named_layouts_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    monkeypatch.setattr(rd, "LAYOUTS_FILE", str(tmp_path / "layouts.json"))
    rd.save_tiles({"tiles": [{"id": "soc", "span": 6, "x": 0, "y": 0, "h": 5},
                             {"id": "u1", "kind": "signal", "signal": "soc", "type": "dial", "span": 3}]})
    saved = rd.save_layout("  driving  ")
    assert saved["tiles"][0]["span"] == 6 and len(rd.list_layouts()) == 1
    assert rd.list_layouts()[0]["name"] == "driving"
    # change the active layout, then load the saved one back
    rd.save_tiles({"tiles": []})
    assert rd.load_tiles()["tiles"][0]["span"] == rd.DEFAULT_SPAN["soc"]
    cfg = rd.load_layout("driving")
    assert cfg["tiles"][0]["span"] == 6
    assert any(t["id"] == "u1" and t["type"] == "dial" for t in rd.load_tiles()["tiles"])
    # explicit tiles body is stored (not the active file)
    rd.save_layout("empty", {"tiles": [{"id": "soc", "enabled": False}]})
    assert [t for t in rd._read_layouts()["empty"]["tiles"] if t["id"] == "soc"][0]["enabled"] is False
    assert rd.delete_layout("empty") is True and rd.delete_layout("empty") is False
    with pytest.raises(KeyError):
        rd.load_layout("nope")
    with pytest.raises(ValueError):
        rd.save_layout("   ")


def test_emit_events_records_transitions(r):
    r.store.insert_event  # ensure store has events API
    r.cache.update({"hvac_ac_on": False, "gear": "P", "locked": True})
    r.emit_events()                                   # first sight → baseline events
    r.cache.update({"hvac_ac_on": True})              # A/C turns on
    r.emit_events()
    r.cache.update({"hvac_ac_on": True})              # unchanged → no new event
    r.emit_events()
    r.cache.update({"gear": "D"})                     # gear change
    r.emit_events()
    ac = r.store.events("hvac_ac_on")
    assert [e["value"] for e in ac] == ["0", "1"]     # baseline off, then on — not duplicated
    assert ac[1]["prev"] == "0"
    assert [e["value"] for e in r.store.events("gear")] == ["P", "D"]
