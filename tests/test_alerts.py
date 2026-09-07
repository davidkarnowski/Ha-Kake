# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Audible threshold alerts — per value, from each tile's ⋯ menu.

The rules live in a tile's `opts.alerts` (one per signal the tile shows) and
are evaluated in the browser by web/static/alerts.js: a pure engine that fires
on the transition into breach, nags on a per-rule repeat, re-arms only after
the value comes back inside a hysteresis band, and freezes while the data is
stale or the reader is not "ok". The sound is the browser's own oscillator.
These tests drive the engine from node the way test_dashboard_tiles.py drives
tiles.js, pin the source-level seams (no network in the engine, thresholds
commit on change), and round-trip the rules through every store they ride in.
"""
import json
import os
import shutil
import subprocess

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import app as webapp  # noqa: E402  (web/app.py)
import reader as rd  # noqa: E402
import signals  # noqa: E402
from store import Store  # noqa: E402

STATIC = os.path.join(ROOT, "web", "static")
ALERTS_JS = os.path.join(STATIC, "alerts.js")
TILESTUDIO_JS = os.path.join(STATIC, "tilestudio.js")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def read(path):
    with open(path) as f:
        return f.read()


def run_node(script):
    r = subprocess.run(["node", "-e", script, ALERTS_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── the engine, from node ──

@needs_node
def test_alerts_js_parses():
    r = subprocess.run(["node", "--check", ALERTS_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


ENGINE_HARNESS = """
  globalThis.window = globalThis; require(process.argv[1]);
  const A = window.Alerts, e = A.createEngine();
  const ctx = { signals: { soc: {min: 0, max: 100, item: 'lbc01'}, door_any: {kind: 'bool', item: 'p60D'},
                           'temps_f.2': {min: 20, max: 130, item: 'lbc04'} },
                items: { lbc01: {period: 0}, p60D: {period: 5}, lbc04: {period: 30} }, staleAfter: 90 };
  const out = [];
  const run = (rules, rec, now) => { const r = e.evaluate(rules, rec, now, ctx);
                                     out.push([r.fired.map(x => x.id), r.breached]); };
"""


@needs_node
def test_engine_fires_on_crossing_repeats_and_rearms_with_hysteresis():
    out = run_node(ENGINE_HARNESS + """
      const rules = [{id: 'soc:soc', signal: 'soc', min: 20, repeat: 10}];
      const ok = (soc, age) => ({soc, status: 'ok', item_age: {lbc01: age == null ? 1 : age}});
      run(rules, ok(25), 0);            // inside
      run(rules, ok(19), 1000);         // crossed below: fire
      run(rules, ok(18), 2000);         // still below, repeat not due
      run(rules, ok(18), 11000);        // repeat due (10 s)
      run(rules, ok(20.5), 12000);      // back over 20 but inside the 1 % band: still breached, no nag
      run(rules, ok(21), 13000);        // re-armed
      run(rules, ok(19), 14000);        // fires again
      console.log(JSON.stringify(out));
    """)
    assert out == [[[], []], [["soc:soc"], ["soc:soc"]], [[], ["soc:soc"]], [["soc:soc"], ["soc:soc"]],
                   [[], ["soc:soc"]], [[], []], [["soc:soc"], ["soc:soc"]]]


@needs_node
def test_engine_freezes_on_stale_or_not_ok_data_and_ignores_nulls():
    out = run_node(ENGINE_HARNESS + """
      const rules = [{id: 'soc:soc', signal: 'soc', min: 20, repeat: 10}];
      run(rules, {soc: 19, status: 'ok', item_age: {lbc01: 1}}, 0);             // fire
      run(rules, {soc: 19, status: 'reconnecting', item_age: {lbc01: 1}}, 20000); // frozen: no nag
      run(rules, {soc: 19, status: 'ok', item_age: {lbc01: 500}}, 30000);        // stale: frozen
      run(rules, {soc: 19, status: 'ok', item_age: {}}, 31000);                  // no age for the item: frozen
      run(rules, {soc: null, status: 'ok', item_age: {lbc01: 1}}, 32000);        // null: frozen
      run(rules, {soc: 25, status: 'asleep', item_age: {lbc01: 1}}, 33000);      // inside but asleep: still breached
      run(rules, {soc: 25, status: 'ok', item_age: {lbc01: 1}}, 34000);          // re-arm
      run(rules, {soc: 19}, 35000);                                              // the cockpit's record: no status, no ages
      console.log(JSON.stringify(out));
    """)
    assert out == [[["soc:soc"], ["soc:soc"]], [[], ["soc:soc"]], [[], ["soc:soc"]], [[], ["soc:soc"]],
                   [[], ["soc:soc"]], [[], ["soc:soc"]], [[], []], [["soc:soc"], ["soc:soc"]]]


@needs_node
def test_engine_handles_bools_dotted_keys_max_and_disabled_rules():
    out = run_node(ENGINE_HARNESS + """
      const rules = [{id: 'body:door_any', signal: 'door_any', when: 'on'},
                     {id: 'temps:temps_f.2', signal: 'temps_f.2', max: 100, repeat: 0},
                     {id: 'off:soc', signal: 'soc', min: 50, enabled: false}];
      run(rules, {door_any: false, temps_f: [80, 80, 90, 80], soc: 10}, 0);
      run(rules, {door_any: true, temps_f: [80, 80, 101, 80], soc: 10}, 1000);   // both fire; disabled never does
      run(rules, {door_any: true, temps_f: [80, 80, 105, 80], soc: 10}, 60000);  // repeat 0: once only
      run(rules, {door_any: false, temps_f: [80, 80, 99.5, 80], soc: 10}, 61000); // door re-arms at once; temp inside band (h = 1.1)
      run(rules, {door_any: false, temps_f: [80, 80, 98, 80], soc: 10}, 62000);  // temp re-armed
      e.reset('body:door_any');
      run(rules, {door_any: true, temps_f: [80, 80, 98, 80], soc: 10}, 63000);   // fresh state fires again
      console.log(JSON.stringify(out));
    """)
    assert out == [[[], []], [["body:door_any", "temps:temps_f.2"], ["body:door_any", "temps:temps_f.2"]],
                   [[], ["body:door_any", "temps:temps_f.2"]], [[], ["temps:temps_f.2"]], [[], []],
                   [["body:door_any"], ["body:door_any"]]]


@needs_node
def test_hysteresis_and_patterns():
    out = run_node("""
      globalThis.window = globalThis; require(process.argv[1]);
      const A = window.Alerts;
      console.log(JSON.stringify({
        soc: A.hysteresis({min: 0, max: 100}, {min: 20}),
        lv: +A.hysteresis({min: 10, max: 15}, {max: 14.5}).toFixed(3),
        close: A.hysteresis({min: 0, max: 100}, {min: 20, max: 21}),
        none: A.hysteresis({kind: 'bool'}, {when: 'on'}),
        patterns: Object.keys(A.PATTERNS),
        lengths: Object.values(A.PATTERNS).map(p => p.notes.reduce((a, n) => a + n[1], 0)),
        muted: A.muted(),
        rep: [A.REPEAT, A.repeatSeconds(undefined), A.repeatSeconds(''), A.repeatSeconds(0), A.repeatSeconds(30.4), A.repeatSeconds(999)],
      }));
    """)
    assert out["soc"] == 1 and out["lv"] == 0.05 and out["close"] == 0.5 and out["none"] == 0
    assert out["rep"] == [{"min": 1, "max": 60, "dflt": 10}, 10, 10, 1, 30, 60], "slider range 1–60 s, default 10"
    assert out["patterns"] == ["low", "high", "chirp", "triple"]
    assert all(0 < ms <= 1000 for ms in out["lengths"])
    assert out["muted"] is False, "no localStorage → not muted"


# ── source seams ──

def test_engine_has_no_network_and_exports_window_alerts():
    src = read(ALERTS_JS)
    assert "fetch(" not in src and "'/api/" not in src and '"/api/' not in src
    assert "window.Alerts = {" in src
    assert "webkitAudioContext" in src and "createOscillator" in src
    assert "'pointerdown', 'keydown'" in src, "unlocks the AudioContext on the first gesture"


def test_tilestudio_menu_binds_thresholds_on_change_not_input():
    src = read(TILESTUDIO_JS)
    menu = src[src.index("function openTileMenu"):src.index("// ── header controls")]
    assert 'id="tm-alerts"' in menu and "bindAlertRows(m, t)" in menu
    rows = src[src.index("function bindAlertRows"):src.index("document.addEventListener('click'")]
    assert ".al-min, .al-max, .al-when, .al-tone, .al-rep').forEach(el => el.addEventListener('change'" in rows
    assert rows.count("addEventListener('input'") == 1 and "rep.addEventListener('input'" in rows, \
        "only the slider's live label listens to input; thresholds never do"
    menu = src[src.index("function alertRows"):src.index("function bindAlertRows")]
    assert 'type="range" class="al-rep" min="${Alerts.REPEAT.min}" max="${Alerts.REPEAT.max}"' in menu
    assert "flashCard(c)" in src[src.index("function runAlerts"):src.index("function refreshBell")], "every fire flashes the card"
    assert "openTileMenu(" not in rows, "an alert edit must not rebuild the menu under the cursor"
    assert "runAlerts(data)" in src[src.index("window.TileStudio = {"):]


def test_pages_load_the_engine_before_tile_studio():
    for page in ("index.html", "sim.html"):
        src = read(os.path.join(ROOT, "web", "templates", page))
        assert src.index('src="/static/alerts.js"') < src.index('src="/static/tilestudio.js"'), page
    assert 'id="alerts-bell"' in read(os.path.join(ROOT, "web", "templates", "index.html"))
    css = read(os.path.join(STATIC, "tilestudio.css"))
    assert ".card.alerting" in css and ".tile-menu .al-row" in css
    assert "@keyframes alert-flash" in css and ".card.alert-flash { animation: alert-flash" in css


# ── which values a tile offers ──

def test_tile_signals_declared_or_derived():
    leaf = rd.VEHICLE if rd.VEHICLE.NAME == "leaf_ze0" else None
    rd.set_vehicle("leaf_ze0")
    try:
        ts = signals.tile_signals(rd.TILES)
        assert ts["soc"] == ["soc", "pack_v"]
        assert "lv_volts" in ts["health"] and "cell_spread" in ts["cells"]
        assert all(k in signals.SIGNALS for keys in ts.values() for k in keys)
        derived = signals.tile_signals([{"id": "x", "items": ["p421", "p60D"]}])["x"]
        assert "door_any" in derived and "headlights" in derived
        assert "gear" not in derived, "text signals cannot be thresholded"
    finally:
        if leaf is None:
            rd.set_vehicle(rd.VEHICLE.NAME)


# ── the API and the stores ──

@pytest.fixture
def api(tmp_path, monkeypatch):
    rd.set_vehicle("leaf_ze0")
    monkeypatch.setattr(webapp, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(webapp, "DEMO", None)
    for attr, name in (("STATE_FILE", "state.json"), ("TILES_FILE", "tiles.json"),
                       ("CALIB_FILE", "calibration.json"), ("LAYOUTS_FILE", "layouts.json"),
                       ("SIM_TILES_FILE", "sim_tiles.json"), ("PAUSE_FILE", "reader.pause")):
        monkeypatch.setattr(rd, attr, str(tmp_path / name))
    store = Store(str(tmp_path / "api.db"))
    monkeypatch.setattr(webapp, "store", lambda: store)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c
    store.close()
    rd.set_vehicle("leaf_ze0")


def test_api_signals_serves_tile_signals(api):
    body = api.get("/api/signals").get_json()
    assert body["tile_signals"]["soc"] == ["soc", "pack_v"]
    rd.set_vehicle("lancer_2009")
    try:
        assert api.get("/api/signals").get_json()["tile_signals"] == {}
    finally:
        rd.set_vehicle("leaf_ze0")


RULES = [{"signal": "soc", "min": "20", "repeat": "30", "tone": "low"},
         {"signal": "door_any", "when": "on"},                       # no repeat: the default
         {"signal": "nope", "min": 1},            # unknown signal: dropped
         {"signal": "soc"},                        # no threshold: dropped
         {"signal": "lv_volts", "min": "abc", "max": 14.8, "junk": 1, "enabled": False, "repeat": 999}]  # clamped


def test_tiles_put_cleans_and_round_trips_alert_rules(api):
    body = {"tiles": [{"id": "soc", "enabled": True, "opts": {"alerts": RULES, "color": "soc"}},
                      {"id": "u_lv", "kind": "signal", "signal": "lv_volts", "opts": {"alerts": [RULES[4]]}}]}
    out = {t["id"]: t for t in api.put("/api/tiles", json=body).get_json()["tiles"]}
    assert out["soc"]["opts"]["color"] == "soc"
    assert out["soc"]["opts"]["alerts"] == [
        {"signal": "soc", "min": 20.0, "max": None, "tone": "low", "repeat": 30, "enabled": True},
        {"signal": "door_any", "min": None, "max": None, "when": "on", "repeat": 10, "enabled": True},
        {"signal": "lv_volts", "min": None, "max": 14.8, "repeat": 60, "enabled": False}]
    assert out["u_lv"]["opts"]["alerts"] == [{"signal": "lv_volts", "min": None, "max": 14.8, "repeat": 60, "enabled": False}]
    again = {t["id"]: t for t in api.get("/api/tiles").get_json()["tiles"]}
    assert again["soc"]["opts"] == out["soc"]["opts"]
    # an empty or all-junk list leaves no key behind
    out = api.put("/api/tiles", json={"tiles": [{"id": "soc", "opts": {"alerts": [RULES[2]]}}]}).get_json()["tiles"]
    assert "alerts" not in out[0].get("opts", {})


def test_alert_rules_survive_saved_layouts_and_the_cockpit_store(api):
    body = {"tiles": [{"id": "soc", "opts": {"alerts": [RULES[0]]}}]}
    api.put("/api/tiles", json=body)
    api.put("/api/layouts/quiet", json=api.get("/api/tiles").get_json())
    api.put("/api/tiles", json={"tiles": []})
    assert "alerts" not in api.get("/api/tiles").get_json()["tiles"][0].get("opts", {})
    api.post("/api/layouts/quiet/load")
    assert api.get("/api/tiles").get_json()["tiles"][0]["opts"]["alerts"][0]["min"] == 20.0
    sim = api.put("/api/sim/tiles", json={"tiles": [{"id": "vehicle", "opts": {"alerts": RULES[:3]}}]}).get_json()
    assert [r["signal"] for r in sim["tiles"][0]["opts"]["alerts"]] == ["soc", "door_any"]
