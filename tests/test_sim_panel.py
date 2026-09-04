# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The simulator's pages: the cockpit and the control API's fallback panel.

Two pages talk to the control API from a browser:

  * `web/templates/sim.html` + `web/static/sim.js` — the cockpit, served by
    the dashboard at `GET /sim`, built on the dashboard's tile engine and its
    four styled tiles (tests/test_sim_page.py covers the Flask side);
  * `simulator/panel.html` — the API's own landing page, a minimal fallback
    for `hakake_sim.py --pty` runs: the launch commands, the endpoints, and a
    link to the cockpit when told where the dashboard is.

The one architectural rule is the same for all three files and is the only
thing worth testing hard: **every control is generated from `sim.knob_schema()`
and no file contains a knob list of its own**, so the Leaf's sixty-odd knobs
and the Lancer's twenty-eight render from the same code, and so will the next
profile's. The interactive skin (a TEMP button has to know it moves the
setpoint; a door shape has to know which knob it opens) may name a knob, and
the allow-list below says which and why; the rule keeps applying to the real
page, not just to the fallback.

Everything else here is about honesty — neither page may be mistaken for the
real dashboard — and about not dragging in a fourth runtime dependency or a
CDN. This project ships in a car with no signal.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hakake_sim                                                # noqa: E402
from hakake_sim import PANEL_FILE, panel_html, schema_defaults   # noqa: E402
from sim_stub import make_sim as make_stub                       # noqa: E402
from simulator import KNOBS, make_sim                            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_HTML_FILE = os.path.join(ROOT, "web", "templates", "sim.html")
SIM_JS_FILE = os.path.join(ROOT, "web", "static", "sim.js")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


PANEL = panel_html()
SIM_HTML = read(SIM_HTML_FILE)
SIM_JS = read(SIM_JS_FILE)
PAGES = {"panel.html": PANEL, "sim.html": SIM_HTML, "sim.js": SIM_JS}


@pytest.fixture(params=["core", "stub"])
def api(request):
    """The control API over the real core AND over the contract stand-in.

    The stub's schema is deliberately poorer — it has no `category`, and calls
    its text type `str` — so serving the pages against it proves they are
    driven by the contract rather than by the core's particulars.
    """
    sim = (make_sim(vehicle="leaf_ze0", seed=1) if request.param == "core"
           else make_stub(vehicle="leaf_ze0", seed=1))
    httpd = hakake_sim.serve_control(sim, port=0, log=lambda *a: None)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode()

    call.sim = sim
    call.kind = request.param
    try:
        yield call
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── the fallback panel is served, and it is a page ───────────────────────

@pytest.mark.parametrize("path", ["/", "/panel", "/sim/panel"])
def test_the_panel_is_served_as_html(api, path):
    code, ctype, body = api("GET", path)
    assert code == 200
    assert ctype.startswith("text/html")
    assert body.lstrip().lower().startswith("<!doctype html")
    assert "</html>" in body


def test_the_json_api_is_untouched_by_the_panel_living_at_the_root(api):
    """`/` used to answer JSON. `/sim` and `/sim/info` still do, so nothing
    that was scripted against the API cares that a page moved in."""
    for path in ("/sim", "/sim/info"):
        code, ctype, body = api("GET", path)
        assert code == 200 and ctype.startswith("application/json")
        assert json.loads(body)["simulated"] is True


def test_a_missing_panel_file_still_answers_a_page_not_a_traceback(monkeypatch):
    monkeypatch.setattr(hakake_sim, "PANEL_FILE", "/nonexistent/panel.html")
    out = hakake_sim.panel_html()
    assert "SIMULATOR" in out and "/sim/schema" in out


def test_the_fallback_panel_says_how_to_launch_and_where_the_api_is():
    """`--pty` users land here. They need the two commands, the endpoints,
    and — given `?dashboard=` — a link to the cockpit pointed at this API."""
    assert "python web/app.py --adapter sim --port 5055" in PANEL
    assert "python hakake_sim.py --pty --scenario drive --launch-dashboard" in PANEL
    for ep in ("/sim/schema", "/sim/state", "/sim/record", "/sim/knobs", "/sim/scenarios"):
        assert f'href="{ep}"' in PANEL, ep
    assert "?control=" in PANEL and "dashboard" in PANEL
    assert "curl" in PANEL, "every knob must stay reachable from a shell"
    assert "suggestions" in PANEL


# ── the rule: no knob list in any page ───────────────────────────────────

# A page may name a knob for exactly three reasons, and no others:
#
#   1. the interactive skin drives that knob directly: the climate head unit
#      (setpoint, fan, A/C, system), the body tile's door / lamp / lock click
#      targets and button strip, the shifter, the vehicle tile's state and
#      parking-brake toggles, the time card's clock-speed control;
#   2. the emulated cluster or head unit READS a record field that happens to
#      share a knob's name — the skin is feature-detected, and reading a
#      named gauge is the whole point of drawing one;
#   3. a small annotation table adds an honest "this project cannot read
#      that" badge, or the prose explains the clock.
#
# The generated control list itself must know none of them. Handlers for (1)
# install only when the schema carries the knob, so a profile without it
# simply never sees the control.
ALLOWED_KNOB_MENTIONS = {
    # 1 — driven by the skin
    "hvac_on", "hvac_ac_on", "hvac_fan_speed", "hvac_setpoint_f",
    "headlights", "high_beam", "parking_lights", "fog_lights", "turn_signal",
    "locked", "brake_pct", "gear", "start_state", "handbrake", "clock_scale",
    # 2 — record fields the cluster and head unit display
    "soc", "soh", "speed_mph", "current_a", "units_miles", "odometer_mi",
    "pack_temp_c", "cabin_temp_c", "evap_c", "ambient_c", "capacity_ah",
    "charging", "charger",
    # 3 — annotations and prose
    "heater_level", "sunload", "accel_pedal_pct",
}
# How much of the model a page may spell out at all. The first panel drew a
# cluster that only read gauges and sat under a third; the cockpit also makes
# the reused tiles interactive (six body knobs, the shifter, two vehicle
# toggles) and drives the clock, which is what lifts it to two fifths. Every
# name is still individually justified above; this bound only stops the
# allow-list from quietly becoming the list.
MENTION_BOUND = 0.4


def mentions(text, names):
    return {n for n in names if re.search(r"\b" + re.escape(n) + r"\b", text)}


@pytest.mark.parametrize("name", list(PAGES))
def test_the_page_hardcodes_no_knob_list(name):
    """The load-bearing test. If someone pastes a knob list into a page, the
    Lancer stops rendering and nobody notices until they plug in the Lancer."""
    leaf = set(KNOBS["leaf_ze0"])
    mentioned = mentions(PAGES[name], leaf)
    stray = mentioned - ALLOWED_KNOB_MENTIONS
    assert not stray, (
        f"these knob names are written into {name} and are not in the "
        f"documented allow-list: {sorted(stray)}. Drive them from the schema.")
    assert len(mentioned) < len(leaf) * MENTION_BOUND, \
        f"too much of the model is spelled out in {name}: {sorted(mentioned)}"


@pytest.mark.parametrize("name", list(PAGES))
def test_no_fault_knob_is_named_in_the_page(name):
    """Faults are the part most likely to be special-cased by hand. They are
    grouped by the schema's own `category`, so none of their names appear."""
    for knob in KNOBS["leaf_ze0"]:
        if knob.startswith("fault."):
            assert knob not in PAGES[name], f"{knob} is hardcoded in {name}"


@pytest.mark.parametrize("name", list(PAGES))
def test_no_lancer_knob_is_named_in_the_page(name):
    """A second profile is the cheapest proof the first was not special-cased."""
    leaf = set(KNOBS["leaf_ze0"])
    for knob in KNOBS["lancer_2009"]:
        if knob in leaf:
            continue                    # shared names are covered by the test above
        assert not re.search(r"\b" + re.escape(knob) + r"\b", PAGES[name]), \
            f"{knob} is hardcoded in {name}"


def test_the_cockpit_reads_categories_and_labels_from_the_schema_not_a_table():
    """Cards are grouped by `spec.category`, titled by `spec.label`; the °F/°C
    pairing keys on `spec.unit`. None of that is a list of names."""
    assert "spec.category" in SIM_JS
    assert "spec.label" in SIM_JS
    assert "spec.unit" in SIM_JS and "'°C'" in SIM_JS and "'°F'" in SIM_JS
    assert "/sim/schema" in SIM_JS


def test_every_knob_of_every_profile_carries_a_category_for_the_ui():
    """The pages group by `category`. If the schema stops supplying one for a
    new knob, the grouping silently degrades — so require it at the source."""
    for vehicle, kset in KNOBS.items():
        for name, spec in kset.schema().items():
            assert spec.get("category"), f"{vehicle}:{name} has no category"
            assert spec["category"] != "other", \
                f"{vehicle}:{name} fell through to 'other' — give its section a K.group()"
        assert "faults" in kset.categories(), vehicle


def test_faults_are_their_own_category_everywhere():
    for vehicle, kset in KNOBS.items():
        for name, knob in kset.items():
            if name.startswith("fault."):
                assert knob.category == "faults", f"{vehicle}:{name}"


# ── honesty ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["panel.html", "sim.html"])
def test_the_page_says_simulator_loudly_and_early(name):
    page = PAGES[name]
    head = page[:page.index("</head>")] + page[page.index("<body"):][:4000]
    assert "SIMULATOR" in head
    assert "simulated" in page.lower() and "generated" in page.lower()
    assert "No adapter. No car." in page


@pytest.mark.parametrize("name", ["panel.html", "sim.js"])
def test_the_page_disclaims_the_manufacturer(name):
    """The cluster is an original drawing and says so. The line lives in
    sim.js, next to the drawing, so a profile that never draws the Leaf skin
    never mentions the make (test_sim_page.py checks the Lancer render)."""
    assert "not affiliated with or endorsed by Nissan" in PAGES[name]


@pytest.mark.parametrize("name", list(PAGES))
def test_the_page_embeds_no_third_party_image_and_reaches_no_external_origin(name):
    # nothing may be fetched or embedded: no external origin, no data: blobs
    page = PAGES[name]
    assert "http://" not in page.replace("http://127.0.0.1", "")
    assert "https://" not in page
    assert "data:image" not in page
    assert "<img" not in page
    for host in ("cdn.", "cdnjs", "jsdelivr", "unpkg", "esm.sh", "googleapis"):
        assert host not in page.lower(), f"{name} reaches for {host}"


def test_the_fallback_panel_vendors_everything_and_pulls_in_no_dependency():
    """Three runtime dependencies, and a landing page is not a reason for a
    fourth. No <script src>, no <link rel=stylesheet>, no import."""
    assert not re.search(r"<script[^>]+src=", PANEL)
    assert not re.search(r"<link[^>]+stylesheet", PANEL)
    assert not re.search(r"\bimport\s+[\w{*]", PANEL)


def test_the_cockpit_loads_only_the_dashboards_own_static_files():
    """The cockpit reuses the vendored gridstack, tilestudio.js and tiles.js —
    all from /static/, never a CDN — and sim.js imports nothing."""
    srcs = re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', SIM_HTML)
    assert srcs, "the cockpit must link the shared engine"
    for s in srcs:
        assert s.startswith("/static/"), s
    for lib in ("vendor/gridstack-all.js", "tilestudio.js", "tiles.js", "sim.js"):
        assert any(s.endswith(lib) for s in srcs), lib
    assert not re.search(r"\bimport\s+[\w{*]", SIM_JS)
    assert "require(" not in SIM_JS


def test_controls_the_project_cannot_read_are_drawn_inert_and_say_why():
    """docs/SIGNALS.md records vent MODE, AUTO and fresh/recirc as walked and
    unmoved. The head unit draws them and refuses to pretend they work — which
    is the difference between an honest map and a mock-up."""
    assert "inert" in SIM_JS and "not readable" in SIM_JS
    for label in ("AUTO", "MODE", "FRESH / RECIRC", "DEFROST", "DEFOG"):
        assert label in SIM_JS
    assert "80 01 80 00" in SIM_JS, "cite the actual documented negative"
    assert "docs/SIGNALS.md" in SIM_JS
    assert "0x180" in SIM_JS, "say that the throttle knob has no dashboard tile"


def test_lamps_the_model_cannot_drive_are_drawn_dim_and_say_so():
    """`lamps_unmodelled` is drawn, dimmed, with the reason — never faked lit."""
    assert "lamps_unmodelled" in SIM_JS
    assert "no driver in the model" in SIM_JS


def test_the_panel_is_not_in_the_dashboards_static_directory():
    """web/static belongs to the real dashboard. The API's own page in there
    is one wrong URL away from being mistaken for it."""
    assert os.path.basename(os.path.dirname(PANEL_FILE)) == "simulator"
    assert not os.path.exists(os.path.join(
        os.path.dirname(os.path.dirname(PANEL_FILE)), "web", "static", "panel.html"))


# ── the endpoints the pages drive ────────────────────────────────────────

def test_the_panel_can_list_scenarios(api):
    code, ctype, body = api("GET", "/sim/scenarios")
    assert code == 200
    doc = json.loads(body)
    assert doc["simulated"] is True and isinstance(doc["scenarios"], list)
    if api.kind == "core":
        assert "charge" in doc["scenarios"]


def test_reset_puts_every_knob_back_to_its_schema_default(api):
    api("POST", "/sim/knobs", {"soc": 3})
    code, _, body = api("POST", "/sim/reset", {})
    assert code == 200, body
    assert json.loads(body)["reset"] is True
    _, _, knobs = api("GET", "/sim/knobs")
    now = json.loads(knobs)["knobs"]
    for name, want in schema_defaults(api.sim).items():
        assert now[name] == want or now[name] == pytest.approx(want), name


def test_reset_needs_no_knowledge_of_any_particular_vehicle():
    """Same code path, a different profile, nothing added."""
    sim = make_sim(vehicle="lancer_2009", seed=1)
    sim.set(rpm=4200.0, mil_on=True)
    applied, err = hakake_sim.apply_knobs(sim, schema_defaults(sim))
    assert err is None, err
    assert sim.get_knobs()["rpm"] == 719.0 and sim.get_knobs()["mil_on"] is False


def test_a_bad_value_from_the_panel_comes_back_with_a_suggestion(api):
    """What the pages put in front of the user when a field is wrong: the
    API's own near match, not a silent failure."""
    code, _, body = api("POST", "/sim/knobs", {"sock": 20})
    assert code == 400
    doc = json.loads(body)
    assert "soc" in doc["suggestions"]["sock"]


def test_the_cockpit_surfaces_those_suggestions_as_clickable_names():
    assert "suggestions" in SIM_JS and "flashKnob" in SIM_JS


def test_the_404_lists_the_panel_so_it_can_be_found(api):
    code, _, body = api("GET", "/sim/nowhere")
    assert code == 404
    assert "/panel" in json.loads(body)["endpoints"]
