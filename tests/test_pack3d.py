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
    return {"module": v.PACK_MODULE, "case": v.PACK_CASE, "layout": v.PACK_LAYOUT, "sensors": v.PACK_SENSORS, "modes": v.PACK_MODES}


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
    assert [s["n"] for s in v.PACK_SENSORS] == ["T1", "T2", "T3", "T4"] and all(s["where"] for s in v.PACK_SENSORS)


def test_pack3d_tile_shares_the_cell_grid_items_and_has_a_default_height():
    v = get_vehicle("leaf_ze0")
    tile = next(t for t in v.TILES if t["id"] == "pack3d")
    cells = next(t for t in v.TILES if t["id"] == "cells")
    assert set(cells["items"]) < set(tile["items"]) and "lbc04" in tile["items"]      # the sensors need the temperatures
    assert set(cells["signals"]) < set(tile["signals"]) and "temp_avg_f" in tile["signals"]
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
      console.log(JSON.stringify({ n: bodies.length, m: modules.length, values: P.bodies(pack).values,
        idx: bodies.map(b => b.i), vs: bodies.map(b => b.v), mods: bodies.map(b => b.m) }));""")
    assert out["n"] == 96 and out["m"] == 48 and out["values"] == 96
    assert out["idx"] == list(range(96)) and out["vs"] == list(range(96))          # Leaf: one body per value
    assert out["mods"] == [i // 2 for i in range(96)]


@needs_node
def test_grouped_and_sliced_layouts_map_bodies_to_values():
    """The abstraction other packs need: `group` (Prius NiMH — one voltage per two
    modules) and `split` other than 2, on the same geometry code."""
    prius = {"module": {"L": 285, "W": 106, "T": 20}, "case": {"L": 700, "W": 400, "H": 150},
             "layout": [{"name": "rear row", "kind": "edge", "x": 0, "z": 0, "n": 28, "first": 0, "group": 2}],
             "sensors": [], "modes": [{"id": "volt", "key": "blocks", "name": "block", "unit": "V"}]}
    sliced = {"module": {"L": 300, "W": 200, "T": 40}, "case": {"L": 700, "W": 400, "H": 150},
              "layout": [{"name": "a", "kind": "flat", "x": 0, "z": 0, "n": 3, "first": 0, "split": 4},
                         {"name": "b", "kind": "flat", "x": 300, "z": 0, "n": 2, "first": 12, "split": 1}]}
    r = subprocess.run(["node", "-e", """
      globalThis.window = globalThis; require(process.argv[1]);
      const P = window.PackLayout, a = P.bodies(JSON.parse(process.argv[2])), b = P.bodies(JSON.parse(process.argv[3]));
      console.log(JSON.stringify({ an: a.bodies.length, am: a.modules.length, av: a.values, avs: a.bodies.map(x => x.v), ashared: a.bodies.every(x => x.shared),
        bn: b.bodies.length, bv: b.values, bvs: b.bodies.map(x => x.v), bys: b.bodies.slice(0, 4).map(x => x.cy), bsy: b.bodies[0].sy, bsy1: b.bodies[12].sy }));""",
        LAYOUT_JS, json.dumps(prius), json.dumps(sliced)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["an"] == 28 and out["am"] == 28 and out["av"] == 14 and out["ashared"]
    assert out["avs"] == [i // 2 for i in range(28)]                                    # two modules share a value
    assert out["bn"] == 14 and out["bv"] == 14 and out["bvs"] == list(range(14))
    ys = out["bys"]
    assert ys[0] > ys[1] > ys[2] > ys[3] and abs((ys[0] - ys[3]) - 30) < 1e-9        # four 10 mm slices, first on top
    assert out["bsy"] < 10 and out["bsy1"] < 40                                        # slice thickness, then a whole module


def test_validate_profile_checks_grouped_coverage_and_modes():
    from vehicles import validate_profile
    import types
    v = get_vehicle("leaf_ze0")
    m = types.ModuleType("grouped")
    for k in dir(v):
        if not k.startswith("__"):
            setattr(m, k, getattr(v, k))
    m.PACK_LAYOUT = [{"name": "row", "kind": "edge", "x": 0, "z": 0, "n": 28, "first": 0, "group": 2}]
    assert not [x for x in validate_profile(m) if "PACK_LAYOUT" in x]                  # 28 modules → values 0..13
    m.PACK_LAYOUT = [{"name": "row", "kind": "edge", "x": 0, "z": 0, "n": 27, "first": 0, "group": 2}]
    assert any("group=2" in x for x in validate_profile(m))
    m.PACK_LAYOUT = v.PACK_LAYOUT
    m.PACK_MODES = [{"id": "temp", "key": "module_temps_f", "name": "module", "unit": "°F", "scales": ["abs", "nope"]}]
    assert any("unknown scale" in x for x in validate_profile(m))
    m.PACK_MODES = [{"id": "temp"}]
    assert any("PACK_MODES entries need" in x for x in validate_profile(m))


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
    assert "cellColorMenu(box, o, commit); cellLogMenu(box, o, commit);" in page   # both sets of rows, one registration
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
    assert "Tiles.cellColor(val, f.min, f.max)" in js                      # exactly the grid's call
    assert "LeafSpy" not in js and "№" not in js                             # no third-party app names in the UI
    # pairs are numbered 1–96 on screen (the manual's count); indices stay 0-based underneath
    assert "${md.name} <b>${v + 1}</b>" in js and "`${b.v + 1} · ${cells[b.v]}`" in js and "${mode().name} ${vs.map(v => v + 1).join(' & ')}" in js
    assert "function valuesOf(data)" in js and "MODES = (pack.modes && pack.modes.length)" in js   # value modes from the profile
    assert "if (md.invert) t = 1 - t;" in js                                                    # temperatures: hot is red
    with open(os.path.join(ROOT, "web", "templates", "index.html")) as f:
        page = f.read()
    assert 'id="cell-${c0}">${c0 + 1}</div>' in page and "Cell pair ${i + 1}:" in page
    assert "HemisphereLight(0xffffff, 0x334466, Math.PI)" in js            # top face ≈ plain colour
    assert "°F · " in js and "* 5 / 9" in js                               # sensors labelled in both units
    assert "s.mesh.material.color.copy(c)" in js                           # sensor balls colour-mapped
    assert "function paintPane(i, cells, f, sc)" in js and "pack3d-pane-close" in js
    assert "TileStudio.size('pack3d', null, state.baseH * 2)" in js        # expand doubles the real height
    assert "flash the lowest ${md.name} white and the highest blue" in js and "state.flashing = flashing" in js
    assert "Flash all below" in js and 'data-k="flashBelow"' in js and 'data-k="flashAbove"' in js   # threshold flashes
    assert "if (below > 0 && cells[v] < below) add(v, WHITE);" in js and "if (above > 0 && cells[v] > above) add(v, BLUE);" in js
    assert "RoundedBoxGeometry(b0.sx, b0.sy, b0.sz, 2, r)" in js              # rounded like the real module
    assert "slot[i] = { g: groups.length, k }" in js                          # one instanced mesh per body size
    assert 'style="color:${css}"' in js                                        # the pane's voltages in the pair's colour
    assert "selBox" in js and "selPin" in js and "selLabel" in js             # the pinned module's marker
    # render on demand (a new record, hover, resize, camera motion, a 20 fps flash tick), never a blind 60 fps
    assert "const animating = state.flashing.length > 0 || selBox.visible;" in js   # full rate while breathing
    assert "if (labels) labelRenderer.render(scene, camera);" in js                  # labels only when they moved
    assert "new IntersectionObserver(" in js and "if (!(needsRender || moving || animating)) return;" in js
    assert "function paintSensorPane(sj)" in js and "state.pinnedSensor" in js  # sensors are selectable too
    assert "...sensors.map(s => s.mesh)" in js                                  # and picked by the raycast
    assert 'class="pack3d-pane-mod"' in js and "spread rank" in js             # the module's own section
    assert "TileStudio.setOpt('pack3d', 'spin', on)" in js and "auto-rotate" not in js.split("menuExtra('pack3d'")[1]
    with open(os.path.join(ROOT, "web", "templates", "tiles", "pack3d.html")) as f:
        html = f.read()
    for cls in ("pack3d-spin", "pack3d-expand", "pack3d-help", "pack3d-pane"):
        assert cls in html, cls
    assert "pack3d-hint" not in html
    with open(os.path.join(STATIC, "tiles.css")) as f:
        css = f.read()
    assert ".pack3d-pane {" in css and ".pack3d-hint" not in css


# ── the fixed colour scale (owner's request 2026-09-10) ──

def test_fixed_scale_holds_its_range_and_clamps_outside_it():
    """`abs` rescales to every frame, so one colour means different voltages as the
    pack sags. `fixed` maps a value into bounds that never move — the profile's, or
    the tile's own two numbers — and clamps anything beyond them."""
    out = run_node(HARNESS + """
      const S = P.SCALES, md = { fixed: [3000, 4200], unit: 'mV' }, f = P.stats([3900, 4000, 4100]);
      const t = (v, o) => S.fixed.t(v, f, 0, null, md, o || {});
      console.log(JSON.stringify({
        ends:    [t(3000), t(4200), t(3600)],
        clamps:  [t(2000), t(5000)],
        tile:    t(3600, { fixedLo: 3500, fixedHi: 4200 }),
        legend:  [S.fixed.lo(f, 'mV', md, {}), S.fixed.hi(f, 'mV', md, {})],
        tileleg: [S.fixed.lo(f, 'mV', md, { fixedLo: 3500 }), S.fixed.hi(f, 'mV', md, { fixedHi: 4150 })],
        blank:   P.fixedRange(md, { fixedLo: '', fixedHi: '' }),
        bad:     P.fixedRange(md, { fixedLo: 4500, fixedHi: 3000 }),
        junk:    P.fixedRange(md, { fixedLo: 'x' }),
        // the same value keeps its colour as the frame moves; `abs` does not
        steady:  [t(3900), t(3900)],
        absmoves: [S.abs.t(3900, P.stats([3900, 4100])), S.abs.t(3900, P.stats([3800, 4100]))],
      }));""")
    assert out["ends"] == [0, 1, 0.5]
    assert out["clamps"] == [0, 1]
    assert abs(out["tile"] - (3600 - 3500) / (4200 - 3500)) < 1e-9
    assert out["legend"] == ["3000 mV", "4200 mV"]
    assert out["tileleg"] == ["3500 mV", "4150 mV"]
    assert out["blank"] == [3000, 4200] and out["bad"] == [3000, 4200] and out["junk"] == [3000, 4200]
    assert out["steady"][0] == out["steady"][1]
    assert out["absmoves"][0] != out["absmoves"][1], "the frame-relative scale moves; that is the point"


def test_leaf_declares_a_fixed_range_and_the_validator_demands_a_sane_one():
    import vehicles
    leaf = get_vehicle("leaf_ze0")
    mode = next(m for m in leaf.PACK_MODES if m["id"] == "volt")
    assert "fixed" in mode["scales"] and "abs" in mode["scales"]
    lo, hi = mode["fixed"]
    assert (lo, hi) == (3000, 4200), "the bounds the signal registry declares for a cell pair"
    assert leaf.SIGNALS["cell_min"]["min"] == lo and leaf.SIGNALS["cell_max"]["max"] == hi
    for bad in ([4200, 3000], [3000], "3000-4200", None, [True, False]):
        assert vehicles._fixed_ok(bad) is False, bad
    assert vehicles._fixed_ok([3000, 4200]) is True


def test_both_tiles_offer_the_fixed_range_and_say_it_is_display_only():
    with open(os.path.join(ROOT, "web", "templates", "index.html")) as f:
        page = f.read()
    assert "window.cellColorMenu = function (box, o, commit)" in page
    assert "PackLayout.fixedRange(cellMode, cellOpts)" in page          # the grid resolves the same way
    assert "cellColor(PackLayout.clamp(mv, range[0], range[1]), range[0], range[1])" in page
    assert 'id="cell-legend-lo"' in page and 'id="cell-legend-hi"' in page
    assert "Display only." in page
    with open(os.path.join(STATIC, "pack3d.js")) as f:
        src = f.read()
    assert "sc.t(val, f, i, state.rest, md, state.opts)" in src          # opts reach the scale
    assert "sc.lo(f, md.unit, md, state.opts)" in src
    assert 'data-k="fixedLo"' in src and 'data-k="fixedHi"' in src


# ── a profile with no pack (the Lancer) ──

NO_PACK_RENDER = r"""
  const fs = require('fs'), path = require('path');
  const [src, out, packJson] = process.argv.slice(1);
  // three.js cannot load in node: swap the four imports for inert stand-ins
  const stub = "const THREE = new Proxy({}, { get: () => function () {} });"
    + " class OrbitControls {} class CSS2DRenderer {} class CSS2DObject {} class RoundedBoxGeometry {}";
  fs.writeFileSync(out, stub + '\n' + fs.readFileSync(src, 'utf8').replace(/^import .*$/gm, ''));
  const el = (w) => ({ clientWidth: w, querySelector: () => null, addEventListener() {} });
  globalThis.window = globalThis;
  globalThis.document = { addEventListener() {}, querySelector: () => null };
  window.PACK = JSON.parse(packJson);
  window.PackLayout = {};
  const root = { querySelector: (s) => s === '#pack3d' ? el(300) : null };
  import(out).then(() => {
    window.Pack3D.render(root, { timestamp: '2026-09-12T19:27:44Z', rpm: 703 });
    console.log('rendered');
  }).catch(e => { console.error(e.stack); process.exit(1); });
"""


@needs_node
@pytest.mark.parametrize("pack_json", ["null", "undefined"])
def test_render_with_no_pack_does_not_throw(tmp_path, pack_json):
    """The Lancer page has window.PACK = null. From b59a6c4 build() declared a local
    `const PACK`, which made its own `typeof PACK` a ReferenceError; updateDash threw
    on every poll and the page said "Dashboard offline" over a healthy reader."""
    script = NO_PACK_RENDER.replace("JSON.parse(packJson)", pack_json)
    r = subprocess.run(["node", "-e", script, os.path.join(STATIC, "pack3d.js"),
                        str(tmp_path / "pack3d.mjs"), "null"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "rendered"


def test_pack3d_never_shadows_the_page_pack():
    with open(os.path.join(STATIC, "pack3d.js")) as f:
        src = f.read()
    assert not re.search(r"\b(const|let|var)\s+PACK\b", src)
    assert not re.search(r"[,{]\s*PACK\s*=", src)


def test_poll_logs_what_made_the_dashboard_say_offline():
    with open(os.path.join(ROOT, "web", "templates", "index.html")) as f:
        page = f.read()
    catch = page[page.index("async function poll()"):]
    catch = catch[catch.index("} catch (e) {"):catch.index("'Dashboard offline'")]
    assert "console.error(" in catch
