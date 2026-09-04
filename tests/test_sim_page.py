# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The simulator cockpit is a dashboard page: `GET /sim`.

It is served by app.py, reuses the dashboard's tile engine and its four
styled tiles, keeps its own arrangement in web/sim_tiles.json through
`GET/PUT /api/sim/tiles`, and drives the simulator's control API from the
browser. These tests pin the Flask side and the seams the page depends on:

  * `/sim` always renders — in a simulated run it carries the control URL,
    otherwise it carries the two launch commands;
  * the partials are included only for a profile that declares those tiles,
    and the Lancer render names no manufacturer;
  * nothing external is referenced;
  * the layout store round-trips a layout, rejects junk, and never filters
    ids (that is the dashboard store's job, and it would drop this page's);
  * the dashboard header carries a SIMULATED badge that links here;
  * the knobs the interactive skin drives, and the record keys it reads,
    are what the model publishes.
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
from simulator import KNOBS, make_sim  # noqa: E402
from store import Store  # noqa: E402

TEMPLATES = os.path.join(ROOT, "web", "templates")
STATIC = os.path.join(ROOT, "web", "static")
SIM_JS = os.path.join(STATIC, "sim.js")
ANCHORS = {"vehicle": 'id="shifter"', "tires": 'id="wheels"', "body": 'id="body-svg"', "climate": 'id="hvac-cabin"'}
CARDS = ("cluster", "climate-head", "time", "state")
LAUNCH = ("python web/app.py --adapter sim --port 5055",
          "python hakake_sim.py --pty --scenario drive --launch-dashboard")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The Flask test client with every file under tmp_path and no control URL."""
    rd.set_vehicle("leaf_ze0")
    monkeypatch.setattr(webapp, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(webapp, "DEMO", None)
    monkeypatch.setattr(webapp, "SIM_CONTROL_URL", None)
    for attr, name in (("STATE_FILE", "state.json"), ("TILES_FILE", "tiles.json"),
                       ("SIM_TILES_FILE", "sim_tiles.json"),
                       ("CALIB_FILE", "calibration.json"), ("LAYOUTS_FILE", "layouts.json"),
                       ("PAUSE_FILE", "reader.pause")):
        monkeypatch.setattr(rd, attr, str(tmp_path / name))
    store = Store(str(tmp_path / "sim_page.db"))
    monkeypatch.setattr(webapp, "store", lambda: store)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c
    store.close()
    rd.set_vehicle("leaf_ze0")


# ── /sim renders, whatever is behind it ──

def test_sim_renders_without_a_simulator_and_says_how_to_launch(client):
    r = client.get("/sim")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "SIMULATOR" in page and "No adapter. No car." in page
    assert "window.SIM_CONTROL_URL = null;" in page
    for cmd in LAUNCH:
        assert cmd in page, cmd
    assert "?control=" in page, "a rig on an odd port can be named in the URL"


def test_sim_carries_the_control_url_the_app_knew_at_startup(client, monkeypatch):
    monkeypatch.setattr(webapp, "SIM_CONTROL_URL", "http://127.0.0.1:8099")
    page = client.get("/sim").get_data(as_text=True)
    assert 'window.SIM_CONTROL_URL = "http://127.0.0.1:8099";' in page


def test_sim_hosts_the_four_tiles_and_its_own_cards_once_each(client):
    page = client.get("/sim").get_data(as_text=True)
    for tile, anchor in ANCHORS.items():
        assert page.count(anchor) == 1, tile
        assert page.count(f'data-tile="{tile}"') == 1, tile
    for card in CARDS:
        assert page.count(f'data-tile="{card}"') == 1, card
    assert 'id="simtiles"' in page and 'id="knob-card-tpl"' in page
    assert 'href="/"' in page, "the hazard bar links back to the dashboard"
    assert "none (free-running)" in page


def test_sim_loads_the_shared_engine_in_order_and_nothing_external(client):
    page = client.get("/sim").get_data(as_text=True)
    srcs = re.findall(r'<script src="([^"]+)"', page)
    assert srcs == ["/static/vendor/gridstack-all.js", "/static/tilestudio.js",
                    "/static/tiles.js", "/static/sim.js"]
    links = re.findall(r'<link rel="stylesheet" href="([^"]+)"', page)
    assert links[:4] == ["/static/vendor/gridstack.min.css", "/static/tilestudio.css",
                         "/static/hakake.css", "/static/tiles.css"], "the dashboard's cascade order"
    assert links[-1] == "/static/sim.css"
    for bad in ("<img", "data:image", "https://"):
        assert bad not in page, bad
    assert "http://" not in page.replace("http://127.0.0.1", "")


def test_sim_includes_partials_only_for_a_profile_that_declares_them(client):
    """The dashboard's own rule, applied server-side: the Lancer declares no
    built-in tiles, so its cockpit carries no Leaf markup — and no make."""
    rd.set_vehicle("lancer_2009")
    try:
        r = client.get("/sim")
        assert r.status_code == 200
        page = r.get_data(as_text=True)
        for tile, anchor in ANCHORS.items():
            assert anchor not in page, tile
            assert f'data-tile="{tile}"' not in page, tile
        for card in CARDS:                       # the page's own cards stay; sim.js drops what the record cannot drive
            assert f'data-tile="{card}"' in page, card
        assert "Nissan" not in page
        assert "SIMULATOR" in page and 'id="knob-card-tpl"' in page
    finally:
        rd.set_vehicle("leaf_ze0")


def test_sim_ctx_names_the_control_url_and_the_declared_tiles(monkeypatch):
    rd.set_vehicle("leaf_ze0")
    monkeypatch.setattr(webapp, "SIM_CONTROL_URL", None)
    ctx = webapp.sim_ctx()
    assert ctx["control_url"] is None
    assert set(ANCHORS) <= set(ctx["tiles"])
    rd.set_vehicle("lancer_2009")
    try:
        assert webapp.sim_ctx()["tiles"] == []
    finally:
        rd.set_vehicle("leaf_ze0")


# ── the layout store ──

def test_sim_tiles_round_trips_a_layout(client, tmp_path):
    assert client.get("/api/sim/tiles").get_json() == {"tiles": []}
    layout = {"tiles": [
        {"id": "cluster", "kind": "builtin", "enabled": True, "span": 8, "h": 9, "x": 0, "y": 0},
        {"id": "knobs-faults", "kind": "builtin", "enabled": False, "span": 4},
        {"id": "u123", "kind": "signal", "signal": "soc", "type": "ring", "span": 3, "opts": {"color": "soc"}, "title": "SOC"},
    ]}
    r = client.put("/api/sim/tiles", data=json.dumps(layout), content_type="application/json")
    assert r.status_code == 200
    got = r.get_json()["tiles"]
    assert [t["id"] for t in got] == ["cluster", "knobs-faults", "u123"], "no id is filtered"
    assert got[0] == layout["tiles"][0]
    assert got[1]["enabled"] is False
    assert got[2]["opts"] == {"color": "soc"} and got[2]["title"] == "SOC"
    assert client.get("/api/sim/tiles").get_json()["tiles"] == got
    assert os.path.exists(tmp_path / "sim_tiles.json")
    assert not os.path.exists(tmp_path / "tiles.json"), "never the dashboard's store"


@pytest.mark.parametrize("body", ["[]", '"x"', '{"tiles": "x"}', '{"tiles": {}}', "not json"])
def test_sim_tiles_rejects_junk(client, body):
    r = client.put("/api/sim/tiles", data=body, content_type="application/json")
    assert r.status_code == 400
    assert client.get("/api/sim/tiles").get_json() == {"tiles": []}


def test_sim_tiles_enforces_shape_only(client):
    r = client.put("/api/sim/tiles", data=json.dumps({"tiles": [
        "nope", {"id": 7}, {"id": ""},
        {"id": "a", "x": "abc", "y": 3.9, "span": 99, "h": "2", "enabled": "yes", "opts": "bad", "title": 5},
        {"id": "a", "span": 3},                      # duplicate id: first wins
    ]}), content_type="application/json")
    assert r.status_code == 200
    got = r.get_json()["tiles"]
    assert len(got) == 1
    t = got[0]
    assert t["id"] == "a" and "x" not in t and t["y"] == 3 and t["span"] == 12 and t["h"] == 2
    assert t["enabled"] is True and t["opts"] == {} and "title" not in t


def test_sim_tiles_store_tolerates_a_missing_or_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "SIM_TILES_FILE", str(tmp_path / "sim_tiles.json"))
    assert rd.load_sim_tiles() == {"tiles": []}
    (tmp_path / "sim_tiles.json").write_text("{not json")
    assert rd.load_sim_tiles() == {"tiles": []}
    (tmp_path / "sim_tiles.json").write_text('{"tiles": 5}')
    assert rd.load_sim_tiles() == {"tiles": []}


def test_sim_tiles_file_is_gitignored():
    git = shutil.which("git")
    if not git or not os.path.isdir(os.path.join(ROOT, ".git")):
        pytest.skip("not a git checkout")
    p = subprocess.run([git, "check-ignore", "-q", "web/sim_tiles.json"], cwd=ROOT)
    assert p.returncode == 0, "web/sim_tiles.json is personal layout and must stay out of git"


# ── the dashboard links here ──

def test_dashboard_header_carries_a_hidden_simulated_badge_linking_to_sim(client):
    page = client.get("/").get_data(as_text=True)
    m = re.search(r'<a class="sim-badge" id="sim-badge" href="/sim"[^>]*>SIMULATED</a>', page)
    assert m, "the badge"
    assert ".sim-badge {" in page and "display: none" in page[page.index(".sim-badge {"):][:200]
    assert "classList.toggle('on', !!data.simulated)" in page, "shown only when the record says simulated"


# ── the page's assumptions about the model, pinned ──

def test_sim_js_boots_the_shared_engine_against_its_own_store_and_no_layouts_ui():
    js = read(SIM_JS)
    assert "TileStudio.init({" in js
    assert "gridId: 'simtiles'" in js
    assert "tiles: '/api/sim/tiles'" in js
    assert "layouts: null" in js
    assert "discover: true" in js
    assert "lsKey: 'hakake-sim-tiles-v1'" in js
    assert "/api/tiles'" not in js, "never the dashboard's store"


def test_sim_js_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    p = subprocess.run([node, "--check", SIM_JS], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_sim_js_resolves_the_control_url_in_the_documented_order_and_falls_back_to_state():
    js = read(SIM_JS)
    i_q, i_inj, i_st = js.index("get('control')"), js.index("window.SIM_CONTROL_URL"), js.index("sim_control_url")
    assert i_q < i_inj < i_st
    assert "/sim/record" in js and "501" in js and "/sim/state" in js
    assert "sim_seconds" in js, "skip-ahead is clock-independent"
    assert "{ name: sel.value }" in js, "the free-running option posts an empty name"
    assert "location.reload()" in js
    assert "Tiles.fmtTemp" in js, "every temperature goes through the house formatter"
    assert "EDIT_GRACE = 1500" in js


def test_the_knobs_the_skin_drives_exist_with_the_shapes_it_assumes():
    """The interactive skin installs a handler only when a knob exists; this
    pins that, on the Leaf, they do — and with the types the handlers send."""
    s = KNOBS["leaf_ze0"].schema()
    for name in ("headlights", "high_beam", "parking_lights", "fog_lights", "locked",
                 "handbrake", "hvac_on", "hvac_ac_on"):
        assert s[name]["type"] == "bool", name
    assert set(s["gear"]["choices"]) >= {"P", "R", "N", "D", "Eco"}
    assert "off" in s["turn_signal"]["choices"]
    assert s["start_state"]["choices"]
    for name in ("brake_pct", "hvac_fan_speed", "clock_scale", "tpms_fl", "tpms_fr", "tpms_rl", "tpms_rr"):
        assert s[name]["type"] in ("float", "int"), name
    assert s["hvac_setpoint_f"]["unit"] == "°F"
    # every temperature knob declares a temperature unit, or the °F/°C pairing cannot key on it
    for vehicle, kset in KNOBS.items():
        for name, spec in kset.schema().items():
            if re.search(r"(temp|_c|_f)$", name) and spec["type"] in ("float", "int") and "limit" not in name:
                assert spec["unit"] in ("°C", "°F"), f"{vehicle}:{name} unit={spec['unit']!r}"


def test_the_page_mounts_the_power_button_and_sim_js_wires_it():
    """The markup is a mount and nothing else: sim.js draws the button and
    decides whether to draw it at all, so a profile with a key gets none."""
    page = read(os.path.join(TEMPLATES, "sim.html"))
    js = read(SIM_JS)
    assert page.count('id="power-switch"') == 1
    assert "hidden" in page[page.index('id="power-switch"') - 60:page.index('id="power-switch"') + 60]
    assert "POWER" not in page, "the button itself is built in sim.js, not written into the page"
    # drawn only when the schema carries an ignition knob, removed on a 501
    assert "has('start_state')" in js
    assert "/sim/power" in js
    assert "501" in js
    # and the brake, because the brake is what reaches READY on this car
    assert "hold the brake" in js


def test_the_power_button_has_a_state_for_every_ignition_position():
    """Dim OFF, amber ACC, green ON, lit green READY — one per choice the
    model publishes, so a new position could not be drawn as OFF by default."""
    js = read(SIM_JS)
    ring = js[js.index("const POWER_RING"):js.index("function powerState")]
    for state in KNOBS["leaf_ze0"].schema()["start_state"]["choices"]:
        assert state + ":" in ring, state


def test_the_cockpit_shows_the_whole_power_budget_not_just_the_loads():
    """wall -> charge -> loads -> pack, with the house sign explained where a
    reader will meet it."""
    js = read(SIM_JS)
    for bit in ("wall_kw", "charger_kw", "loads_kw", "pack_kw", "regen_kw"):
        assert bit in js, bit
    assert "into the pack" in js, "the sign has to be said, not assumed"


def test_the_record_carries_the_power_budget_the_cockpit_paints():
    sim = make_sim(vehicle="leaf_ze0", knobs={"start_state": "off", "charging": True,
                                              "charger": "l2", "soc": 45.0}, seed=1)
    rec = sim.record()
    assert set(("wall_kw", "charger_kw", "loads_kw", "regen_kw", "pack_kw")) <= set(rec["power"])
    assert rec["power"]["wall_kw"] > rec["power"]["charger_kw"] > rec["power"]["pack_kw"]


def test_the_record_carries_what_the_cluster_and_head_unit_read():
    """Feature detection keys on these; the load table is what makes a
    headlight toggle visible on both pages."""
    sim = make_sim(vehicle="leaf_ze0", seed=1)
    rec = sim.record()
    for k in ("lamps", "lamps_unmodelled", "messages", "loads_w", "soc", "soh", "speed_mph", "speed_kmh",
              "units_miles", "power_kw", "current_a", "temp_avg_c", "temp_avg_f", "range_mi", "range_km",
              "odometer_mi", "odometer_km", "gear", "hvac_ambient_c", "hvac_ambient_f",
              "hvac_target_c", "hvac_target_f", "cabin_temp_c", "cabin_temp_f", "hvac_evap_c", "hvac_evap_f",
              "hvac_on", "hvac_ac_on", "hvac_fan_speed", "hvac_heater_level", "sim_t"):
        assert k in rec, k
    for lamp in ("ready", "turn_left", "turn_right", "hazards", "low_beam", "high_beam", "position", "fog",
                 "parking_brake", "door_ajar", "plug_in", "charge_12v", "ev_system", "power_limit",
                 "low_battery", "tpms", "headlight_warning", "security", "eco", "master_red", "master_yellow"):
        assert lamp in rec["lamps"], lamp
    assert rec["lamps"]["low_beam"] is False and rec["loads_w"]["low_beam"] == 0
    sim.set(headlights=True)
    rec = sim.record()
    assert rec["lamps"]["low_beam"] is True and rec["loads_w"]["low_beam"] == 70
    # the Lancer core has no record(): the page must fall back to state and drop the Leaf skin
    lancer = make_sim(vehicle="lancer_2009", seed=1)
    can = getattr(lancer, "can_record", None)
    assert not callable(getattr(lancer, "record", None)) or (callable(can) and not can())
