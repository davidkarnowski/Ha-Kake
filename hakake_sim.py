#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
hakake_sim — run the vehicle simulator as a standalone rig.

Three things live here, and they are deliberately in one file because they are
one idea: *a car that is not there*.

  1. A **control API** (127.0.0.1, stdlib http.server) so a human — or, more
     to the point, an agent — can read the knob schema and change conditions
     **while the dashboard is watching**. Turning a knob mid-run and seeing a
     tile move is the entire reason a simulator beats a recording.

  2. A **pseudo-terminal front end** (`--pty`). The model sits behind a real
     serial device created with os.openpty(), so the dashboard connects with
     the real SerialELM, over a real tty, and cannot tell it is not a car.
     This exercises the serial transport that `--adapter sim` bypasses. It
     works on macOS and Linux; SocketCAN would not.

  3. A **CLI** — `--dump-schema`, `--knob`, `--scenario`, `--seed`, `--speed`,
     `--json` — so everything is reachable without reading source.

  4. A **bulk history generator** (`--generate`). Not a fourth idea so much as
     the same car run fast-forward: months of plausible rows written straight
     into a `Store` in seconds, so the *charts* can be worked on. See
     simulator/history.py and the "iterating on the UI" section of
     docs/SIMULATOR.md.

Populate a database and point the dashboard at it:

    python hakake_sim.py --generate --days 180 --out /tmp/ui.db
    python web/app.py --db /tmp/ui.db --no-reader --port 5001

The one command (the reader hosts the model; control API on 8099 by default):

    python web/app.py --adapter sim --port 5055
    # dashboard http://127.0.0.1:5055   panel /sim   control API :8099/sim/schema

Quick start over a real serial device (human):

    python hakake_sim.py --pty --scenario drive
    # prints the pty, the control URL, and the exact dashboard command:
    python web/app.py --adapter sim --sim-serial /dev/ttys012 --sim-control 8099 --port 5055
    # or let the rig start the dashboard itself:
    python hakake_sim.py --pty --scenario drive --launch-dashboard

Quick start (agent):

    python hakake_sim.py --dump-schema | jq .              # discover the knobs
    python hakake_sim.py --pty --json &                    # run it
    curl -s localhost:8099/sim/state | jq .                # read the model
    curl -s -X POST localhost:8099/sim/knobs \
         -H 'content-type: application/json' -d '{"soc": 15}'

Honesty, restated from docs/SIMULATOR_CONTRACT.md: **a simulator verifies
consistency, not truth.** A green run here proves the encoder and the decoder
agree; it does not prove either matches the car. Nothing this program prints
is a reading from any vehicle, and every record it feeds the dashboard is
stamped `simulated: true`.
"""

import argparse
import difflib
import json
import os
import select
import subprocess
import sys
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_CONTROL_PORT = 8099
DEFAULT_DASHBOARD_PORT = 5055        # what --launch-dashboard and the printed command use
MAX_BODY = 256 * 1024

# `POST /sim/step {"sim_seconds": N}` — a week of simulated time is the most
# anyone skips ahead in one go; past that the request is almost certainly a
# units mistake (milliseconds, a timestamp) and refusing it is kinder.
MAX_SIM_SECONDS = 7 * 86400

# the bounds the core clamps the effective time scale to; mirrored here only so
# --help can state them without importing the core at parse time
TIME_SCALE_MIN, TIME_SCALE_MAX = 0.01, 3600.0

# The control panel. One hand-written file, no build step, no CDN, no third
# dependency — the same rule the dashboard follows, for the same reason: this
# runs in a car park with no signal. It lives beside the model rather than in
# web/static/, which belongs to the real dashboard and must never be confused
# with this.
PANEL_FILE = os.path.join(_ROOT, "simulator", "panel.html")


def panel_html():
    """The panel page, or a plain-text apology that is still a valid page."""
    try:
        with open(PANEL_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        return ("<!doctype html><meta charset=utf-8><title>Ha-Kake SIMULATOR</title>"
                "<body style=\"background:#0a0e17;color:#e0e6f0;font-family:sans-serif;padding:2rem\">"
                f"<h1>SIMULATOR</h1><p>The control panel file is missing: {e}</p>"
                "<p>The JSON API is unaffected: <code>/sim/schema</code>, "
                "<code>/sim/state</code>, <code>/sim/knobs</code>.</p>")


# ── knob parsing ─────────────────────────────────────────────────────────

def parse_knob_value(text):
    """`--knob soc=20` → 20.0, `fault.x=true` → True, `gear=D` → "D".

    JSON first (so 20, true, null, [1,2] and "quoted" all mean what they look
    like), bare word second. An agent that wants no ambiguity can send JSON.
    """
    t = (text or "").strip()
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none"):
        return None
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return t


def parse_knob_args(pairs):
    """A list of `name=value` strings → a dict. Raises ValueError on junk."""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"--knob wants name=value, got {p!r}")
        name, _, value = p.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"--knob wants name=value, got {p!r}")
        out[name] = parse_knob_value(value)
    return out


def known_knob_names(sim):
    names = set()
    for getter in ("knob_schema", "get_knobs"):
        try:
            d = getattr(sim, getter)()
            if isinstance(d, dict):
                names.update(str(k) for k in d.keys())
        except Exception:
            pass
    return names


def suggest(sim, name, limit=5):
    """Near matches for a knob name the model rejected.

    The core is supposed to supply these on the ValueError; we compute our own
    as well, because an agent that mistypes a knob must never be answered with
    a bare 500 and no way forward.
    """
    return difflib.get_close_matches(str(name), sorted(known_knob_names(sim)),
                                     n=limit, cutoff=0.3)


def apply_knobs(sim, knobs, lock=None):
    """Validate then apply. Returns (applied_dict, error_dict_or_None)."""
    if not isinstance(knobs, dict):
        return {}, {"error": "body must be a JSON object of knob: value"}
    known = known_knob_names(sim)
    unknown = [k for k in knobs if known and k not in known]
    if unknown:
        return {}, {"error": f"unknown knob(s): {', '.join(sorted(unknown))}",
                    "unknown": sorted(unknown),
                    "suggestions": {k: suggest(sim, k) for k in sorted(unknown)}}
    try:
        with (lock or _NullLock()):
            applied = sim.set(**knobs)
    except ValueError as e:
        bad = [k for k in knobs if str(k) in str(e)] or list(knobs)
        return {}, {"error": str(e),
                    "unknown": sorted(bad),
                    "suggestions": {k: suggest(sim, k) for k in sorted(bad)}}
    except TypeError as e:
        return {}, {"error": f"bad knob value: {e}"}
    return (applied if isinstance(applied, dict) else dict(knobs)), None


def schema_defaults(sim):
    """{knob: default} straight out of the schema — no hardcoded knob list.

    This is what `POST /sim/reset` applies, so reset works on any vehicle
    profile, present or future, without this file knowing a single knob name.
    """
    try:
        schema = sim.knob_schema()
    except Exception:
        return {}
    out = {}
    for name, spec in (schema or {}).items():
        if isinstance(spec, dict) and "default" in spec:
            out[name] = spec["default"]
    return out


def scenario_list():
    """Shipped scenario names, for the panel's loader. Empty if the core is a
    stand-in that ships none — the panel copes."""
    try:
        from simulator import scenario_names
        return list(scenario_names())
    except Exception:
        return []


def time_scale_info(sim):
    """The effective simulated-seconds-per-real-second, and where it came from.

    Bug of 2026-09-02: `--speed` and the scenario's `clock_scale` knob used to
    multiply, so `--speed 120` on a scenario carrying `clock_scale: 120` ran at
    14400x and a five-hour charge finished before the first sample. `--speed`
    now overrides. It is reported here, on the banner, and on the panel so that
    it can never be silent again. A core that predates the fix (or the contract
    stub) has no time_scale(); say so rather than guessing.
    """
    fn = getattr(sim, "time_scale_info", None)
    if callable(fn):
        return fn()
    return {"time_scale": None, "source": "unknown (core has no time_scale())",
            "speed_override": None, "clock_scale": None}


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── control API ──────────────────────────────────────────────────────────
#
# Deliberately stdlib-only. Ha-Kake has three runtime dependencies (flask,
# bleak, pyserial) and a test rig is not a reason to make it four.
#
#   GET  /sim/schema      every knob: type, unit, range, default, help
#   GET  /sim/state       the model's current state
#   GET  /sim/knobs       current knob values
#   GET  /sim/faults      active faults
#   GET  /sim/record      the state in the dashboard's own vocabulary (decoder
#                         keys, °F twins) — 501 when the core has no record()
#   GET  /sim/info        vehicle, seed, scenario, uptime
#   GET  /health          {"ok": true}
#   POST /sim/knobs       {"soc": 20, "fault.cell_degraded": true}
#   POST /sim/scenario    {"name": "drive"}  or {"path": "..."};
#                         {"name": ""} (or null) clears the scenario — the
#                         model free-runs from where it is
#   POST /sim/power       {"brake": true}      — one push of the car's power
#                         switch (501 when the core has no press_power())
#   POST /sim/step        {"dt": 30}           — advance 30 REAL seconds
#                         (the clock scale applies, as it does live), or
#                         {"sim_seconds": 600} — advance 600 SIMULATED seconds
#                         whatever the clock scale is (a "skip ahead")
#
# Unknown knob → 400 with near matches. Never a 500 for a typo. A capability
# the core lacks (record, clear_scenario) is a 501/400 that says so, never a
# 500 — the Lancer core has no record() and that is how it hides Leaf tiles.

def make_handler(sim, lock, log):
    lock = lock or threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "hakake-sim/1"
        protocol_version = "HTTP/1.1"

        # ── plumbing ─────────────────────────────────────────────────────

        def log_message(self, fmt, *args):        # quiet unless asked
            if os.environ.get("HAKAKE_SIM_HTTP_LOG"):
                log("  [control] " + (fmt % args))

        def _send(self, code, payload):
            # 204 (the CORS preflight) must not carry a body; a browser that
            # sees one may reject the preflight and refuse the real request.
            body = b"" if code == 204 else json.dumps(payload, default=str, sort_keys=True).encode()
            self.send_response(code)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # A simulator control port is loopback-only, but a browser tab on
            # the dashboard is a natural client, so allow it explicitly.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > MAX_BODY:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def _call(self, name, *a, **kw):
            with lock:
                return getattr(sim, name)(*a, **kw)

        def do_OPTIONS(self):
            self._send(204, {})

        # ── reads ────────────────────────────────────────────────────────

        def _send_html(self, code, text):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                if path in ("/", "/panel", "/sim/panel"):
                    return self._send_html(200, panel_html())
                if path == "/sim/scenarios":
                    return self._send(200, {"simulated": True,
                                            "scenarios": scenario_list(),
                                            "current": getattr(sim, "scenario", None)})
                if path in ("/health", "/sim/health"):
                    return self._send(200, {"ok": True, "simulated": True})
                if path == "/sim/schema":
                    return self._send(200, {"simulated": True, "knobs": self._call("knob_schema")})
                if path == "/sim/state":
                    return self._send(200, {"simulated": True, "state": self._call("state")})
                if path == "/sim/knobs":
                    return self._send(200, {"simulated": True, "knobs": self._call("get_knobs")})
                if path == "/sim/faults":
                    return self._send(200, {"simulated": True, "faults": self._call("faults")})
                if path == "/sim/record":
                    # Optional in the contract: a core that cannot speak the
                    # dashboard's vocabulary says so with a 501, and the
                    # cockpit page draws knob cards only. Looked up per
                    # request, not at startup, so a core that grows record()
                    # while this runs (tests swap cores) is seen.
                    can = getattr(sim, "can_record", None)
                    if not callable(getattr(sim, "record", None)) or (callable(can) and not can()):
                        return self._send(501, {"error": "this simulator core has no record()",
                                                "simulated": True})
                    try:
                        rec = self._call("record")
                    except NotImplementedError as e:   # a wrapper whose model cannot speak the vocabulary
                        return self._send(501, {"error": f"this simulator core has no record(): {e}",
                                                "simulated": True})
                    return self._send(200, {"simulated": True, "record": rec})
                if path in ("/sim", "/sim/info"):
                    return self._send(200, self._info())
            except Exception as e:                # a broken core must not look like a broken API
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": f"no such endpoint: {path}", "endpoints": ENDPOINTS})

        def _info(self):
            ts = time_scale_info(sim)
            return {"simulated": True,
                    "vehicle": getattr(sim, "vehicle", None),
                    "seed": getattr(sim, "seed", None),
                    "scenario": getattr(sim, "scenario", None),
                    # the effective clock, stated out loud — see time_scale_info()
                    "time_scale": ts.get("time_scale"),
                    "time_scale_source": ts.get("source"),
                    "speed_override": ts.get("speed_override"),
                    "clock_scale": ts.get("clock_scale"),
                    "time_scale_max": ts.get("max"),
                    "scenarios": scenario_list(),
                    "warning": "SIMULATED DATA — not a reading from any vehicle",
                    "endpoints": {"panel": "/panel", "schema": "/sim/schema",
                                  "state": "/sim/state",
                                  "record": "/sim/record (501 if the core has no record())",
                                  "knobs": "/sim/knobs (GET, POST)",
                                  "faults": "/sim/faults",
                                  "scenarios": "/sim/scenarios",
                                  "scenario": '/sim/scenario (POST {"name": "drive"}; '
                                              '{"name": ""} clears)',
                                  "power": '/sim/power (POST {"brake": bool}; '
                                           '501 if the core has no power switch)',
                                  "reset": "/sim/reset (POST)",
                                  "step": '/sim/step (POST {"dt": real s} or '
                                          '{"sim_seconds": simulated s})'}}

        # ── writes ───────────────────────────────────────────────────────

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": f"bad JSON body: {e}"})
            try:
                if path == "/sim/knobs":
                    applied, err = apply_knobs(sim, body, lock)
                    if err:
                        return self._send(400, err)
                    log(f"  [control] set {applied}")
                    return self._send(200, {"applied": applied, "simulated": True})

                if path == "/sim/scenario":
                    name = body.get("name") or body.get("path") or body.get("scenario")
                    if not name and any(k in body for k in ("name", "path", "scenario")):
                        # {"name": ""} / {"name": null}: an explicit "no
                        # scenario" — the timeline stops, the knobs stay where
                        # they are, the model free-runs. An empty body is
                        # still the mistake it always was (400 below).
                        clear = getattr(sim, "clear_scenario", None)
                        if not callable(clear):
                            return self._send(400, {"error": "this simulator core has no "
                                                             "clear_scenario(); load a "
                                                             "scenario by name instead"})
                        self._call("clear_scenario")
                        log("  [control] scenario cleared (free-running)")
                        return self._send(200, {"scenario": None, "cleared": True,
                                                "simulated": True,
                                                "knobs": self._call("get_knobs")})
                    if not name:
                        return self._send(400, {"error": 'expected {"name": "drive"}, {"path": "..."} '
                                                         'or {"name": ""} to clear'})
                    try:
                        self._call("load_scenario", name)
                    except (ValueError, KeyError, FileNotFoundError) as e:
                        return self._send(400, {"error": f"cannot load scenario {name!r}: {e}"})
                    log(f"  [control] scenario {name}")
                    return self._send(200, {"scenario": name, "simulated": True,
                                            "knobs": self._call("get_knobs")})

                if path == "/sim/reset":
                    defaults = schema_defaults(sim)
                    if not defaults:
                        return self._send(400, {"error": "this core publishes no defaults "
                                                         "to reset to"})
                    applied, err = apply_knobs(sim, defaults, lock)
                    if err:
                        return self._send(400, err)
                    log("  [control] reset to defaults")
                    return self._send(200, {"reset": True, "simulated": True,
                                            "applied": applied})

                if path == "/sim/power":
                    # One push of the car's power switch. Optional per core,
                    # like /sim/record: a profile whose model has no power
                    # switch answers 501 and the cockpit draws no button.
                    press = getattr(sim, "press_power", None)
                    can = getattr(sim, "can_press_power", None)
                    if not callable(press) or (callable(can) and not can()):
                        return self._send(501, {"error": "this simulator core has no "
                                                         "press_power(); set the ignition "
                                                         "knob instead"})
                    brake = body.get("brake", None)
                    if brake is not None and not isinstance(brake, bool):
                        return self._send(400, {"error": 'brake must be true or false '
                                                         '(omit it to read the brake pedal)'})
                    hold = body.get("hold", False)
                    if not isinstance(hold, bool):
                        return self._send(400, {"error": "hold must be true or false"})
                    try:
                        out = self._call("press_power", brake=brake, hold=hold)
                    except NotImplementedError as e:
                        return self._send(501, {"error": f"this simulator core has no "
                                                         f"press_power(): {e}"})
                    log(f"  [control] power switch -> {out.get('start_state')} "
                        f"({out.get('message')})")
                    return self._send(200, out)

                if path == "/sim/step":
                    return self._step(body)
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            self._send(404, {"error": f"no such endpoint: {path}", "endpoints": ENDPOINTS})

        def _step(self, body):
            """`{"dt": N}` = N real seconds; `{"sim_seconds": N}` = N simulated seconds.

            `dt` is what the live transport does every cycle — the clock scale
            applies, so `dt: 1` on a 60x scenario is a simulated minute. That
            is right for "let time pass" and wrong for "skip ahead an hour",
            which is what a panel button means whatever the clock is doing;
            `sim_seconds` is that. The conversion is
            `real = sim_seconds * outer / time_scale`, where `outer` is the
            multiplier a transport has already installed with set_speed(...,
            outer=) and the core divides back out in step() — without it a
            rig running `--speed 10` would skip a tenth of what was asked.
            A core with no time_scale() runs real time; sim_seconds == dt.
            """
            if "dt" in body and "sim_seconds" in body:
                return self._send(400, {"error": "give dt (real seconds) or sim_seconds "
                                                 "(simulated seconds), not both"})
            if "sim_seconds" in body:
                try:
                    want = float(body["sim_seconds"])
                except (TypeError, ValueError):
                    return self._send(400, {"error": "sim_seconds must be a number of "
                                                     "simulated seconds"})
                if want < 0 or want > MAX_SIM_SECONDS:
                    return self._send(400, {"error": f"sim_seconds must be between 0 and "
                                                     f"{MAX_SIM_SECONDS} s (7 days)"})
                scale = getattr(sim, "time_scale", None)
                ts = float(scale()) if callable(scale) else 1.0
                outer = float(getattr(sim, "_outer_scale", 1.0) or 1.0)
                dt = want * outer / max(ts, 1e-9)
                self._call("step", dt)
                return self._send(200, {"stepped": dt, "sim_seconds": want,
                                        "time_scale": ts, "simulated": True})
            try:
                dt = float(body.get("dt", 1.0))
            except (TypeError, ValueError):
                return self._send(400, {"error": "dt must be a number of real seconds "
                                                 "(or send sim_seconds)"})
            if dt < 0 or dt > 86400:
                return self._send(400, {"error": "dt must be between 0 and 86400 real s"})
            self._call("step", dt)
            return self._send(200, {"stepped": dt, "simulated": True})

    return Handler


# What a 404 lists. Extended, never replaced: the panel entries stay because a
# client that lands on the wrong path is usually a human, and "/panel" is the
# answer they want.
ENDPOINTS = ["/", "/panel", "/sim/schema", "/sim/state", "/sim/record",
             "/sim/knobs", "/sim/faults", "/sim/info", "/sim/scenarios",
             "POST /sim/knobs", "POST /sim/scenario", "POST /sim/scenario {name: ''} (clear)",
             "POST /sim/step {dt}", "POST /sim/step {sim_seconds}", "POST /sim/reset",
             "POST /sim/power {brake} (501 if the core has no power switch)"]


def serve_control(sim, port=DEFAULT_CONTROL_PORT, lock=None, log=print, host="127.0.0.1"):
    """Start the control API in a daemon thread. Returns the HTTPServer.

    Loopback only, always: this thing can put a car into a fault state on a
    dashboard someone might be reading, and it has no authentication.
    """
    httpd = ThreadingHTTPServer((host, int(port)), make_handler(sim, lock, log))
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="sim-control", daemon=True)
    t.start()
    httpd.thread = t
    return httpd


# ── pseudo-terminal front end ────────────────────────────────────────────
#
# os.openpty() gives a (master, slave) pair. We answer on the master; the
# slave has a real device path (/dev/ttysNNN on macOS, /dev/pts/N on Linux)
# that pyserial opens like any USB dongle. The slave fd stays open for the
# process lifetime so the master does not see EOF when a client disconnects
# and reconnects — the reader's supervisor does exactly that.

class PtyServer:
    """An ELM327 on a pseudo-terminal, backed by a SimELM state machine."""

    def __init__(self, elm, monitor_window=0.5, log=print):
        self.elm = elm
        self.monitor_window = monitor_window
        self.log = log
        self.master = self.slave = None
        self.path = None
        self.commands = 0
        self._buf = b""

    def open(self):
        import tty
        self.master, self.slave = os.openpty()
        try:
            tty.setraw(self.master)
            tty.setraw(self.slave)      # no echo, no CR/LF translation
        except Exception:               # pragma: no cover - platform quirk
            pass
        self.path = os.ttyname(self.slave)
        return self.path

    def close(self):
        for fd in (self.master, self.slave):
            try:
                if fd is not None:
                    os.close(fd)
            except OSError:
                pass
        self.master = self.slave = None

    # ── wire format ──────────────────────────────────────────────────────

    def format(self, lines):
        """ELM327 with ATL1/ATE0: CR-terminated lines, then a bare CR and '>'."""
        if not lines:
            lines = ["OK"]
        return ("".join(l + "\r" for l in lines) + "\r>").encode("ascii", "replace")

    def feed(self, data):
        """Bytes in from the client → response bytes out. Pure, so it is testable."""
        out = b""
        self._buf += data
        while True:
            i = min((j for j in (self._buf.find(b"\r"), self._buf.find(b"\n")) if j >= 0),
                    default=-1)
            if i < 0:
                break
            raw, self._buf = self._buf[:i], self._buf[i + 1:]
            cmd = raw.decode("ascii", "replace").strip()
            self.commands += 1
            if not cmd:
                out += b"\r>"          # any char interrupts monitor mode
                continue
            out += self.format(self.elm.handle(cmd, timeout=self.monitor_window))
        return out

    def pump(self, timeout=0.2):
        """One select() turn. Returns False if the pty went away."""
        self.elm.advance()
        try:
            r, _, _ = select.select([self.master], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not r:
            return True
        try:
            data = os.read(self.master, 4096)
        except OSError as e:
            # EIO on macOS/Linux just means no client has the slave open.
            if getattr(e, "errno", None) in (5, 35, 11):
                time.sleep(0.05)
                return True
            return False
        if not data:
            return True
        resp = self.feed(data)
        if resp:
            try:
                os.write(self.master, resp)
            except OSError:
                return True
        return True


# ── CLI ──────────────────────────────────────────────────────────────────

# ── bulk history generation ──────────────────────────────────────────────

def run_generate(args):
    """`--generate`: write a synthetic database and say what to do with it."""
    from simulator import history

    t0 = time.monotonic()
    try:
        sm = history.generate(
            out=args.out, days=args.days, vehicle=args.vehicle or "leaf_ze0",
            seed=1 if args.seed is None else args.seed,
            sample_s=args.sample, idle_sample_s=args.idle_sample,
            cells_per_day=args.cells_per_day,
            log=(lambda *a: None) if args.json else print)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    sm["seconds"] = round(time.monotonic() - t0, 2)
    if args.json:
        emit(sm, True)
        return 0
    print(f"  wrote {sm['rows']:,} readings, {sm['cell_rows']:,} cell rows, "
          f"{sm['events']:,} events in {sm['seconds']}s")
    print(f"  {sm['drives']} drives, {sm['charges']} charges, "
          f"{sm['idle_days']} days the car never moved")
    print(f"  span: {sm['start'][:10]} .. {sm['end'][:10]}   seed {sm['seed']}")
    print(f"  database: {sm['path']}")
    print(f"  state:    {sm['state_path']}")
    print("  *** SYNTHETIC DATA — generated, not a reading from any vehicle. "
          "The meta table says so too. ***")
    print("\n  Look at it:")
    print(f"    python web/app.py --db {sm['path']} --no-reader --port 5001")
    return 0


def build_sim(args):
    """The simulator core, with `--speed` installed as the clock override.

    The override goes on the core and NOT on the transport as a second
    multiplier — that is the whole fix. SimELM still gets the same number so
    the wall-clock -> dt conversion is right; the core divides it back out so
    the product is exactly `--speed` and never `--speed * clock_scale`.
    """
    from elm327 import make_simulator
    knobs = parse_knob_args(args.knob)
    sim = make_simulator(vehicle=args.vehicle, scenario=args.scenario,
                         seed=args.seed, knobs=knobs)
    setter = getattr(sim, "set_speed", None)
    if callable(setter) and args.speed is not None:
        setter(args.speed, outer=args.speed)
    return sim


def emit(obj, json_mode):
    if json_mode:
        print(json.dumps(obj, default=str), flush=True)
    else:
        print(obj if isinstance(obj, str) else json.dumps(obj, default=str), flush=True)


# ── the dashboard, pointed at this rig ───────────────────────────────────
#
# `--pty` used to print a pty path and leave the rest to the reader. The
# command that starts the dashboard against it is not obvious (it is
# `--adapter sim`, not `--adapter usb`, so the rows go to the throwaway
# database), so the rig prints it verbatim and, with --launch-dashboard,
# runs it. One place builds the argv so the printed line and the spawned
# process cannot drift apart.

def dashboard_command(pty, control_port=None, port=DEFAULT_DASHBOARD_PORT, python="python"):
    """argv for `web/app.py` against a pty from this rig.

    `--sim-control <port>` here means "an external rig (us) owns that port —
    do not start one"; the reader exports it as HAKAKE_SIM_CONTROL_URL so the
    dashboard can link to the panel. Without a control port the dashboard
    still runs; there is just nothing to link to.
    """
    cmd = [python, os.path.join("web", "app.py"), "--adapter", "sim", "--sim-serial", str(pty)]
    if control_port:
        cmd += ["--sim-control", str(int(control_port))]
    cmd += ["--port", str(int(port))]
    return cmd


def panel_url(port=DEFAULT_DASHBOARD_PORT, control_url=None):
    """The cockpit page on the dashboard, told where the control API is.

    `?control=` wins over anything the page could discover on its own, which
    is what a rig on an unusual port needs. The page itself is served by the
    dashboard (`GET /sim`), not by this process.
    """
    u = f"http://127.0.0.1:{int(port)}/sim"
    return u + (f"?control={control_url}" if control_url else "")


class DashboardChild:
    """`--launch-dashboard`: web/app.py as a child, taken down when we go.

    SIGTERM first — the app installs a handler that kills its own reader
    child and exits cleanly — then SIGKILL after a grace, so a wedged
    dashboard cannot keep a pty and a database open after the rig is gone.
    """

    GRACE_S = 5.0

    def __init__(self, argv, log=print, popen=None):
        self.argv = list(argv)
        self.log = log
        self.proc = (popen or subprocess.Popen)(self.argv, cwd=_ROOT)

    @property
    def pid(self):
        return self.proc.pid

    def poll(self):
        """Exit code if the dashboard has stopped, else None."""
        return self.proc.poll()

    def stop(self):
        if self.proc.poll() is not None:
            return self.proc.returncode
        try:
            self.proc.terminate()
        except OSError:
            return self.proc.poll()
        try:
            return self.proc.wait(timeout=self.GRACE_S)
        except subprocess.TimeoutExpired:
            self.log(f"  dashboard pid {self.pid} ignored SIGTERM for {self.GRACE_S}s; killing it")
            try:
                self.proc.kill()
            except OSError:
                pass
            return self.proc.wait()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hakake_sim",
        description="Run the Ha-Kake vehicle simulator (no car, no adapter). "
                    "Everything it produces is simulated and labelled as such.",
        epilog="Agent guide: docs/SIMULATOR.md")
    ap.add_argument("--vehicle", default=None, help="Vehicle profile (default: the active one)")
    ap.add_argument("--scenario", default=None, help="Scenario name (idle/drive/charge/degraded_pack) or a JSON file")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed — same seed, same run")
    ap.add_argument("--speed", type=float, default=None, metavar="X",
                    help="Effective clock scaling: X simulated seconds per real second. "
                         "OVERRIDES a scenario's clock_scale knob rather than multiplying "
                         f"it; clamped to {TIME_SCALE_MIN}..{TIME_SCALE_MAX}. Omit it and "
                         "the scenario's clock_scale applies; with neither, real time.")
    ap.add_argument("--knob", action="append", default=[], metavar="NAME=VALUE",
                    help="Set a knob at startup; repeatable (e.g. --knob soc=20 --knob fault.insulation_low=true)")
    ap.add_argument("--pty", action="store_true",
                    help="Publish a pseudo-terminal so the dashboard connects with the real serial transport")
    ap.add_argument("--launch-dashboard", nargs="?", const=DEFAULT_DASHBOARD_PORT, default=None,
                    type=int, metavar="PORT",
                    help="With --pty: also start `web/app.py --adapter sim --sim-serial <pty>` "
                         f"on 127.0.0.1:PORT (default {DEFAULT_DASHBOARD_PORT}) and stop it "
                         "when this rig exits")
    ap.add_argument("--control", type=int, default=DEFAULT_CONTROL_PORT, metavar="PORT",
                    help=f"Control API port on 127.0.0.1 (default {DEFAULT_CONTROL_PORT}; 0 picks a free one)")
    ap.add_argument("--no-control", action="store_true", help="Do not start the control API")
    ap.add_argument("--monitor-window", type=float, default=0.5,
                    help="Seconds of frames an ATMA returns (default 0.5)")
    ap.add_argument("--report", type=float, default=5.0, help="Seconds between status lines (0 = never)")
    ap.add_argument("--duration", type=float, default=0.0, help="Exit after N seconds (0 = run until interrupted)")
    ap.add_argument("--json", action="store_true", help="Machine-readable output: one JSON object per line")
    ap.add_argument("--dump-schema", action="store_true", help="Print the knob schema as JSON and exit")
    ap.add_argument("--dump-state", action="store_true", help="Print the model state as JSON and exit")
    g = ap.add_argument_group("bulk history generation (--generate)")
    g.add_argument("--generate", action="store_true",
                   help="Write months of synthetic history to a database and exit — "
                        "for iterating on the UI and on report output")
    g.add_argument("--days", type=float, default=180.0, help="Days of history to generate (default 180)")
    g.add_argument("--out", default=None, metavar="PATH",
                   help="Database to write (default web/sim_history.db; the real "
                        "database is refused)")
    g.add_argument("--sample", type=float, default=120.0,
                   help="Seconds between rows while the car is doing something (default 120)")
    g.add_argument("--idle-sample", type=float, default=1800.0, dest="idle_sample",
                   help="Seconds between rows while it is parked (default 1800)")
    g.add_argument("--cells-per-day", type=int, default=4, dest="cells_per_day",
                   help="Full 96-cell reads written per simulated day (default 4)")
    args = ap.parse_args(argv)

    if args.generate:
        return run_generate(args)

    try:
        sim = build_sim(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ConnectionError as e:      # simulator core missing
        print(f"error: {e}", file=sys.stderr)
        return 3

    if args.dump_schema:
        print(json.dumps({"simulated": True,
                          "vehicle": getattr(sim, "vehicle", args.vehicle),
                          "knobs": sim.knob_schema()}, indent=2, default=str, sort_keys=True))
        return 0
    if args.dump_state:
        print(json.dumps({"simulated": True, "state": sim.state()},
                         indent=2, default=str, sort_keys=True))
        return 0

    from elm327 import SimELM
    elm = SimELM(sim=sim, vehicle=args.vehicle, scenario=args.scenario,
                 seed=args.seed, speed=(1.0 if args.speed is None else args.speed))
    elm.advance()                                    # start the clock

    log = (lambda *a: None) if args.json else print
    ts = time_scale_info(sim)
    ready = {"event": "ready", "simulated": True,
             "vehicle": getattr(sim, "vehicle", args.vehicle),
             "scenario": elm.scenario, "seed": elm.seed, "speed": elm.speed,
             # the effective multiplier, resolved once and stated out loud
             "time_scale": ts.get("time_scale"),
             "time_scale_source": ts.get("source"),
             "warning": "SIMULATED DATA — not a reading from any vehicle"}

    httpd = None
    if not args.no_control:
        try:
            httpd = serve_control(sim, args.control, lock=elm.lock, log=log)
        except OSError as e:
            print(f"error: control port {args.control} unavailable: {e}", file=sys.stderr)
            return 4
        ready["control"] = f"http://127.0.0.1:{httpd.server_address[1]}"
        elm.control_url = ready["control"]

    if args.launch_dashboard is not None and not args.pty:
        print("error: --launch-dashboard needs --pty (the dashboard connects over the pty); "
              "without a pty use `python web/app.py --adapter sim` directly", file=sys.stderr)
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        return 2

    pty = None
    dashboard = None
    if args.pty:
        pty = PtyServer(elm, monitor_window=args.monitor_window, log=log)
        ready["pty"] = pty.open()
        control_port = httpd.server_address[1] if httpd else None
        dash_port = args.launch_dashboard if args.launch_dashboard is not None else DEFAULT_DASHBOARD_PORT
        ready["dashboard_command"] = " ".join(
            dashboard_command(ready["pty"], control_port, port=dash_port))
        ready["panel_url"] = panel_url(dash_port, ready.get("control"))
        if args.launch_dashboard is not None:
            try:
                dashboard = DashboardChild(
                    dashboard_command(ready["pty"], control_port, port=dash_port,
                                      python=sys.executable), log=log)
            except OSError as e:
                print(f"error: could not start the dashboard: {e}", file=sys.stderr)
                pty.close()
                if httpd:
                    httpd.shutdown()
                    httpd.server_close()
                return 5
            ready["dashboard_pid"] = dashboard.pid
            ready["dashboard_url"] = f"http://127.0.0.1:{dash_port}"

    if args.json:
        emit(ready, True)
    else:
        print("Ha-Kake SIMULATOR — no adapter, no car. Every value is generated.")
        print(f"  vehicle:  {ready['vehicle']}    scenario: {elm.scenario}    seed: {elm.seed}")
        if ts.get("time_scale") is not None:
            print(f"  clock:    {ts['time_scale']}x simulated time  ({ts['source']})")
        if pty:
            print(f"  pty:      {ready['pty']}")
            if dashboard:
                print(f"  dashboard: {ready['dashboard_url']}   (pid {dashboard.pid}, "
                      f"stops when this does)")
            else:
                print("  start the dashboard against it with exactly this:")
                print(f"            {ready['dashboard_command']}")
            print(f"  cockpit:  {ready['panel_url']}   <- the dashboard's simulator page")
        if httpd:
            print(f"  panel:    {ready['control']}/   (minimal fallback panel served here)")
            print(f"  control:  {ready['control']}/sim/schema   (see docs/SIMULATOR.md)")
        for note in list(getattr(sim, "notes", []) or []):
            print(f"  note:     {note}")
        if not pty and not httpd:
            print("  nothing to serve (--no-control and no --pty); the model just runs.")
        print("  *** SIMULATED DATA — not a reading from any vehicle ***")
        sys.stdout.flush()

    t0 = time.monotonic()
    last_report = t0
    rc = 0
    try:
        while True:
            if pty:
                if not pty.pump(0.2):
                    break
            else:
                elm.advance()
                time.sleep(0.2)
            now = time.monotonic()
            if args.report and now - last_report >= args.report:
                last_report = now
                snap = {"event": "tick", "simulated": True,
                        "elapsed_s": round(now - t0, 1),
                        "sim_time_s": round(elm.sim_time, 1),
                        "commands": elm.commands,
                        "state": sim.state()}
                if args.json:
                    emit(snap, True)
                else:
                    st = snap["state"]
                    keys = [k for k in ("soc", "pack_voltage_v", "current_a", "speed_mph", "rpm")
                            if k in st]
                    brief = "  ".join(f"{k}={st[k]}" for k in keys) or f"{len(st)} keys"
                    print(f"  [sim {snap['sim_time_s']:8.1f}s] {brief}   ({elm.commands} cmds)", flush=True)
            if args.duration and now - t0 >= args.duration:
                break
            if dashboard is not None and dashboard.poll() is not None:
                # The thing we were started to feed is gone (port taken,
                # Ctrl-C in its window). Running on would leave a rig nobody
                # can see; stop and say so.
                rc = dashboard.poll()
                msg = {"event": "dashboard_exited", "simulated": True, "rc": rc}
                emit(msg, True) if args.json else print(f"  dashboard exited rc={rc}; stopping.")
                break
    except KeyboardInterrupt:
        if not args.json:
            print("\nStopped.")
    finally:
        if dashboard is not None:
            dashboard.stop()
        if pty:
            pty.close()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
