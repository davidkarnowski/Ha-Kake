# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The simulator's agent control surface.

The point of a simulator over a recording is that conditions can be changed
*while the dashboard watches*. That makes this HTTP surface a feature, not
scaffolding, and it has to behave the way an agent needs: discoverable
(`/sim/schema`), machine-readable everywhere, and loud-but-recoverable when a
knob name is wrong — a typo must come back as a 400 with near matches, never a
500 with a traceback an agent cannot act on.

Bound to 127.0.0.1 only, always: this thing can put a fault on somebody's
dashboard and it has no authentication.
"""

import json
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import hakake_sim  # noqa: E402
from hakake_sim import parse_knob_args, parse_knob_value, serve_control, suggest  # noqa: E402
from sim_stub import make_sim  # noqa: E402


# ── knob parsing (the CLI half of the surface) ───────────────────────────

@pytest.mark.parametrize("text,want", [
    ("20", 20), ("20.5", 20.5), ("-80", -80),
    ("true", True), ("false", False), ("on", True), ("off", False),
    ("null", None), ("D", "D"), ('"D"', "D"), ("[1, 2]", [1, 2]),
])
def test_knob_values_parse_the_way_they_look(text, want):
    assert parse_knob_value(text) == want


def test_knob_pairs_become_a_dict():
    assert parse_knob_args(["soc=20", "fault.cell_degraded=true", "gear=D"]) == {
        "soc": 20, "fault.cell_degraded": True, "gear": "D"}


@pytest.mark.parametrize("bad", ["soc", "=20", ""])
def test_malformed_knob_arguments_are_rejected(bad):
    with pytest.raises(ValueError, match="name=value"):
        parse_knob_args([bad])


def test_suggestions_come_from_the_live_knob_list():
    sim = make_sim()
    assert "soc" in suggest(sim, "sock")
    assert "fault.cell_degraded" in suggest(sim, "fault.cell_degrade")


# ── the server ───────────────────────────────────────────────────────────

def _serve(sim):
    """The control API over `sim` on an ephemeral loopback port.

    Yields a `call(method, path, body)` -> (status, json) client with `.sim`
    and `.base` on it; tears the server down after.
    """
    httpd = serve_control(sim, port=0, log=lambda *a: None)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    call.sim = sim
    call.base = base
    try:
        yield call
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def api():
    """The control API over the contract STUB, torn down after."""
    yield from _serve(make_sim(vehicle="leaf_ze0", seed=1))


@pytest.fixture
def core_api():
    """The control API over the REAL core — for the clock-dependent checks and
    the optional capabilities (record, clear_scenario) the stub deliberately
    lacks."""
    simulator = pytest.importorskip("simulator")
    yield from _serve(simulator.make_sim(vehicle="leaf_ze0", seed=1))


def test_it_binds_loopback_only(api):
    assert api.base.startswith("http://127.0.0.1:")


# ── the CORS preflight ───────────────────────────────────────────────────
#
# A browser tab on the dashboard is a natural client of this port, so the
# preflight is part of the contract and documented in docs/SIMULATOR.md. It
# regressed once already — a 204 that carried a JSON body, which a browser may
# reject outright, taking the real request with it — so it is pinned here.

CORS_HEADERS = ("Access-Control-Allow-Origin",
                "Access-Control-Allow-Headers",
                "Access-Control-Allow-Methods")


def _raw(base, method, path):
    """One request, returning (status, headers, body-bytes) — the `api` fixture's
    client parses JSON and throws the headers away, and headers are the point."""
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_the_preflight_is_a_204_with_the_cors_headers_and_no_body(api):
    status, headers, body = _raw(api.base, "OPTIONS", "/sim/knobs")
    assert status == 204
    for h in CORS_HEADERS:
        assert headers.get(h), f"preflight is missing {h}"
    assert headers.get("Access-Control-Allow-Origin") == "*"
    assert "OPTIONS" in headers.get("Access-Control-Allow-Methods")
    assert "content-type" in headers.get("Access-Control-Allow-Headers").lower()
    # the regression that already happened once: a 204 must carry nothing
    assert headers.get("Content-Length") == "0"
    assert body == b""
    assert headers.get("Content-Type") is None


def test_an_ordinary_json_response_carries_the_same_origin_header(api):
    """The preflight is worthless if the real request is not allowed too."""
    status, headers, body = _raw(api.base, "GET", "/sim/state")
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") == "*"
    assert headers.get("Content-Type") == "application/json"
    assert json.loads(body.decode())["simulated"] is True


def test_schema_describes_every_knob_well_enough_to_use_blind(api):
    code, body = api("GET", "/sim/schema")
    assert code == 200 and body["simulated"] is True
    knobs = body["knobs"]
    assert "soc" in knobs and "fault.cell_degraded" in knobs
    for name, spec in knobs.items():
        assert spec.get("type"), f"{name} has no type"
        assert "default" in spec, f"{name} has no default"
        assert spec.get("help"), f"{name} has no help — an agent cannot use it"


def test_state_and_knobs_are_readable(api):
    code, body = api("GET", "/sim/state")
    assert code == 200 and "soc" in body["state"]
    code, body = api("GET", "/sim/knobs")
    assert code == 200 and body["knobs"]["soc"] == 80.0


def test_info_says_it_is_simulated_and_lists_the_endpoints(api):
    code, body = api("GET", "/sim/info")
    assert code == 200
    assert body["simulated"] is True and "SIMULATED" in body["warning"]
    assert "schema" in body["endpoints"]


def test_setting_a_knob_takes_effect_immediately(api):
    code, body = api("POST", "/sim/knobs", {"soc": 15})
    assert code == 200 and body["applied"] == {"soc": 15.0}
    assert api("GET", "/sim/state")[1]["state"]["soc"] == 15.0


def test_injecting_a_fault_shows_up_in_the_state_and_the_faults_list(api):
    """A degraded cell is the case a real car cannot be asked to produce on
    demand, which is the whole argument for having a simulator at all."""
    before = api("GET", "/sim/state")[1]["state"]["cell_spread_mv"]
    code, _ = api("POST", "/sim/knobs", {"fault.cell_degraded": True})
    assert code == 200
    assert api("GET", "/sim/state")[1]["state"]["cell_spread_mv"] > before
    assert api("GET", "/sim/faults")[1]["faults"].get("cell_degraded") is True


def test_several_knobs_apply_together(api):
    code, body = api("POST", "/sim/knobs",
                     {"soc": 20, "fault.insulation_low": True, "gear": "D"})
    assert code == 200
    assert set(body["applied"]) == {"soc", "fault.insulation_low", "gear"}


def test_a_bad_knob_name_is_a_400_with_near_matches(api):
    """The failure an agent will actually hit. It must be able to recover from
    the response alone, without reading source."""
    code, body = api("POST", "/sim/knobs", {"sock": 20})
    assert code == 400
    assert "sock" in body["error"]
    assert body["unknown"] == ["sock"]
    assert "soc" in body["suggestions"]["sock"]


def test_a_bad_knob_does_not_half_apply_the_good_ones(api):
    api("POST", "/sim/knobs", {"sock": 20, "soc": 11})
    assert api("GET", "/sim/knobs")[1]["knobs"]["soc"] == 80.0, "all or nothing"


def test_a_bad_body_is_a_400_not_a_500(api):
    req = urllib.request.Request(api.base + "/sim/knobs", data=b"{nope",
                                 method="POST", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400


def test_a_scenario_can_be_loaded_mid_run(api):
    code, body = api("POST", "/sim/scenario", {"name": "drive"})
    assert code == 200 and body["scenario"] == "drive"
    assert api("GET", "/sim/state")[1]["state"]["gear"] == "D"


def test_an_unknown_scenario_is_a_400(api):
    code, body = api("POST", "/sim/scenario", {"name": "teleport"})
    assert code == 400 and "teleport" in body["error"]
    code, body = api("POST", "/sim/scenario", {})
    assert code == 400


def test_the_model_can_be_stepped_by_hand(api):
    api("POST", "/sim/knobs", {"current_a": -100})
    before = api("GET", "/sim/state")[1]["state"]["soc"]
    code, body = api("POST", "/sim/step", {"dt": 600})
    assert code == 200 and body["stepped"] == 600
    assert api("GET", "/sim/state")[1]["state"]["soc"] < before


@pytest.mark.parametrize("dt", [-1, 999999, "soon"])
def test_a_nonsense_step_is_refused(api, dt):
    assert api("POST", "/sim/step", {"dt": dt})[0] == 400


def test_unknown_endpoints_list_the_real_ones(api):
    code, body = api("GET", "/sim/nonsense")
    assert code == 404 and any("/sim/schema" in e for e in body["endpoints"])
    assert api("POST", "/sim/nonsense", {})[0] == 404


def test_health_is_cheap_and_says_simulated(api):
    assert api("GET", "/health") == (200, {"ok": True, "simulated": True})


# ── what the cockpit page needs (simulator v2) ───────────────────────────
#
# Three additions, all written against `getattr` so the plumbing does not
# grow a dependency on the core: /sim/record (optional in the contract),
# {"sim_seconds"} on /sim/step, and {"name": ""} on /sim/scenario.

def test_record_is_a_501_on_a_core_without_record(api):
    """The stub has no record(). The answer is a 501 that says so — never a
    500 — because that is exactly how a Lancer core tells the cockpit page to
    draw knob cards only."""
    code, body = api("GET", "/sim/record")
    assert code == 501
    assert body["simulated"] is True and "record()" in body["error"]


def test_record_speaks_the_dashboards_vocabulary_on_the_real_core(core_api):
    if not hasattr(core_api.sim, "record"):
        pytest.skip("this simulator core has no record() yet (lands with P1B)")
    code, body = core_api("GET", "/sim/record")
    assert code == 200 and body["simulated"] is True
    rec = body["record"]
    assert isinstance(rec, dict) and rec, "record() must return the dashboard record"
    assert rec == core_api.sim.record() or set(rec) == set(core_api.sim.record())


def test_sim_seconds_mean_simulated_seconds_whatever_the_clock_scale(core_api):
    """A 'skip ahead 2 minutes' button must skip two simulated minutes on a
    60x scenario too; dt would skip two hours."""
    assert core_api("POST", "/sim/knobs", {"clock_scale": 60})[0] == 200
    before = core_api("GET", "/sim/state")[1]["state"]["t"]
    code, body = core_api("POST", "/sim/step", {"sim_seconds": 120})
    assert code == 200 and body["sim_seconds"] == 120 and body["simulated"] is True
    assert body["stepped"] == pytest.approx(2.0)         # the real seconds it took
    after = core_api("GET", "/sim/state")[1]["state"]["t"]
    assert after - before == pytest.approx(120, abs=1e-6)


def test_sim_seconds_survive_a_speed_override(core_api):
    """`--speed X` installs an outer multiplier the core divides back out of
    step(); sim_seconds must still land exactly."""
    core_api.sim.set_speed(10, outer=10)
    before = core_api("GET", "/sim/state")[1]["state"]["t"]
    assert core_api("POST", "/sim/step", {"sim_seconds": 50})[0] == 200
    after = core_api("GET", "/sim/state")[1]["state"]["t"]
    assert after - before == pytest.approx(50, abs=1e-6)


def test_dt_still_means_real_seconds_and_the_clock_applies(core_api):
    assert core_api("POST", "/sim/knobs", {"clock_scale": 60})[0] == 200
    before = core_api("GET", "/sim/state")[1]["state"]["t"]
    code, body = core_api("POST", "/sim/step", {"dt": 2})
    assert code == 200 and body["stepped"] == 2
    assert core_api("GET", "/sim/state")[1]["state"]["t"] - before == pytest.approx(120)


def test_sim_seconds_on_a_core_with_no_clock_equal_dt(api):
    """The stub has no time_scale(): it runs real time, so both are the same."""
    before = api("GET", "/sim/state")[1]["state"]["sim_time_s"]
    code, body = api("POST", "/sim/step", {"sim_seconds": 30})
    assert code == 200 and body["stepped"] == 30 and body["time_scale"] == 1.0
    assert api("GET", "/sim/state")[1]["state"]["sim_time_s"] - before == pytest.approx(30)


@pytest.mark.parametrize("bad", [-1, hakake_sim.MAX_SIM_SECONDS + 1, "soon", None])
def test_a_nonsense_sim_seconds_is_refused(api, bad):
    code, body = api("POST", "/sim/step", {"sim_seconds": bad})
    assert code == 400 and "sim_seconds" in body["error"]


def test_dt_and_sim_seconds_together_are_ambiguous(api):
    code, body = api("POST", "/sim/step", {"dt": 1, "sim_seconds": 1})
    assert code == 400 and "not both" in body["error"]


@pytest.mark.parametrize("body", [{"name": ""}, {"name": None}, {"scenario": ""}])
def test_clearing_a_scenario_is_a_400_on_a_core_that_cannot(api, body):
    code, out = api("POST", "/sim/scenario", body)
    assert code == 400 and "clear_scenario" in out["error"]


def test_clearing_a_scenario_on_the_real_core(core_api):
    if not hasattr(core_api.sim, "clear_scenario"):
        pytest.skip("this simulator core has no clear_scenario() yet (lands with P1B)")
    assert core_api("POST", "/sim/scenario", {"name": "drive"})[0] == 200
    code, body = core_api("POST", "/sim/scenario", {"name": ""})
    assert code == 200 and body["cleared"] is True and body["scenario"] is None
    assert core_api("GET", "/sim/info")[1]["scenario"] in (None, "")


# ── the power switch (POST /sim/power) ───────────────────────────────────
#
# Optional per core, exactly like /sim/record: the stub has no press_power(),
# and a car with a key never will. The rules themselves live in the model and
# are tested in tests/test_sim_power.py; these pin the plumbing.

def test_pressing_the_power_switch_is_a_501_on_a_core_without_one(api):
    code, body = api("POST", "/sim/power", {"brake": True})
    assert code == 501
    assert "press_power()" in body["error"]


def test_the_power_switch_starts_and_stops_the_real_core(core_api):
    if not getattr(core_api.sim, "can_press_power", lambda: False)():
        pytest.skip("this simulator core has no power switch")
    core_api("POST", "/sim/knobs", {"start_state": "off", "gear": "P",
                                    "charging": False, "plugged_in": False})
    code, body = core_api("POST", "/sim/power", {})
    assert code == 200 and body["accepted"] is True and body["start_state"] == "acc"
    assert body["simulated"] is True and body["message"]
    code, body = core_api("POST", "/sim/power", {"brake": True})
    assert code == 200 and body["start_state"] == "ready"
    assert core_api("GET", "/sim/knobs")[1]["knobs"]["start_state"] == "ready"
    code, body = core_api("POST", "/sim/power", {})
    assert code == 200 and body["start_state"] == "off" and body["gear"] == "P"


def test_the_power_switch_refuses_with_the_connector_latched(core_api):
    if not getattr(core_api.sim, "can_press_power", lambda: False)():
        pytest.skip("this simulator core has no power switch")
    core_api("POST", "/sim/knobs", {"start_state": "off", "gear": "P", "plugged_in": True})
    code, body = core_api("POST", "/sim/power", {"brake": True})
    assert code == 200, "a refusal is an answer, not an error"
    assert body["accepted"] is False and "connector" in body["message"].lower()
    assert body["start_state"] == "off"


@pytest.mark.parametrize("bad", [{"brake": "yes"}, {"hold": 1}])
def test_a_nonsense_power_body_is_a_400_not_a_500(core_api, bad):
    if not getattr(core_api.sim, "can_press_power", lambda: False)():
        pytest.skip("this simulator core has no power switch")
    code, body = core_api("POST", "/sim/power", bad)
    assert code == 400 and body["error"]


def test_the_new_endpoints_are_listed_alongside_the_old(api):
    code, body = api("GET", "/sim/nowhere")
    assert code == 404
    listed = body["endpoints"]
    assert "/sim/record" in listed
    assert any("sim_seconds" in e for e in listed)
    assert any("clear" in e for e in listed)
    assert "/panel" in listed, "extend the list, never replace it"
    info = api("GET", "/sim/info")[1]["endpoints"]
    assert "record" in info and "sim_seconds" in info["step"] and "clears" in info["scenario"]
    assert any("/sim/power" in e for e in listed) and "power" in info


# ── the CLI ──────────────────────────────────────────────────────────────

def run_cli(*argv, timeout=30):
    return subprocess.run([sys.executable, "-c",
                           "import sys; sys.path.insert(0, 'tests');"
                           "import sim_stub, elm327;"
                           "elm327.make_simulator = lambda vehicle=None, scenario=None, seed=None, knobs=None:"
                           " sim_stub.make_sim(vehicle=vehicle or 'leaf_ze0', knobs=knobs,"
                           " seed=seed, scenario=scenario);"
                           "import hakake_sim; sys.exit(hakake_sim.main(sys.argv[1:]))",
                           *argv], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def test_dump_schema_is_json_an_agent_can_read():
    p = run_cli("--dump-schema")
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["simulated"] is True
    assert doc["knobs"]["soc"]["help"]


def test_dump_state_applies_startup_knobs():
    p = run_cli("--knob", "soc=33", "--knob", "fault.cell_degraded=true", "--dump-state")
    assert p.returncode == 0, p.stderr
    st = json.loads(p.stdout)["state"]
    assert st["soc"] == 33.0 and st["faults"]["cell_degraded"] is True


def test_a_mistyped_startup_knob_fails_before_anything_starts():
    p = run_cli("--knob", "soc")
    assert p.returncode == 2 and "name=value" in p.stderr


def test_json_mode_emits_one_object_per_line_and_labels_it_simulated():
    p = run_cli("--json", "--no-control", "--duration", "0.5", "--report", "0.2")
    assert p.returncode == 0, p.stderr
    lines = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    assert lines[0]["event"] == "ready"
    assert lines[0]["simulated"] is True and "SIMULATED DATA" in lines[0]["warning"]
    assert any(l["event"] == "tick" and "state" in l for l in lines[1:])


def test_human_mode_says_loudly_that_it_is_not_a_car():
    p = run_cli("--no-control", "--duration", "0.3", "--report", "0")
    assert p.returncode == 0, p.stderr
    assert "SIMULATOR" in p.stdout and "SIMULATED DATA" in p.stdout


def test_the_pty_path_is_printed_so_the_dashboard_can_be_pointed_at_it():
    p = run_cli("--json", "--pty", "--no-control", "--duration", "0.4", "--report", "0")
    assert p.returncode == 0, p.stderr
    ready = json.loads(p.stdout.splitlines()[0])
    assert ready["pty"].startswith("/dev/")


def test_the_control_url_is_printed_and_the_port_is_reported():
    p = run_cli("--json", "--control", "0", "--duration", "0.4", "--report", "0")
    assert p.returncode == 0, p.stderr
    ready = json.loads(p.stdout.splitlines()[0])
    assert ready["control"].startswith("http://127.0.0.1:")


def test_a_busy_control_port_is_a_clear_error_not_a_crash():
    httpd = serve_control(make_sim(), port=0, log=lambda *a: None)
    port = httpd.server_address[1]
    try:
        p = run_cli("--control", str(port), "--duration", "0.2")
        assert p.returncode == 4 and "unavailable" in p.stderr
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_record_is_501_not_500_on_a_core_whose_model_lacks_record():
    """The Lancer model has no record(); the Simulator wrapper raises
    NotImplementedError. The control API must answer 501 (so the cockpit
    draws knob cards only), never a 500 that looks like a broken server."""
    import json, threading, urllib.error, urllib.request
    import hakake_sim
    from simulator import make_sim
    sim = make_sim(vehicle="lancer_2009", seed=1)
    httpd = hakake_sim.serve_control(sim, port=0, lock=threading.Lock(), log=lambda *a, **k: None)
    try:
        port = httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/sim/record", timeout=3)
        assert ei.value.code == 501
        body = json.load(ei.value)
        assert "record()" in body["error"] and body["simulated"] is True
    finally:
        httpd.shutdown()
