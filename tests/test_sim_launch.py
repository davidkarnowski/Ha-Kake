# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One command starts the simulated car, the dashboard and the control API.

`python web/app.py --adapter sim` is canonical: the control API comes up on
8099 unless told otherwise, and a busy port is a fallback with a loud log —
not a reader child that dies and is restarted every two seconds for ever
(that is what a stale `hakake_sim.py --pty` on 8099 used to cause under a
default-on port). The pty rig, for its part, prints the exact dashboard
command instead of a pty path and a shrug, and can start the dashboard
itself.

Nothing here binds port 5000, and every process started is stopped.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import urllib.request

import pytest

from conftest import ROOT                                                # noqa: E402
import elm327                                                            # noqa: E402
import hakake_sim                                                        # noqa: E402
from hakake_sim import (DEFAULT_DASHBOARD_PORT, DashboardChild,          # noqa: E402
                        dashboard_command, panel_url)
from sim_stub import make_sim                                            # noqa: E402
from test_sim_control import run_cli                                     # noqa: E402

APP = os.path.join(ROOT, "web", "app.py")


def run(coro):
    return asyncio.run(coro)


def busy_port():
    """A listening socket on a free loopback port, and that port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


# ── the control port under the reader ────────────────────────────────────

def test_a_busy_control_port_falls_back_to_a_free_one_and_says_so():
    holder, port = busy_port()
    lines = []
    elm = elm327.SimELM(sim=make_sim(), control_port=port)
    try:
        run(elm.connect(log=lines.append))            # must not raise
        assert elm.control_port_requested == port
        assert elm.control_port not in (None, 0, port)
        # the record carries the port actually bound, not the one asked for
        assert elm.marker()["sim_control_url"] == f"http://127.0.0.1:{elm.control_port}"
        assert elm.control_url == elm.marker()["sim_control_url"]
        loud = [l for l in lines if str(port) in l and str(elm.control_port) in l]
        assert loud, f"the log must name both ports: {lines}"
        with urllib.request.urlopen(elm.control_url + "/health", timeout=5) as r:
            assert json.loads(r.read())["ok"] is True
    finally:
        run(elm.close())
        holder.close()


def test_port_zero_means_a_free_port_not_no_server():
    """0 used to be falsy all the way down the chain and silently meant 'off'."""
    elm = elm327.SimELM(sim=make_sim(), control_port=0)
    try:
        run(elm.connect(log=lambda *a: None))
        assert elm.control_port > 0
        assert elm.marker()["sim_control_url"] == f"http://127.0.0.1:{elm.control_port}"
    finally:
        run(elm.close())


def test_no_control_port_means_no_server():
    elm = elm327.SimELM(sim=make_sim(), control_port=None)
    run(elm.connect(log=lambda *a: None))
    assert elm._control is None and elm.marker()["sim_control_url"] == ""
    run(elm.close())


# ── the dashboard's arguments ─────────────────────────────────────────────

def test_the_dashboard_help_shows_the_default_port_and_the_opt_out():
    p = subprocess.run([sys.executable, APP, "--adapter", "sim", "--help"],
                       cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, p.stderr
    assert "8099" in p.stdout and "--no-sim-control" in p.stdout
    assert "0 = a free port" in p.stdout


def test_the_status_api_carries_the_startup_control_url(tmp_path, monkeypatch):
    """/api/status.sim_control_url: the state record's value when it has one
    (the port really bound), else the one this process knew at startup."""
    import app as webapp
    state = tmp_path / "state.json"
    monkeypatch.setattr(webapp, "STATE_FILE", str(state))
    monkeypatch.setattr(webapp, "DEMO", None)
    monkeypatch.setattr(webapp, "SIM_CONTROL_URL", "http://127.0.0.1:8099")
    client = webapp.app.test_client()
    assert client.get("/api/status").get_json()["sim_control_url"] == "http://127.0.0.1:8099"
    state.write_text(json.dumps({"status": "ok", "simulated": True,
                                 "sim_control_url": "http://127.0.0.1:49999"}))
    assert client.get("/api/status").get_json()["sim_control_url"] == "http://127.0.0.1:49999"
    monkeypatch.setattr(webapp, "SIM_CONTROL_URL", None)
    state.write_text(json.dumps({"status": "ok"}))
    assert "sim_control_url" not in client.get("/api/status").get_json()


# ── the pty rig prints the exact command ─────────────────────────────────

def test_dashboard_command_is_exactly_what_the_app_accepts():
    cmd = dashboard_command("/dev/ttys012", 8099)
    assert cmd == ["python", os.path.join("web", "app.py"), "--adapter", "sim",
                   "--sim-serial", "/dev/ttys012", "--sim-control", "8099", "--port", "5055"]
    assert "--sim-control" not in dashboard_command("/dev/ttys012", None)
    assert dashboard_command("/dev/ttys012", 8099, port=5077, python="/x/python")[0] == "/x/python"
    with open(APP, encoding="utf-8") as f:
        src = f.read()
    for flag in ("--adapter", "--sim-serial", "--sim-control", "--port"):
        assert f'"{flag}"' in src, f"web/app.py no longer accepts {flag}"


def test_panel_url_points_the_cockpit_at_the_rig():
    assert panel_url(5055, "http://127.0.0.1:8099") == \
        "http://127.0.0.1:5055/sim?control=http://127.0.0.1:8099"
    assert panel_url(5055, None) == "http://127.0.0.1:5055/sim"
    assert DEFAULT_DASHBOARD_PORT == 5055


def test_pty_mode_prints_the_dashboard_command_and_the_cockpit_url():
    p = run_cli("--json", "--pty", "--control", "0", "--duration", "0.4", "--report", "0")
    assert p.returncode == 0, p.stderr
    ready = json.loads(p.stdout.splitlines()[0])
    port = ready["control"].rsplit(":", 1)[1]
    assert ready["dashboard_command"] == (
        f"python web/app.py --adapter sim --sim-serial {ready['pty']} "
        f"--sim-control {port} --port 5055")
    assert ready["panel_url"] == f"http://127.0.0.1:5055/sim?control={ready['control']}"
    assert "dashboard_pid" not in ready              # nothing was launched


def test_pty_mode_says_it_in_plain_text_too():
    p = run_cli("--pty", "--control", "0", "--duration", "0.3", "--report", "0")
    assert p.returncode == 0, p.stderr
    assert "python web/app.py --adapter sim --sim-serial /dev/" in p.stdout
    assert "/sim?control=http://127.0.0.1:" in p.stdout
    assert "SIMULATED DATA" in p.stdout


def test_pty_without_a_control_api_still_prints_a_command():
    p = run_cli("--json", "--pty", "--no-control", "--duration", "0.3", "--report", "0")
    assert p.returncode == 0, p.stderr
    ready = json.loads(p.stdout.splitlines()[0])
    assert "--sim-serial" in ready["dashboard_command"]
    assert "--sim-control" not in ready["dashboard_command"]
    assert ready["panel_url"] == "http://127.0.0.1:5055/sim"


# ── --launch-dashboard ────────────────────────────────────────────────────
#
# The real command would start Flask plus a reader child against the pty —
# a whole stack, seconds of startup, a port to pick. What matters here is
# the contract: it spawns exactly the printed command, with this
# interpreter, from the repo root, and it is stopped when the rig exits.
# A fake Popen proves all of that without a process.

class FakeProc:
    started = []

    def __init__(self, argv, cwd=None, **kw):
        self.argv, self.cwd = list(argv), cwd
        self.pid = 4242
        self.rc = None
        self.terminated = self.killed = False
        FakeProc.started.append(self)

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = -15

    def kill(self):
        self.killed = True
        self.rc = -9

    def wait(self, timeout=None):
        if self.rc is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.rc

    @property
    def returncode(self):
        return self.rc


@pytest.fixture
def stubbed_rig(monkeypatch):
    monkeypatch.setattr(elm327, "make_simulator",
                        lambda vehicle=None, scenario=None, seed=None, knobs=None:
                        make_sim(vehicle=vehicle or "leaf_ze0", knobs=knobs,
                                 seed=seed, scenario=scenario))
    monkeypatch.setattr(hakake_sim.subprocess, "Popen", FakeProc)
    FakeProc.started.clear()
    yield FakeProc


def test_launch_dashboard_spawns_the_printed_command_and_stops_it(stubbed_rig, capsys):
    rc = hakake_sim.main(["--json", "--pty", "--control", "0", "--launch-dashboard", "5077",
                          "--duration", "0.3", "--report", "0"])
    assert rc == 0
    ready = json.loads(capsys.readouterr().out.splitlines()[0])
    assert len(stubbed_rig.started) == 1
    proc = stubbed_rig.started[0]
    assert ready["dashboard_pid"] == 4242 and ready["dashboard_url"] == "http://127.0.0.1:5077"
    assert proc.cwd == ROOT
    assert proc.argv[0] == sys.executable
    assert proc.argv[1:] == ready["dashboard_command"].split()[1:]
    assert proc.argv[-2:] == ["--port", "5077"]
    assert proc.terminated, "the dashboard must be taken down with the rig"


def test_launch_dashboard_defaults_to_5055_and_never_5000(stubbed_rig, capsys):
    rc = hakake_sim.main(["--json", "--pty", "--no-control", "--launch-dashboard",
                          "--duration", "0.2", "--report", "0"])
    assert rc == 0
    proc = stubbed_rig.started[0]
    assert proc.argv[-2:] == ["--port", str(DEFAULT_DASHBOARD_PORT)]
    assert "5000" not in proc.argv


def test_a_dashboard_that_dies_stops_the_rig(stubbed_rig, capsys, monkeypatch):
    def die(self):
        self.rc = 1
        return 1
    monkeypatch.setattr(stubbed_rig, "poll", die)
    rc = hakake_sim.main(["--json", "--pty", "--no-control", "--launch-dashboard",
                          "--duration", "30", "--report", "0"])
    assert rc == 1
    events = [json.loads(l)["event"] for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert "dashboard_exited" in events


def test_launch_dashboard_needs_a_pty(stubbed_rig, capsys):
    rc = hakake_sim.main(["--no-control", "--launch-dashboard", "--duration", "0.1"])
    assert rc == 2 and "--pty" in capsys.readouterr().err
    assert not stubbed_rig.started


def test_a_wedged_dashboard_is_killed_after_the_grace():
    class Wedged(FakeProc):
        def terminate(self):
            self.terminated = True            # ignores it

        def kill(self):
            self.killed = True
            self.rc = -9

    child = DashboardChild(["x"], log=lambda *a: None, popen=Wedged)
    child.GRACE_S = 0.01
    assert child.stop() == -9
    assert child.proc.terminated and child.proc.killed
