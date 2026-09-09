"""Stored values are the values the car reported (owner's rule, 2026-09-09).

A profile's apply_policy() may add keys — fused, calibrated, clamped, derived —
but it must never change a value decode() produced. The database keeps what the
car said; anything derived lives under its own key; smoothing for readability
is the dashboard's business. This runs the real fixture through every profile
that has a policy and checks each decoded key survives untouched.
"""
import copy
import importlib
import json
import pathlib

import pytest

import vehicles

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _profiles():
    return sorted(p.stem for p in (ROOT / "vehicles").glob("*.py") if p.stem != "__init__")


def _fixture_responses(mod):
    fx = ROOT / "tests" / "fixtures" / f"session_{mod.NAME}.json"
    if not fx.exists():
        return None
    doc = json.loads(fx.read_text())
    frames = doc.get("frames") or []
    if not frames:
        return None
    merged = {"uds": {}, "passive": {}}
    for fr in frames:                                    # cumulative, as ReplayELM merges them
        for hdr, cmds in (fr.get("uds") or {}).items():
            merged["uds"].setdefault(hdr, {}).update(cmds)
        merged["passive"].update(fr.get("passive") or {})
    responses = {}
    for item, it in mod.ITEMS.items():
        tgt = mod.TARGETS[it["kind"]]
        if tgt is None:
            lines = merged["passive"].get(it["id"].upper()) or merged["passive"].get(it["id"])
        else:
            lines = (merged["uds"].get(tgt[0]) or {}).get(it["cmd"])
        if lines:
            responses[item] = list(lines)
    return responses or None


@pytest.mark.parametrize("name", _profiles())
def test_policy_never_changes_a_reported_value(name):
    mod = importlib.import_module(f"vehicles.{name}")
    fn = getattr(mod, "apply_policy", None)
    if fn is None:
        pytest.skip(f"{name} has no apply_policy")
    responses = _fixture_responses(mod)
    if responses is None:
        pytest.skip(f"no session fixture for {name}")
    rec, _alive = mod.decode(responses)
    before = copy.deepcopy(rec)
    state = {}
    for _ in range(3):                                   # the policy learns across cycles
        fn(rec, {"current_offset_a": 0.37}, state)
    changed = {k: (before[k], rec[k]) for k in before if rec.get(k) != before[k]}
    assert not changed, f"{name}.apply_policy rewrote reported values: {changed}"


def test_leaf_policy_derives_under_its_own_keys():
    leaf = importlib.import_module("vehicles.leaf_ze0")
    c = {"current_a": -1.27, "hv_current2_a": -1.27, "g05_current_a": -1.18,
         "pack_v": 380.0, "discharging": True}
    leaf.apply_policy(c, {"current_offset_a": 0.5}, {})
    assert c["current_a"] == -1.27 and c["power_kw" if "power_kw" in c else "current_a"] is not None
    assert c["current_adj_a"] == pytest.approx(-1.18 - 0.5)
    assert c["power_adj_kw"] == pytest.approx(380.0 * c["current_adj_a"] / 1000.0, abs=1e-3)
    assert c["current_adj_src"] == "s2+g05_offset+zero_cal"


def test_power_tile_smoothing_is_a_display_only_option():
    """The tile's EMA is a setting in its ⋯ menu (off / 3 / 5 / 10), persisted in the
    tile's opts, labelled display-only; the tile shows the adjusted value and falls
    back to the raw one; the reader never smooths."""
    page = (ROOT / "web" / "templates" / "index.html").read_text()
    assert "window.powerMenu = function (box, o, commit)" in page
    assert "TileStudio.menuExtra('power', powerMenu)" in page
    assert "const EMA_STEPS = [['off', 1.0, 'off'], ['3', 0.6, '3-sample EMA'], ['5', 0.4, '5-sample EMA'], ['10', 0.2, '10-sample EMA']]" in page
    assert "data.power_adj_kw ?? data.power_kw" in page and "data.current_adj_a ?? data.current_a" in page
    assert 'id="power-smooth"' in page and "Cosmetic. The database always stores the raw reading" in page
    reader = (ROOT / "web" / "reader.py").read_text()
    assert "ema" not in reader.lower().replace("schema", "").replace("emanate", ""), "no smoothing in the reader"
