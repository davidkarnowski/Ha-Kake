# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strings the page did not write itself are shown as text.

web/static/html.js is the one escape helper; the tile renderers build markup
from numbers only; the simulator cockpit only talks to a control API on this
machine. The node tests load the real scripts with a stub window, the way the
other front-end tests do; the source checks pin the call sites that take
user-entered text (tile titles, layout names, flag labels).
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import ROOT  # noqa: E402

STATIC = os.path.join(ROOT, "web", "static")
TEMPLATES = os.path.join(ROOT, "web", "templates")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def node(script, *args):
    r = subprocess.run(["node", "-e", script, *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── the helper ───────────────────────────────────────────────────────────

@needs_node
def test_html_esc_turns_markup_characters_into_entities():
    out = node("""
      globalThis.window = globalThis; require(process.argv[1]);
      console.log(JSON.stringify([Html.esc(`<b a="1" b='2'>&</b>`), Html.esc(null), Html.esc(undefined),
                                  Html.esc(42.5), Html.esc('pull <40 A'), Html.attr === Html.esc]));
    """, os.path.join(STATIC, "html.js"))
    assert out[0] == "&lt;b a=&quot;1&quot; b=&#39;2&#39;&gt;&amp;&lt;/b&gt;"
    assert out[1:5] == ["", "", "42.5", "pull &lt;40 A"]
    assert out[5] is True


def test_html_js_loads_first_on_both_pages():
    index, sim = read(TEMPLATES, "index.html"), read(TEMPLATES, "sim.html")
    assert index.index('src="/static/html.js"') < index.index('src="/static/pack_layout.js"')
    assert index.index('src="/static/html.js"') < index.index('src="/static/tilestudio.js"')
    assert sim.index('src="/static/html.js"') < sim.index('src="/static/alerts.js"')


# ── tile renderers: numbers only ─────────────────────────────────────────

TILE_HARNESS = """
  globalThis.window = globalThis; require(process.argv[1]);
  const els = {};
  const mk = id => els[id] || (els[id] = { id, innerHTML: '', textContent: '', className: '', style: {},
    classList: { toggle() {}, add() {} }, setAttribute() {}, getAttribute() { return 'x'; }, querySelectorAll() { return []; } });
  const root = { querySelector: s => mk(s.replace(/^#/, '')), querySelectorAll: () => [] };
"""


@needs_node
def test_vehicle_and_climate_tiles_show_text_values_as_dashes_not_markup():
    out = node(TILE_HARNESS + """
      const s = '<img src=x onerror=1>';
      Tiles.renderVehicle(root, { soh_dash_pct: s, odometer_mi: s, speed_mph: s, range_mi: s });
      Tiles.renderClimate(root, { cabin_temp_c: s, cabin_temp_f: 70, hvac_blower_v: s, hvac_fan_on: true, hvac_fan_speed: s });
      console.log(JSON.stringify(['veh-soh', 'veh-odo', 'veh-speed', 'veh-range', 'hvac-cabin', 'hvac-fan'].map(i => els[i].innerHTML)));
    """, os.path.join(STATIC, "tiles.js"))
    for html in out:
        assert "<img" not in html and "onerror" not in html
        assert "--" in html


@needs_node
def test_vehicle_and_climate_tiles_still_format_numbers():
    out = node(TILE_HARNESS + """
      Tiles.renderVehicle(root, { soh_dash_pct: 85.2, odometer_mi: 65632, speed_mph: 31.4, range_mi: 48.6 });
      Tiles.renderClimate(root, { cabin_temp_c: 21.5, hvac_blower_v: 4, hvac_fan_on: true, hvac_fan_speed: 2 });
      console.log(JSON.stringify(['veh-soh', 'veh-odo', 'veh-speed', 'veh-range', 'hvac-cabin', 'hvac-fan'].map(i => els[i].innerHTML)));
    """, os.path.join(STATIC, "tiles.js"))
    assert out[0] == "85.2<small>%</small>"
    assert out[1].startswith("65") and out[1].endswith("<small>mi</small>")
    assert out[2] == "31<small>mph</small>" and out[3].startswith("49<small>")
    assert out[4] == "71°F<small>21.5°C</small>"
    assert out[5].startswith("2<small") and "4 V" in out[5]


# ── the cockpit's control URL ────────────────────────────────────────────

@needs_node
def test_cockpit_accepts_only_a_control_url_on_this_machine():
    src = read(STATIC, "sim.js")
    start = src.index("const LOOPBACK = ")
    end = src.index("async function resolveControl")
    out = node(src[start:end] + """
      const urls = ['http://127.0.0.1:8099', 'http://localhost:8099/sim', 'http://[::1]:8099',
                    'https://example.com', 'http://example.com:8099', 'http://127.0.0.1.example.com',
                    'http://127.0.0.1@example.com/', 'javascript:alert(1)', 'not a url'];
      console.log(JSON.stringify(urls.map(loopbackUrl)));
    """)
    assert out[:3] == ["http://127.0.0.1:8099", "http://localhost:8099", "http://[::1]:8099"]
    assert out[3:] == [None] * 6


def test_every_control_url_source_goes_through_the_loopback_check():
    body = read(STATIC, "sim.js")
    body = body[body.index("async function resolveControl"):body.index("function showNotice")]
    assert "loopbackUrl(q)" in body
    assert "loopbackUrl(window.SIM_CONTROL_URL)" in body
    assert "loopbackUrl(st.sim_control_url)" in body
    assert "return q.replace" not in body


# ── user-entered text: the call sites ────────────────────────────────────

def test_user_entered_text_reaches_markup_only_through_the_helper():
    ts = read(STATIC, "tilestudio.js")
    assert "const E = v => window.Html.esc(v);" in ts
    # the card title is filled with textContent, never interpolated into markup
    card = ts[ts.index("if (!card && t.kind === 'signal')"):ts.index("grid.appendChild(card);")]
    assert "t.title" not in card
    assert "${E(l.name)}" in ts                                         # layout names
    assert "E(t.title || (s ? s.label : t.id))" in ts                   # tiles panel
    assert 'value="${E(t.title || \'\')}"' in ts                        # the menu's title field
    assert "htesc" not in ts and ".replace(/\"/g, '&quot;')" not in ts  # no local half-escapers left
    page = read(TEMPLATES, "index.html")
    assert "' · ' + Html.esc(f.label)" in page                          # timeline flag labels
    assert "Html.esc(sessionLabel(s))" in page


def test_no_innerhtml_template_interpolates_a_bare_title_or_label():
    """A coarse net under the pinned sites above: no template literal assigned
    to innerHTML in these files interpolates .title / .label / .name unescaped."""
    risky = re.compile(r"\$\{\s*[A-Za-z_.]*\.(title|label|name)\b[^}]*\}")
    for path in (os.path.join(STATIC, "tilestudio.js"), os.path.join(STATIC, "pack3d.js"),
                 os.path.join(TEMPLATES, "index.html")):
        src = read(path)
        for m in re.finditer(r"innerHTML\s*=\s*`([^`]*)`", src):
            for hit in risky.finditer(m.group(1)):
                assert hit.group(0).startswith(("${E(", "${Html.esc(")), (path, hit.group(0))
