# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The 3D pack tile's pure layer (web/static/pack_layout.js) and the profile
data that feeds it. The WebGL half (pack3d.js) is checked in the browser by the
owner; everything that can be asserted without a GPU is asserted here."""
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import ROOT
from vehicles import get_vehicle

STATIC = os.path.join(ROOT, "web", "static")
LAYOUT_JS = os.path.join(STATIC, "pack_layout.js")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def pack():
    v = get_vehicle("leaf_ze0")
    return {"module": v.PACK_MODULE, "case": v.PACK_CASE, "layout": v.PACK_LAYOUT, "sensors": v.PACK_SENSORS}


def run_node(script):
    r = subprocess.run(["node", "-e", script, LAYOUT_JS, json.dumps(pack())], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


HARNESS = """
  globalThis.window = globalThis; require(process.argv[1]);
  const P = window.PackLayout, pack = JSON.parse(process.argv[2]);
  const { bodies, modules } = P.bodies(pack);
"""


# ── profile data ──

def test_leaf_declares_a_pack_layout_covering_every_pair_once():
    v = get_vehicle("leaf_ze0")
    idx = []
    for s in v.PACK_LAYOUT:
        idx.extend(range(s["first"], s["first"] + s["n"] * 2))
    assert sorted(idx) == list(range(96))
    assert sum(s["n"] for s in v.PACK_LAYOUT) == 48
    # service manual EVB-20 (via RegGuheert, mynissanleaf 2013-04-29): MD1–24 rear stack passenger
    # end → driver end; MD25–28 rear driver footwell; MD29–36 front driver seat; MD37–44 front
    # passenger seat; MD45–48 rear passenger footwell; module n holds cells 2n−1, 2n
    rear = v.PACK_LAYOUT[0]
    assert rear["kind"] == "edge" and rear["first"] == 0 and rear["n"] == 24 and rear["verify"] == ""
    groups = [(s["z"] < 0, s["x"] > 0, s["first"], s["n"]) for s in v.PACK_LAYOUT[1:]]
    assert [g[2] for g in groups] == [48, 52, 56, 64, 72, 80, 88, 92]
    assert [(g[0], g[1], g[3]) for g in groups] == [(True, False, 2), (True, False, 2), (True, True, 4), (True, True, 4),
                                                    (False, True, 4), (False, True, 4), (False, False, 2), (False, False, 2)]
    assert all(s["verify"] for s in v.PACK_LAYOUT[1:]), "the stack order inside a group is still assumed"
    assert [s["n"] for s in v.PACK_SENSORS] == ["T1", "T2", "T3", "T4"]


def test_pack3d_tile_shares_the_cell_grid_items_and_has_a_default_height():
    v = get_vehicle("leaf_ze0")
    tile = next(t for t in v.TILES if t["id"] == "pack3d")
    cells = next(t for t in v.TILES if t["id"] == "cells")
    assert tile["items"] == cells["items"] and tile["signals"] == cells["signals"]
    assert v.DEFAULT_SPAN["pack3d"] == 12
    default = next(t for t in v.DEFAULT_TILES if t["id"] == "pack3d")
    assert default["h"] == 12                      # a canvas cannot be auto-measured
    assert all("h" not in t for t in v.DEFAULT_TILES if t["id"] != "pack3d")


def test_validate_profile_rejects_a_layout_with_a_gap():
    from vehicles import validate_profile
    v = get_vehicle("leaf_ze0")
    import types
    m = types.ModuleType("bad")
    for k in dir(v):
        if not k.startswith("__"):
            setattr(m, k, getattr(v, k))
    m.PACK_LAYOUT = [dict(s) for s in v.PACK_LAYOUT]
    m.PACK_LAYOUT[1]["first"] = 50                # 48–49 now uncovered, 50–53 doubled
    problems = validate_profile(m)
    assert any("PACK_LAYOUT" in p and "exactly once" in p for p in problems)


# ── the pure layer, from node ──

@needs_node
def test_pack_layout_js_parses_and_is_pure():
    r = subprocess.run(["node", "--check", LAYOUT_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with open(LAYOUT_JS) as f:
        src = f.read()
    assert "fetch(" not in src and "/api/" not in src and "THREE" not in src
    assert "window.PackLayout = {" in src


@needs_node
def test_layout_yields_96_bodies_and_48_modules_with_indices_once():
    out = run_node(HARNESS + """
      console.log(JSON.stringify({ n: bodies.length, m: modules.length,
        idx: bodies.map(b => b.i), mods: bodies.map(b => b.m) }));""")
    assert out["n"] == 96 and out["m"] == 48
    assert out["idx"] == list(range(96))
    assert out["mods"] == [i // 2 for i in range(96)]


@needs_node
def test_rear_block_runs_across_the_car_and_flat_stacks_rise_by_thickness():
    out = run_node(HARNESS + """
      const rear = bodies.slice(0, 48), zs = rear.map(b => b.cz), xs = new Set(rear.map(b => b.cx));
      const flat = bodies.filter(b => b.halfAxis === 'y');
      const stack = bodies.slice(56, 64);          // driver side, stack 3: 4 modules flat
      console.log(JSON.stringify({ zmin: Math.min(...zs), zmax: Math.max(...zs), xs: [...xs].length,
        rearHalf: rear.map(b => b.halfAxis), flatN: flat.length,
        ys: stack.map(b => b.cy), sy: stack[0].sy, sz: stack[0].sz, T: pack.module.T }));""")
    assert out["xs"] == 1 and out["zmax"] - out["zmin"] > 23 * out["T"]     # one row across the car
    assert -430 < out["zmin"] < -400 and 400 < out["zmax"] < 430
    assert set(out["rearHalf"]) == {"z"} and out["flatN"] == 48
    ys, T = out["ys"], out["T"]
    assert len(set(ys)) == 8 and ys[0] > ys[1]                                # first pair on the upper half
    assert abs((ys[2] - ys[0]) - T) < 1e-9 and abs((ys[6] - ys[0]) - 3 * T) < 1e-9   # one module per two pairs
    assert out["sy"] < out["T"] / 2 and out["sz"] == pack()["module"]["L"] - 3


@needs_node
def test_scales_clamp_and_orient():
    out = run_node(HARNESS + """
      const S = P.SCALES, f = P.stats([4000, 4050, 4100]);
      const rest = [4100, 4100, 4100];
      console.log(JSON.stringify({
        abs: [S.abs.t(4000, f), S.abs.t(4100, f), S.abs.t(4050, f)],
        dev: [S.dev.t(f.mean - 50, f), S.dev.t(f.mean + 50, f), S.dev.t(f.mean, f), S.dev.t(f.mean - 500, f)],
        drop: [S.drop.t(4100, f, 0, rest), S.drop.t(3800, f, 0, rest), S.drop.t(3950, f, 0, rest), S.drop.t(4000, f, 0, null)],
        stats: f, flat: S.abs.t(5, P.stats([5, 5])) }));""")
    assert out["abs"] == [0, 1, 0.5]
    assert out["dev"] == [0, 1, 0.5, 0]
    assert out["drop"] == [1, 0, 0.5, 1]
    assert out["stats"] == {"min": 4000, "max": 4100, "imin": 0, "imax": 2, "mean": 4050}
    assert out["flat"] == 1


# ── the tile in the page: TileStudio hooks it needs ──

def test_tilestudio_exposes_opts_enabled_menu_extra_and_the_applied_event():
    with open(os.path.join(STATIC, "tilestudio.js")) as f:
        src = f.read()
    pub = src[src.index("window.TileStudio = {"):]
    for fn in ("opts(id)", "enabled(id)", "menuExtra(id, fn)", "tile(id)", "size(id, w, h)", "setOpt(id, key, value)"):
        assert fn in pub, fn
    assert "new CustomEvent('tiles:applied')" in src
    assert "MENU_EXTRAS[id](m.querySelector('#tm-extra'), o, () => { save(); apply(); })" in src
    assert len(re.findall(r"['\"]/api/", src)) == 3        # still only the DEFAULTS routes


def test_cell_log_is_offered_from_both_tile_menus_and_shown_in_the_header():
    with open(os.path.join(ROOT, "web", "templates", "index.html")) as f:
        page = f.read()
    assert "window.cellLogMenu = function (box, o, commit)" in page
    assert "TileStudio.menuExtra('cells', cellLogMenu); TileStudio.init();" in page
    assert 'id="celllog-badge"' in page
    assert "classList.toggle('on', !!data.celllog && !data.playback)" in page
    with open(os.path.join(STATIC, "pack3d.js")) as f:
        assert "if (window.cellLogMenu) cellLogMenu(box, o, commit);" in f.read()


def test_tile_matches_the_grid_colours_and_carries_its_tools():
    """Owner's second browser round (2026-09-08): same colour as the grid side by side,
    sensor balls coloured and labelled in °F/°C, a side pane on click, a help hint that is
    not a button, an expand button, and the lowest pair flashing."""
    with open(os.path.join(STATIC, "pack3d.js")) as f:
        js = f.read()
    assert "scale: 'abs'" in js                                            # the grid's own scale by default
    assert "Tiles.cellColor(mv, f.min, f.max)" in js                       # exactly the grid's call
    assert "LeafSpy" not in js                                             # no third-party app names in the UI
    assert "HemisphereLight(0xffffff, 0x334466, Math.PI)" in js            # top face ≈ plain colour
    assert "°F · " in js and "* 5 / 9" in js                               # sensors labelled in both units
    assert "s.mesh.material.color.copy(c)" in js                           # sensor balls colour-mapped
    assert "function paintPane(i, cells, f, sc)" in js and "pack3d-pane-close" in js
    assert "TileStudio.size('pack3d', null, state.baseH * 2)" in js        # expand doubles the real height
    assert "flash the lowest pair white and the highest blue" in js and "state.flashLo" in js and "state.flashHi" in js
    assert "RoundedBoxGeometry(b0.sx, b0.sy, b0.sz, 2, r)" in js              # rounded like the real module
    assert "slot[i] = { g: groups.length, k }" in js                          # one instanced mesh per body size
    assert 'style="color:${css}"' in js                                        # the pane's voltages in the pair's colour
    assert "selBox" in js and "selPin" in js and "selLabel" in js             # the pinned module's marker
    assert "TileStudio.setOpt('pack3d', 'spin', on)" in js and "auto-rotate" not in js.split("menuExtra('pack3d'")[1]
    with open(os.path.join(ROOT, "web", "templates", "tiles", "pack3d.html")) as f:
        html = f.read()
    for cls in ("pack3d-spin", "pack3d-expand", "pack3d-help", "pack3d-pane"):
        assert cls in html, cls
    assert "pack3d-hint" not in html
    with open(os.path.join(STATIC, "tiles.css")) as f:
        css = f.read()
    assert ".pack3d-pane {" in css and ".pack3d-hint" not in css
