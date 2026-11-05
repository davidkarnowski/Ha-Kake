# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Link health tile and the marks on values (web/static/link.js).

The tile is a framework tile: it describes the transport, so every profile
has it, off by default, and it polls nothing. Its pure half (rows, totals,
marks) runs under node; the page is checked for the card, the script and the
`data-key` hooks the marks hang on.
"""

import json
import os
import shutil
import subprocess

import pytest

from conftest import ROOT  # noqa: F401  (sys.path is set up there)

import vehicles  # noqa: E402

LINK_JS = os.path.join(ROOT, "web", "static", "link.js")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run_node(script):
    r = subprocess.run(["node", "-e", "globalThis.window = globalThis; require(process.argv[1]); const L = window.LinkTile;"
                        + script, LINK_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def page():
    with open(os.path.join(ROOT, "web", "templates", "index.html")) as f:
        html = f.read()
    with open(os.path.join(ROOT, "web", "templates", "tiles", "link.html")) as f:
        return html, f.read()


# ── the tile exists for every car, off, and asks for nothing ─────────────

def test_it_is_a_framework_tile_off_by_default_that_polls_nothing():
    tile = next(t for t in vehicles.FRAMEWORK_TILES if t["id"] == "link")
    assert tile["items"] == [] and tile["signals"] == [] and tile["name"] == "Link health"
    default = next(t for t in vehicles.FRAMEWORK_DEFAULTS if t["id"] == "link")
    assert default["enabled"] is False
    for name in ("leaf_ze0", "lancer_2009"):
        mod = vehicles.get_vehicle(name)
        assert "link" in {t["id"] for t in vehicles.tiles(mod)}
        assert "link" in {t["id"] for t in vehicles.default_tiles(mod)}


def test_the_page_carries_the_card_the_script_and_the_hook():
    html, card = page()
    assert '{% include "tiles/link.html" %}' in html and '<script src="/static/link.js"></script>' in html
    assert "LinkTile.render(document, data)" in html
    assert 'data-tile="link"' in card and 'href="/api/ledger"' in card
    for el in ("link-proof", "link-empty", "link-items", "link-rows", "link-totals", "link-marks", "link-stall"):
        assert f'id="{el}"' in card, el


@pytest.mark.parametrize("element, key", [("soc-num", "soc"), ("pack-v", "pack_v"), ("lv-val", "lv_volts"),
                                          ("power-val", "power_kw"), ("current-val", "current_a")])
def test_the_values_that_can_be_marked_say_which_key_they_show(element, key):
    html, _ = page()
    assert f'id="{element}" data-key="{key}"' in html
    assert key in vehicles.get_vehicle("leaf_ze0").LIMITS             # and the profile bounds every one of them


# ── the pure half, under node ────────────────────────────────────────────

@needs_node
def test_link_js_parses():
    assert subprocess.run(["node", "--check", LINK_JS], capture_output=True, text=True).returncode == 0


@needs_node
def test_rows_say_what_each_request_came_to_and_where_its_time_went():
    out = run_node("""
      console.log(JSON.stringify(L.rows({
        lbc05: {state: 'late', late_s: 12.61},
        lbc01: {state: 'ok', via: 'ack', rtt_ms: 253.4, net_ms: 49.2, queue_ms: 3, bus_ms: 201},
        lbc02: {state: 'timeout', rtt_ms: 16001},
        lbc04: {state: 'superseded'},
      })));""")
    assert [r["item"] for r in out] == ["lbc01", "lbc02", "lbc04", "lbc05"]
    assert out[0] == {"item": "lbc01", "state": "ok", "bad": False, "rtt": "253 ms", "net": "49 ms", "queue": "3 ms",
                      "bus": "201 ms", "via": "ack"}
    assert out[1]["state"] == "no answer" and out[1]["bad"] is True and out[1]["rtt"] == "16 s"
    assert out[2]["state"] == "superseded" and out[3]["state"] == "late 12.6 s"


@needs_node
def test_the_table_remembers_items_a_cycle_did_not_ask():
    out = run_node("""
      let k = L.merge({}, {lbc01: {state: 'ok'}, lbc05: {state: 'timeout'}});
      k = L.merge(k, {lbc01: {state: 'ok', rtt_ms: 9}});
      console.log(JSON.stringify(k));""")
    assert out == {"lbc01": {"state": "ok", "rtt_ms": 9}, "lbc05": {"state": "timeout"}}


@needs_node
def test_totals_lead_with_what_was_answered_and_hide_nothing():
    out = run_node("""
      console.log(JSON.stringify(L.totals({totals: {sent: 5214, ok: 5210, late: 3, late_credited: 2, timeouts: 4,
                                                    mirrored_other: 9, some_new_count: 1}})));""")
    assert out[0] == {"key": "ok", "label": "answered", "n": "5210 of 5214"}
    assert [(t["key"], t["n"]) for t in out[1:]] == [("late", "3"), ("late_credited", "2"), ("timeouts", "4"),
                                                      ("some_new_count", "1")]
    assert out[-1]["label"] == "some new count"                       # a count the tile has no words for is still shown
    assert run_node("console.log(JSON.stringify(L.totals(null)));") == []


@needs_node
def test_marks_and_how_a_reply_was_proven_read_plainly():
    out = run_node("""
      console.log(JSON.stringify([
        L.marks({soc: 'out-of-limits', g05_insulation_kohm: 'late'}),
        L.markFor({temps_f: 'late'}, 'temps_f.2'), L.markFor({soc: 'late'}, 'pack_v'), L.markFor(null, 'soc'),
        L.proof({via: 'ack'}), L.proof({via: 'frames'}), L.proof({}),
        L.ms(49.4), L.ms(1500), L.ms(16001), L.ms(null),
      ]));""")
    assert out[0] == [{"key": "g05_insulation_kohm", "mark": "late", "text": "arrived late"},
                      {"key": "soc", "mark": "out-of-limits", "text": "outside its physical limits"}]
    assert out[1:4] == ["late", None, None]
    assert out[4:7] == ["replies identified by the bridge", "replies picked by their echo", ""]
    assert out[7:] == ["49 ms", "1.5 s", "16 s", ""]


# ── the page as the server renders it ────────────────────────────────────

def test_the_rendered_dashboard_has_the_tile_its_script_and_the_marked_values(isolated_reader):
    import app as webapp
    body = webapp.app.test_client().get("/").get_data(as_text=True)
    assert 'data-tile="link"' in body and '/static/link.js' in body and 'id="link-rows"' in body
    assert body.count('data-key="') >= 5
    css = webapp.app.test_client().get("/static/hakake.css").get_data(as_text=True)
    assert ".q-late" in css and ".q-out-of-limits" in css and ".link-items" in css
    assert webapp.app.test_client().get("/static/link.js").status_code == 200
