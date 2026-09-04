# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Front-end sanity: the vendored gridstack is present and the studio module parses (node optional)."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "web", "static", "vendor")


def test_gridstack_is_vendored_with_license():
    js = os.path.join(VENDOR, "gridstack-all.js")
    assert os.path.getsize(js) > 50_000
    assert os.path.exists(os.path.join(VENDOR, "gridstack.min.css"))
    with open(os.path.join(VENDOR, "LICENSE.gridstack")) as f:
        assert "MIT" in f.read()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_javascript_parses():
    for f in ("tilestudio.js", os.path.join("vendor", "gridstack-all.js")):
        r = subprocess.run(["node", "--check", os.path.join(ROOT, "web", "static", f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def _tilestudio_src():
    with open(os.path.join(ROOT, "web", "static", "tilestudio.js")) as f:
        return f.read()


def test_tilestudio_is_parameterised_and_does_not_self_boot():
    """Tile Studio must wait for TileStudio.init() so a second page (the
    simulator cockpit) can point it at its own grid, routes and storage key.
    A bare top-level load() would boot it against #tiles on every page."""
    import re
    src = _tilestudio_src()
    assert "function init(opts)" in src
    assert re.search(r"window\.TileStudio\s*=\s*\{\s*init,", src)
    body = src.split("window.TileStudio", 1)[1]
    assert not re.search(r"^\s*load\(\);\s*$", body, re.M), "self-boot load() still present"
    # every route goes through the API table; nothing else may hardcode /api/
    routes = [m.start() for m in re.finditer(r"['\"]/api/", src)]
    assert len(routes) == 3, f"expected only the 3 DEFAULTS routes, found {len(routes)}"


# ── the per-tile ⋯ menu ──
# It used to be appended to the card, whose overflow: auto clipped it as soon
# as the tile was narrower or shorter than the menu. These pin the fix:
# portalled to <body>, placed in viewport coordinates, toggled by ⋯, closed
# by Escape / outside click / Done.

def test_tile_menu_is_portalled_to_body_and_placed_in_the_viewport():
    src = _tilestudio_src()
    menu = src[src.index("function openTileMenu"):src.index("// ── header controls")]
    assert "document.body.appendChild(m)" in menu
    assert "card.appendChild(m)" not in menu
    assert "placeMenu(m, btn)" in menu and "trackMenu(m, btn)" in menu
    place = src[src.index("function placeMenu"):src.index("function trackMenu")]
    assert "getBoundingClientRect" in place
    assert "window.innerWidth" in place and "window.innerHeight" in place
    assert "MENU_MARGIN" in place   # clamped inside the viewport, not just anchored


def test_tile_menu_toggles_from_its_button_and_closes_on_escape_and_done():
    src = _tilestudio_src()
    card = src[src.index("function ensureCard"):src.index("function apply()")]
    assert "if (menuFor(t.id)) closeMenus(); else openTileMenu(t.id, card);" in card
    assert 'id="tm-done"' in src and "on('#tm-done', 'click', closeMenus)" in src
    assert "e.key === 'Escape'" in src
    # outside click still closes; clicks inside never reach gridstack's drag handles
    assert "if (!e.target.closest('.tile-menu')) closeMenus();" in src
    assert "m.addEventListener('pointerdown', e => e.stopPropagation());" in src


def test_tile_menu_css_is_fixed_and_scrolls_instead_of_clipping():
    with open(os.path.join(ROOT, "web", "static", "tilestudio.css")) as f:
        css = f.read()
    rule = css[css.index(".tile-menu {"):]
    rule = rule[:rule.index("}")]
    for decl in ("position: fixed", "min-width: 260px", "max-height: calc(100vh - 16px)", "overflow-y: auto"):
        assert decl in rule, decl
    assert "position: absolute" not in rule
    assert ".tile-menu .foot .primary" in css
