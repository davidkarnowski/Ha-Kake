#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ha-Kake Dashboard
Flask web server with integrated reader. The vehicle comes from --vehicle
(profiles in vehicles/; default the 2012 Leaf).

API:
  /api/status                    latest state (battery_state.json)
  /api/history?minutes=1440      downsampled readings (omit or minutes=0 → all)
  /api/health                    per-day capacity / SOH / temps for degradation chart
  /api/cells?limit=30            per-cell voltages for the last N full reads
  /api/tiles                     GET/PUT tile layout (drives what the reader polls)
  /api/signals                   signal registry, colour scales, tile types
  /api/layouts[/<name>[/load]]   named layouts (save / load / delete)
  /api/calibration               GET/PUT/DELETE per-car offsets (current zero)
  /sim                           simulator cockpit page (always renders; drives the
                                 control API when there is one)
  /api/sim/tiles                 GET/PUT the cockpit's own tile layout

Usage:
  python app.py                      # auto-detect adapter
  python app.py --adapter ble        # force BLE
  python app.py --adapter replay     # no car: run the whole stack off a recorded fixture
  python app.py --adapter sim        # no car: the simulated car, the dashboard and the
                                     # control API (127.0.0.1:8099) in one command; /sim
                                     # is the cockpit page
  python app.py --adapter sim --sim-control 0      # ... control API on a free port
  python app.py --adapter sim --no-sim-control     # ... no control API at all
  python app.py --demo               # canned JSON only (docs screenshots), no reader
  python app.py --interval 0.5       # min seconds per reader cycle (default: 0.5)
  python app.py --fast               # group-01-only power loop
  python app.py --no-reader          # dashboard only (reader.py separate)
  python app.py --db /tmp/ui.db --no-reader --port 5001
                                     # no car: open a database someone else wrote
                                     # (e.g. `hakake_sim.py --generate`) and iterate on charts
  python app.py --vehicle lancer_2009  # a different vehicle profile
"""

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from store import Store                 # noqa: E402
import reader                            # noqa: E402  (vehicle-bound globals: reader.ITEMS etc.)
import signals                           # noqa: E402
from reader import (load_tiles, save_tiles, load_calibration, save_calibration,  # noqa: E402
                    list_layouts, save_layout, load_layout, delete_layout,
                    load_sim_tiles, save_sim_tiles)
from signals import COLOR_SCALES, TILE_TYPES              # noqa: E402
from util import env                                     # noqa: E402

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "battery_state.json")
DEMO_DIR = os.path.join(os.path.dirname(DIR), "docs", "demo")
DEMO = env("HAKAKE_DEMO", "LEAF_DEMO")   # dir with state.json / history.json for docs screenshots
DB_PATH = None                           # None → the profile's own file; replay overrides it
# Where the simulator's control API is, when this process knows at startup:
# `--adapter sim` on a fixed port (default 8099), or `--sim-serial` with the
# external rig's port. None otherwise — including `--sim-control 0`, where
# only the reader learns the port it got and reports it in the state record
# (`sim_control_url`), which /api/status prefers over this when present. The
# cockpit page (/sim) reads whichever is available.
SIM_CONTROL_URL = None
DEFAULT_SIM_CONTROL_PORT = 8099

# Two ways to run without a car, and they are not the same thing:
#
#   --demo    serves frozen JSON from docs/demo/. No reader, no transport, no
#             decoders — it exists to make screenshots reproducible. Every API
#             route below has a demo branch so the real database is never
#             opened in this mode.
#   --adapter replay
#             runs the *entire* stack (reader, scheduler, elm327, profile
#             decode, store, API) against a recorded session fixture. This is
#             the one to use to see a vehicle profile work. It writes to its
#             own throwaway database, never web/leaf_battery.db.
#
#   --adapter sim
#             runs the entire stack against a *generated* vehicle (simulator/,
#             docs/SIMULATOR.md) rather than a recorded one. The difference
#             that matters: with --sim-control the conditions can be changed
#             while the dashboard watches — drop the SOC, degrade a cell — so
#             a UI path can be exercised that no recording contains. Its rows
#             also go to a throwaway database.
#
# Replay makes demo mode nearly redundant; demo survives because it needs no
# subprocess and no fixture, which is what the docs screenshots want.

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True   # the page is edited while the server runs; never serve a stale tile
app.jinja_env.auto_reload = True
_local = threading.local()


def store():
    """One SQLite connection per request thread.

    A single shared connection (check_same_thread=False) used concurrently by
    Flask's request threads segfaulted inside sqlite3 (2026-08-24, twice).
    """
    s = getattr(_local, "store", None)
    if s is None:
        s = _local.store = Store(DB_PATH)
    return s


def vehicle_ctx():
    """What the page chrome needs to know about the active profile.

    `logo` is an optional profile attribute (LOGO = "leaf" restores the leaf
    silhouette on the Leaf); anything else gets the neutral dial mark.
    `level_key` is the signal the mark fills with — the first level-ish signal
    the profile's registry actually declares, or None for a static mark.
    """
    v = reader.VEHICLE
    level = next((k for k in ("soc", "fuel_pct") if k in signals.SIGNALS), None)
    return {"name": v.NAME, "title": v.TITLE,
            "logo": getattr(v, "LOGO", "dial"),
            "level_key": level}


@app.route("/")
def index():
    return render_template("index.html", vehicle=vehicle_ctx())


def sim_ctx():
    """What the cockpit page needs from this process.

    `control_url` is the simulator control API known at startup (None when
    the page must discover it from /api/status, or when there is none — the
    page still renders and says how to launch). `tiles` lists the built-in
    tile ids the active profile declares, so the template includes only the
    partials that profile can drive — the dashboard's own rule, applied
    server-side; the page drops any the record still cannot drive.
    """
    return {"control_url": SIM_CONTROL_URL,
            "tiles": [t["id"] for t in reader.TILES]}


@app.route("/sim")
def sim_page():
    return render_template("sim.html", vehicle=vehicle_ctx(), sim=sim_ctx())


@app.route("/api/sim/tiles", methods=["GET", "PUT"])
def api_sim_tiles():
    """The cockpit's arrangement (web/sim_tiles.json). Shape-only validation:
    the page owns its card ids, the server owns the types."""
    if DEMO:
        if request.method == "PUT":
            return jsonify({"error": "demo mode is read-only"}), 403
        return jsonify({"tiles": []})
    if request.method == "PUT":
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("tiles"), list):
            return jsonify({"error": 'expected {"tiles": [...]}'}), 400
        return jsonify(save_sim_tiles(body))
    return jsonify(load_sim_tiles())


def _demo(name, default):
    """One canned file from the demo directory; the default when it is absent.

    A missing file means the demo simply has nothing to show for that route —
    it never falls through to the real database, and it never makes a value up.
    """
    try:
        with open(os.path.join(DEMO, name)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, OSError):
        return default


@app.route("/api/status")
def api_status():
    if DEMO:
        st = _demo("state.json", {"status": "waiting"})
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
        for k in ("timestamp", "last_ok", "state_time"):
            if k in st or k in ("timestamp", "last_ok"):
                st[k] = now                       # keep demo looking live (fresh clock, green dot, pulse)
        st["demo"] = True                         # canned data — say so in the payload
        return jsonify(st)
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"status": "waiting", "message": "No data yet. Reader starting..."}
    # The record's own value wins: it names the port the reader actually
    # bound (which may differ from the one asked for if that was busy). The
    # startup global fills in before the first record exists.
    if SIM_CONTROL_URL and not state.get("sim_control_url"):
        state["sim_control_url"] = SIM_CONTROL_URL
    return jsonify(state)


@app.route("/api/history")
def api_history():
    if DEMO:
        return jsonify(_demo("history.json", []))
    minutes = request.args.get("minutes", type=int)
    if not minutes or minutes <= 0:
        minutes = None
    max_points = min(request.args.get("max", 1500, type=int), 5000)
    return jsonify(store().history(minutes=minutes, max_points=max_points))


@app.route("/api/health")
def api_health():
    if DEMO:
        return jsonify(_demo("health.json", []))
    return jsonify(store().daily_health())


@app.route("/api/tiles", methods=["GET", "PUT"])
def api_tiles():
    """Tile order + enabled flags. PUT persists to web/tiles.json; the reader
    picks the change up on its next cycle and stops polling what nothing shows."""
    if DEMO:                                    # demo → default layout, read-only
        cfg = {"tiles": [dict(t, span=t.get("span", reader.DEFAULT_SPAN.get(t["id"], 3)))
                         for t in reader.DEFAULT_TILES["tiles"]]}
    elif request.method == "PUT":
        body = request.get_json(silent=True) or {}
        cfg = save_tiles(body)
    else:
        cfg = load_tiles()
    names = {t["id"]: t for t in reader.TILES}
    out = []
    for t in cfg["tiles"]:
        if t["id"] in names:
            out.append(dict(t, name=names[t["id"]]["name"], items=names[t["id"]]["items"]))
        else:
            sig = signals.SIGNALS.get(t.get("signal"), {})
            out.append(dict(t, name=t.get("title") or sig.get("label", t["id"]), items=[sig["item"]] if sig else []))
    return jsonify({"tiles": out})


@app.route("/api/calibration", methods=["GET", "PUT", "DELETE"])
def api_calibration():
    """Per-car offsets. PUT {"current_offset_a": x} sets one; PUT {"zero_current": true}
    takes the current raw reading as the new zero (do this with the car ON but
    not READY — contactors open, true current exactly 0). DELETE clears."""
    if DEMO:                                    # canned data → nothing to calibrate
        if request.method in ("PUT", "DELETE"):
            return jsonify({"error": "demo mode is read-only"}), 403
        return jsonify(_demo("calibration.json", {}))
    if request.method == "DELETE":
        return jsonify(save_calibration({}))
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        cal = load_calibration()
        if body.get("zero_current"):
            try:
                with open(STATE_FILE) as f:
                    st = json.load(f)
                raw = st.get("current_raw_a")
            except (FileNotFoundError, json.JSONDecodeError):
                raw = None
            if raw is None:
                return jsonify({"error": "no current reading yet"}), 409
            cal["current_offset_a"] = round(float(raw), 3)
            cal["current_zeroed_at"] = st.get("timestamp")
        if "current_offset_a" in body and body["current_offset_a"] is not None and not body.get("zero_current"):
            cal["current_offset_a"] = round(float(body["current_offset_a"]), 3)
        return jsonify(save_calibration(cal))
    return jsonify(load_calibration())


@app.route("/api/layouts", methods=["GET"])
def api_layouts():
    if DEMO:
        return jsonify({"layouts": _demo("layouts.json", [])})
    return jsonify({"layouts": list_layouts()})


@app.route("/api/layouts/<name>", methods=["PUT", "DELETE"])
def api_layout(name):
    if DEMO:
        return jsonify({"error": "demo mode is read-only"}), 403
    if request.method == "DELETE":
        return jsonify({"deleted": delete_layout(name)})
    body = request.get_json(silent=True) or {}
    try:
        saved = save_layout(name, body.get("tiles") and body or None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"name": name, "saved": saved["saved"], "tiles": len(saved["tiles"])})


@app.route("/api/layouts/<name>/load", methods=["POST"])
def api_layout_load(name):
    if DEMO:
        return jsonify({"error": "demo mode is read-only"}), 403
    try:
        cfg = load_layout(name)
    except KeyError:
        return jsonify({"error": "no such layout"}), 404
    return jsonify(cfg)


@app.route("/api/signals")
def api_signals():
    """Registry for the tile studio: every displayable signal, colour scales, tile types, items."""
    return jsonify({
        "signals": signals.SIGNALS,
        "colors": COLOR_SCALES,
        "types": TILE_TYPES,
        "items": {k: {"label": v["label"], "period": v["period"], "kind": v["kind"]} for k, v in reader.ITEMS.items()},
        "tile_defaults": reader.DEFAULT_SPAN,
        "vehicle": {"name": reader.VEHICLE.NAME, "title": reader.VEHICLE.TITLE},
        "demo": bool(DEMO),
    })


@app.route("/api/cells")
def api_cells():
    if DEMO:
        return jsonify(_demo("cells.json", []))
    limit = min(request.args.get("limit", 30, type=int), 500)
    return jsonify(store().cell_history(limit=limit))


READER = os.path.join(DIR, "reader.py")
PAUSE_FILE = os.path.join(DIR, "reader.pause")
_child = {"proc": None}


def run_reader_supervised(interval, adapter_pref, fast, budget=1.5, vehicle=None,
                          fixture=None, speed=None, db=None, scenario=None,
                          seed=None, knobs=None, sim_control=None, sim_serial=None):
    """Run reader.py as a child process and restart it whenever it exits.

    The reader lives in its own process because CoreBluetooth callbacks on a
    background thread segfaulted the combined process (2026-08-24). A crash
    now costs a few seconds of data, not the dashboard.
    """
    args = [sys.executable, "-u", READER, "--interval", str(interval), "--budget", str(budget)]
    if adapter_pref:
        args += ["--adapter", adapter_pref]
    if fast:
        args.append("--fast")
    if vehicle:
        args += ["--vehicle", vehicle]
    if fixture:
        args += ["--fixture", fixture]
    if speed:
        args += ["--speed", str(speed)]
    if scenario:
        args += ["--scenario", scenario]
    if seed is not None:
        args += ["--seed", str(seed)]
    for k in knobs or []:
        args += ["--knob", k]
    if sim_control is not None:             # 0 is a real request (a free port)
        args += ["--sim-control", str(sim_control)]
    if sim_serial:
        args += ["--sim-serial", sim_serial]
    if db:
        args += ["--db", db]
    backoff = 2
    while True:
        if os.path.exists(PAUSE_FILE):
            time.sleep(1)
            continue
        started = time.time()
        proc = subprocess.Popen(args, cwd=DIR)
        _child["proc"] = proc
        rc = proc.wait()
        _child["proc"] = None
        if os.path.exists(PAUSE_FILE):
            print("[reader] paused (web/reader.pause) — will relaunch when the file is removed", flush=True)
            continue
        if time.time() - started > 60:
            backoff = 2                         # ran fine for a while → reset
        print(f"[reader] exited rc={rc}; restarting in {backoff}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


def _kill_child(*_a):
    p = _child.get("proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()


def _shutdown(signum, _frame):
    """SIGTERM/SIGINT: take the reader down with us.

    atexit does not run on a signal, so a `kill` on the dashboard used to
    leave the reader child running — still holding the adapter (or, in replay,
    still writing the state file) with nothing supervising it.
    """
    _kill_child()
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)  # reader thread prints show up promptly in logs
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Ha-Kake — read-only OBD-II telemetry dashboard")
    ap.add_argument("--interval", type=float, default=0.5, help="Minimum seconds per reader cycle (default: 0.5)")
    ap.add_argument("--budget", type=float, default=1.5, help="Slow-lane seconds per cycle (default: 1.5)")
    ap.add_argument("--adapter", choices=["auto", "usb", "ble", "replay", "sim"], default="auto")
    ap.add_argument("--fast", action="store_true", help="Group-01-only power loop")
    ap.add_argument("--no-reader", action="store_true", help="Run dashboard only (use separate reader.py)")
    ap.add_argument("--vehicle", default=None, help="Vehicle profile in vehicles/ (default: leaf_ze0 or config.local.json)")
    ap.add_argument("--fixture", default=None, help="Replay session fixture (--adapter replay)")
    ap.add_argument("--speed", type=float, default=None, help="Replay/sim time scaling (2 = twice real time)")
    ap.add_argument("--scenario", default=None, help="Simulator scenario (--adapter sim)")
    ap.add_argument("--seed", type=int, default=None, help="Simulator RNG seed (--adapter sim)")
    ap.add_argument("--knob", action="append", default=[], metavar="NAME=VALUE",
                    help="Simulator knob at startup; repeatable (--adapter sim)")
    ap.add_argument("--sim-control", type=int, default=None, metavar="PORT",
                    help="Simulator control API port on 127.0.0.1, so conditions can be "
                         f"changed mid-run (--adapter sim). Default in sim mode: "
                         f"{DEFAULT_SIM_CONTROL_PORT}; 0 = a free port. With --sim-serial "
                         "it names the port the external rig already serves on")
    ap.add_argument("--no-sim-control", action="store_true",
                    help="Do not serve the simulator control API (--adapter sim)")
    ap.add_argument("--sim-serial", default=None, metavar="DEV",
                    help="Point the real serial transport at a simulator pty from "
                         "hakake_sim.py --pty (--adapter sim)")
    ap.add_argument("--demo", nargs="?", const=DEMO_DIR, default=None,
                    help="Serve canned JSON from a demo directory (default: docs/demo), no reader")
    ap.add_argument("--db", default=None, metavar="PATH",
                    help="Open this SQLite database instead of the profile's own. "
                         "The route for generated history: hakake_sim.py --generate "
                         "writes one, this opens it (with its _state.json sibling if "
                         "there is one). --adapter replay/sim still use their own file.")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    pref = None if args.adapter == "auto" else args.adapter
    if args.no_sim_control:
        args.sim_control = None
    elif pref == "sim" and args.sim_control is None and not args.sim_serial:
        # The one canonical command is `--adapter sim`: it must bring the
        # control API up too, or the cockpit page has nothing to drive.
        args.sim_control = DEFAULT_SIM_CONTROL_PORT
    if pref == "sim" and args.sim_control and not args.demo and not args.no_reader:
        SIM_CONTROL_URL = f"http://127.0.0.1:{args.sim_control}"
    if args.demo:
        DEMO = args.demo
    if args.vehicle:
        reader.set_vehicle(args.vehicle)
    print(f"Vehicle: {reader.VEHICLE.TITLE}")

    if args.db:
        # Someone else's database — generated history, an archive, a copy. The
        # dashboard reads it exactly as it reads the car's own file; nothing
        # else about the process changes.
        DB_PATH = os.path.abspath(args.db)
        sibling = (DB_PATH[:-3] if DB_PATH.endswith(".db") else DB_PATH) + "_state.json"
        print(f"Database: {DB_PATH}" + ("" if os.path.exists(DB_PATH) else "  (does not exist yet)"))
        if os.path.exists(sibling):
            STATE_FILE = sibling
            print(f"  state:  {STATE_FILE}")
        try:
            import sqlite3 as _sq3
            _c = _sq3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            if (_c.execute("SELECT value FROM meta WHERE key='synthetic'").fetchone() or [None])[0]:
                print("  *** SYNTHETIC DATABASE — generated history, not readings "
                      "from any vehicle ***")
            _c.close()
        except Exception:
            pass

    if DEMO:
        print(f"DEMO mode — serving canned data from {DEMO}, no reader, no database.")
    elif not args.no_reader:
        db = args.db
        if pref == "replay":
            # Replay rows are not readings from a car and must never land in
            # web/leaf_battery.db. Both the reader and this process read/write
            # the throwaway file instead.
            db = DB_PATH = reader.replay_db()
            STATE_FILE = reader.replay_state()
            print("REPLAY MODE — recorded fixture, not a car. Values below are playback, not live.")
            print(f"  fixture: {args.fixture or 'default for ' + reader.VEHICLE.NAME}")
            print(f"  database: {db} (throwaway — the real one is untouched)")
        if pref == "sim":
            # Generated rows are not readings from a car either. Same
            # throwaway database, same separate state file, same reason.
            db = DB_PATH = reader.sim_db()
            STATE_FILE = reader.sim_state()
            print("SIMULATOR MODE — a running model, not a car. Nothing below is a reading from any vehicle.")
            if args.sim_serial:
                print(f"  transport: real serial to a simulator pty at {args.sim_serial}")
            print(f"  scenario: {args.scenario or 'default'}   seed: {args.seed}")
            print(f"  database: {db} (throwaway — the real one is untouched)")
        print("Starting reader in background...")
        atexit.register(_kill_child)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _shutdown)
        threading.Thread(target=run_reader_supervised,
                         args=(args.interval, pref, args.fast, args.budget, args.vehicle,
                               args.fixture, args.speed, db, args.scenario, args.seed,
                               args.knob, args.sim_control, args.sim_serial),
                         daemon=True).start()
    else:
        print("Dashboard only — run reader.py separately.")

    print(f"\nHa-Kake dashboard   http://127.0.0.1:{args.port}")
    if pref == "sim" and not DEMO and not args.no_reader:
        # Three lines, one per thing a person wants to open. The cockpit page
        # is the dashboard's; the control API is the reader's (or the external
        # rig's, with --sim-serial).
        print(f"  simulator panel   http://127.0.0.1:{args.port}/sim")
        if args.sim_control:
            print(f"  control API       http://127.0.0.1:{args.sim_control}/sim/schema")
        elif args.sim_control == 0:
            print("  control API       on a free port — see sim_control_url in /api/status")
        elif args.sim_serial:
            print("  control API       none named; pass --sim-control <port> of the rig behind "
                  f"{args.sim_serial} to link it")
        else:
            print("  control API       off (--no-sim-control)")
    app.run(host="127.0.0.1", port=args.port, debug=False)
