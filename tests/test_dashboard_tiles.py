# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dashboard's four styled tiles are shared parts, not inline code.

Vehicle & shifter, tires, body and climate live as Jinja partials in
web/templates/tiles/, are painted by web/static/tiles.js (window.Tiles),
and are styled by tiles.css on top of the page chrome in hakake.css — so a
second page (the simulator cockpit) can host the same tiles. These tests pin
the seam: the partials are included once each, the renderers are scoped to a
root element rather than the document, the shared CSS is linked before the
page's own style so the cascade is the one the dashboard always had, and the
temperature formatter reproduces the dashboard's exact strings.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import app as webapp  # noqa: E402  (web/app.py)
import reader as rd  # noqa: E402
from store import Store  # noqa: E402

TEMPLATES = os.path.join(ROOT, "web", "templates")
STATIC = os.path.join(ROOT, "web", "static")
TILES_JS = os.path.join(STATIC, "tiles.js")

# partial → the element id that only that partial's markup carries
PARTIALS = {"vehicle": "shifter", "tires": "wheels", "body": "body-svg", "climate": "hvac-cabin"}
EXPORTS = ["fmtTemp", "fmtTempParts", "tempColor", "socColor", "drawWheel", "setShifter",
           "renderVehicle", "renderTires", "renderBody", "renderClimate"]


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def page(tmp_path, monkeypatch):
    """GET / rendered by the Flask test client with every file under tmp_path."""
    rd.set_vehicle("leaf_ze0")
    monkeypatch.setattr(webapp, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(webapp, "DEMO", None)
    for attr, name in (("STATE_FILE", "state.json"), ("TILES_FILE", "tiles.json"),
                       ("CALIB_FILE", "calibration.json"), ("LAYOUTS_FILE", "layouts.json"),
                       ("PAUSE_FILE", "reader.pause")):
        monkeypatch.setattr(rd, attr, str(tmp_path / name))
    store = Store(str(tmp_path / "tiles.db"))
    monkeypatch.setattr(webapp, "store", lambda: store)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        r = c.get("/")
    store.close()
    rd.set_vehicle("leaf_ze0")
    assert r.status_code == 200
    return r.get_data(as_text=True)


# ── partials ──

def test_each_partial_exists_and_is_included_once():
    index = read(os.path.join(TEMPLATES, "index.html"))
    for name in PARTIALS:
        assert os.path.exists(os.path.join(TEMPLATES, "tiles", f"{name}.html")), name
        assert index.count('{%% include "tiles/%s.html" %%}' % name) == 1, name


def test_rendered_page_carries_each_tile_anchor_exactly_once(page):
    for name, anchor in PARTIALS.items():
        assert page.count(f'id="{anchor}"') == 1, (name, anchor)
        assert page.count(f'data-tile="{name}"') == 1, name


def test_partials_render_verbatim(page):
    """The include must not reshape the markup — the file's bytes are the page's bytes."""
    for name in PARTIALS:
        body = read(os.path.join(TEMPLATES, "tiles", f"{name}.html")).rstrip("\n")
        assert body in page, name


# ── tiles.js ──

def test_tiles_js_exports_the_ten_names():
    m = re.search(r"window\.Tiles\s*=\s*\{([^}]*)\}", read(TILES_JS))
    assert m, "no window.Tiles export"
    names = [n.strip() for n in m.group(1).split(",") if n.strip()]
    assert names == EXPORTS


def test_tiles_js_renderers_are_scoped_to_root_not_document():
    js = read(TILES_JS)
    assert "document.getElementById(" not in js
    assert "document.querySelector" not in js
    for fn in ("renderVehicle", "renderTires", "renderBody", "renderClimate"):
        assert re.search(r"function %s\(root, \w+\)" % fn, js), fn


def test_index_delegates_the_four_tiles_and_shares_the_colour_scales():
    index = read(os.path.join(TEMPLATES, "index.html"))
    for fn in ("renderVehicle", "renderTires", "renderBody", "renderClimate"):
        assert f"Tiles.{fn}(document, data);" in index, fn
    assert "const { tempColor, socColor } = Tiles;" in index
    for gone in ("function tempColor(", "function socColor(", "function drawWheel(", "function setShifter("):
        assert gone not in index, gone


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_tiles_js_parses():
    r = subprocess.run(["node", "--check", TILES_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_fmt_temp_reproduces_the_dashboard_strings():
    """The six inline sites differed only in how the °C was written; fmtTemp covers all of them."""
    script = """
      globalThis.window = globalThis; require(process.argv[1]);
      const T = window.Tiles;
      console.log(JSON.stringify({
        amb:  T.fmtTemp(34, 93.2),                       // hvac-amb / hvac-evap: f.toFixed(0), raw integer c
        pack: T.fmtTemp(31.5, 88.7),                     // hvac-pack: raw one-decimal c
        set:  T.fmtTemp(21.7, 71.06, { cDec: 0 }),       // hvac-set: c.toFixed(0)
        derived: T.fmtTemp(24, null),                    // pack sensors: f computed from c, toFixed(0)
        below: T.fmtTemp(-3, null),
        cabin: T.fmtTempParts(27, 80.6),                 // cabin readout: °F<small>°C</small>
        cold: T.tempColor(5), hot: T.tempColor(45), low: T.socColor(10), full: T.socColor(90),
      }));
    """
    r = subprocess.run(["node", "-e", script, TILES_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["amb"] == "93°F / 34°C"
    assert out["pack"] == "89°F / 31.5°C"
    assert out["set"] == "71°F / 22°C"
    assert out["derived"] == "75°F / 24°C"
    assert out["below"] == "27°F / -3°C"
    assert out["cabin"] == {"f": "81", "c": "27"}
    assert (out["cold"], out["hot"], out["low"], out["full"]) == ("var(--blue)", "var(--red)", "var(--red)", "var(--green)")


# ── CSS and script order ──

def test_head_links_shared_css_after_tilestudio_and_before_inline_style(page):
    head = page[:page.index("</head>")]
    order = [head.index(s) for s in ('href="/static/tilestudio.css"', 'href="/static/hakake.css"',
                                     'href="/static/tiles.css"', "<style>")]
    assert order == sorted(order), order
    assert head.count("<style>") == 1


def test_tiles_js_loads_before_the_page_script_and_tilestudio_is_booted(page):
    assert page.index('src="/static/tiles.js"') < page.index("const { tempColor, socColor } = Tiles;")
    assert page.index('src="/static/tilestudio.js"') < page.index("TileStudio.init();")
    assert page.count("TileStudio.init();") == 1


def _rules(text, prefix=""):
    """(selector, declarations) pairs, flattening @media / @keyframes by prefixing the selector."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out, i = [], 0
    while (j := text.find("{", i)) >= 0:
        sel, depth, k = " ".join(text[i:j].split()), 1, j + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            k += 1
        body = text[j + 1:k - 1]
        if "{" in body:
            out.extend(_rules(body, prefix + sel + " "))
        else:
            out.append((prefix + sel, tuple(" ".join(d.split()) for d in body.split(";") if d.strip())))
        i = k
    return out


def test_shared_css_holds_the_tile_and_chrome_rules_and_nothing_is_duplicated(page):
    hakake = _rules(read(os.path.join(STATIC, "hakake.css")))
    tiles = _rules(read(os.path.join(STATIC, "tiles.css")))
    inline = _rules(re.search(r"<style>(.*?)</style>", page, re.S).group(1))
    sel = lambda rules: {r[0] for r in rules}  # noqa: E731
    # what each file is for
    assert {":root", "body", ".card", ".card-title", ".tiles", ".tiles > .card", ".tile-age", ".tiles-menu"} <= sel(hakake)
    assert {".shifter", ".wheel", ".door", ".lamp", ".fan-rotor", ".hvac-big", "html.shot *"} <= sel(tiles)
    # a rule lives in exactly one place — no selector is styled twice across the three sources
    assert not (sel(hakake) & sel(tiles))
    assert not (sel(hakake) & sel(inline))
    assert not (sel(tiles) & sel(inline))


# ── the tires tile scales with its card ──

def test_tire_block_is_centred_and_sized_by_the_card_not_fixed():
    """The four wheel SVGs were 84 px squares pinned at the top-left of the
    card body. Now the card is a flex column, the wheels block takes the free
    height as an inline-size container, and each wheel is a fraction of the
    block's width (clamped) that shrinks to its grid row when the tile is
    short — so the block stays centred and grows with the tile at every span."""
    rules = dict(_rules(read(os.path.join(STATIC, "tiles.css"))))
    assert "display: flex" in rules['.card[data-tile="tires"]'] and "flex-direction: column" in rules['.card[data-tile="tires"]']
    wheels = rules[".wheels"]
    for decl in ("container-type: inline-size", "grid-template-columns: repeat(2, minmax(0, 1fr))",
                 "grid-template-rows: repeat(2, minmax(0, 1fr))", "flex: 1 1 auto", "min-height: 0", "margin: auto 0"):
        assert decl in wheels, decl
    svg = rules[".wheel svg"]
    assert "width: clamp(64px, 24cqw, 200px)" in svg and "width: 84px" in svg   # fallback first, container units win
    assert "aspect-ratio: 1 / 1" in svg and "min-height: 0" in svg and "height: auto" in svg
    assert any(d.startswith("font-size: clamp(") for d in rules[".wheel-psi"])
    assert any(d.startswith("font-size: clamp(") for d in rules[".wheel-name"])
    # the markup did not change: still four wheels and the note under them
    html = read(os.path.join(TEMPLATES, "tiles", "tires.html"))
    assert html.count('class="wheel" data-w=') == 4 and html.index('id="wheels"') < html.index('id="tire-note"')
