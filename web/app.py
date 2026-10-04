#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ha-Kake Dashboard
Flask web server with integrated reader. The vehicle comes from --vehicle
(profiles in vehicles/; default the 2012 Leaf).

API:
  /api/status                    latest state (battery_state.json), trouble codes described
  /api/stream                    the same record pushed as Server-Sent Events each time the
                                 reader writes it (the live page's feed; 204 in demo mode)
  /api/history?minutes=1440      downsampled readings (omit or minutes=0 → all)
  /api/health                    per-day capacity / SOH / temps for degradation chart
  /api/cells?limit=30            per-cell voltages for the last N full reads
  /api/sessions                  recorded sessions (gaps in the data), newest first
  /api/playback/frames?from=&to= stored readings in an epoch range as playback frames
                                 (&max=3600 thins to the last row per bucket; &cells=1 joins cells)
  /api/bookmarks                 GET ?from&to / PUT {t,label} / DELETE ?t — timeline flags (web/bookmarks.json)
  /api/bookmarks/auto?from=&to=  discharge pulls found in the readings, as candidate flags
  /api/console?since=&kind=&ids=&limit=
                                 the raw output console's entries after an opaque
                                 cursor (docs/CONSOLE.md); a window, not a capture
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
  python app.py --adapter can        # a native USB-CAN adapter (CANable); docs/CAN_TRANSPORT.md
  python app.py --adapter mqtt       # frames from a Pi bridge through an MQTT broker; docs/MQTT.md
  python app.py --adapter sim        # no car: the simulated car, the dashboard and the
                                     # control API (127.0.0.1:8099) in one command; /sim
                                     # is the cockpit page
  python app.py --adapter sim --sim-control 0      # ... control API on a free port
  python app.py --adapter sim --no-sim-control     # ... no control API at all
  python app.py --demo               # canned JSON only (docs screenshots), no reader
  python app.py --interval 0.5       # min seconds per reader cycle (default: the adapter's own)
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
import math
import os
import signal
import subprocess
import sys
import threading
import time

from flask import Flask, Response, abort, jsonify, render_template, request, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from store import Store                 # noqa: E402
import console as consolelog           # the raw output console's file reader  # noqa: E402
import dtc                               # noqa: E402  (trouble-code dictionary; absent is normal)
import reader                            # noqa: E402  (vehicle-bound globals: reader.ITEMS etc.)
import signals                           # noqa: E402
from reader import (load_tiles, save_tiles, load_calibration, save_calibration,  # noqa: E402
                    list_layouts, save_layout, load_layout, delete_layout, layout_name,
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

# ── who may talk to this server ──────────────────────────────────────────
# The dashboard is served on loopback (app.run binds 127.0.0.1). Every request
# must also *name* a loopback host, and a request that changes something must
# come from a page on one (its Origin) — or, from a tool with no Origin such as
# curl or the tests, say it is sending JSON. HAKAKE_ALLOWED_HOSTS adds host
# names for someone who deliberately serves the page elsewhere (comma list).
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MUTATING = ("POST", "PUT", "DELETE", "PATCH")


def allowed_hosts():
    extra = env("HAKAKE_ALLOWED_HOSTS") or ""
    return set(LOOPBACK_HOSTS) | {h.strip().lower() for h in extra.split(",") if h.strip()}


def _hostname(netloc):
    """'127.0.0.1:5000' -> '127.0.0.1'; '[::1]:5000' -> '[::1]'; lower-cased."""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc[:netloc.find("]") + 1] if "]" in netloc else netloc
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


@app.before_request
def check_request_source():
    hosts = allowed_hosts()
    if _hostname(request.host) not in hosts:
        abort(403)
    if request.method in MUTATING:
        origin = request.headers.get("Origin")
        if origin is not None:
            scheme, _, rest = origin.partition("://")
            if scheme != "http" or _hostname(rest) not in hosts:
                abort(403)
        elif not request.is_json:
            abort(403)


# No third-party resource is loaded by either page; scripts and styles are this
# server's own (plus the pages' inline blocks). connect-src also allows the
# simulator's control API, which runs on another loopback port.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self' http://127.0.0.1:* http://localhost:*; "
       "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


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
    # the 3D pack tile's geometry rides along when the profile declares one
    pack = None
    if getattr(v, "PACK_LAYOUT", None):
        pack = {"module": v.PACK_MODULE, "case": v.PACK_CASE,
                "layout": v.PACK_LAYOUT, "sensors": getattr(v, "PACK_SENSORS", []),
                "modes": getattr(v, "PACK_MODES", [])}
    return {"name": v.NAME, "title": v.TITLE,
            "logo": getattr(v, "LOGO", "dial"),
            "level_key": level, "pack": pack}


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
    server-side; the page drops any the record still cannot drive. The
    profile's own TILES, not reader.TILES: a framework tile (the raw output
    console) describes the transport, and the cockpit has no partial for it.
    """
    return {"control_url": SIM_CONTROL_URL,
            "tiles": [t["id"] for t in reader.VEHICLE.TILES]}


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
    return jsonify(status_payload())


def status_payload():
    """The record /api/status serves and /api/stream pushes — one builder, so
    the two can never disagree about what the page is shown."""
    if DEMO:
        st = _demo("state.json", {"status": "waiting"})
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
        for k in ("timestamp", "last_ok", "state_time"):
            if k in st or k in ("timestamp", "last_ok"):
                st[k] = now                       # keep demo looking live (fresh clock, green dot, pulse)
        st["demo"] = True                         # canned data — say so in the payload
        return st
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
    # Trouble codes get their descriptions here, on the way out — never on the
    # way in. What the reader stores is the raw code, which is what was read
    # off the car; the dictionary is machine-local, optional and may change
    # under a running reader (docs/DTC_DICTIONARY.md). With no dictionary this
    # is a no-op and the page shows the bare codes exactly as before.
    dtc.enrich(state, signals.SIGNALS, vehicle=reader.VEHICLE)
    return state


# How often the stream looks at the state file, and how long it may stay
# silent before it sends a comment line. The look is one stat() call; the
# reader replaces the file atomically (reader.write_state), so a changed
# mtime is a whole new record. The heartbeat is what tells a threaded server
# a closed tab has gone: a write to a dead socket ends the generator.
STREAM_TICK = 0.02
STREAM_HEARTBEAT = 10.0


def _state_mtime():
    try:
        return os.stat(STATE_FILE).st_mtime_ns
    except OSError:
        return None


def stream_events(tick=None, heartbeat=None, clock=time.monotonic, sleep=time.sleep):
    """Server-Sent Events: one `data:` line per state-file write, carrying
    exactly what /api/status would have returned at that moment.

    Push, not poll: the page used to fetch /api/status once a second and so
    drew one cell read in two or three once the CANable made a read every
    ~0.4 s (2026-10-03). The server still reads the reader's file — the
    reader is a separate process, and that is deliberate (docs/ARCHITECTURE.md)
    — but it looks every STREAM_TICK and sends only on a change. Loopback
    only, like every other route: app.run binds 127.0.0.1."""
    tick = STREAM_TICK if tick is None else tick
    heartbeat = STREAM_HEARTBEAT if heartbeat is None else heartbeat
    yield "retry: 2000\n\n"                     # EventSource's reconnect delay, ms
    last, quiet = object(), clock()
    while True:
        m = _state_mtime()
        if m != last:
            last = m
            yield "data: " + app.json.dumps(status_payload()) + "\n\n"
            quiet = clock()
        elif clock() - quiet >= heartbeat:
            yield ": keepalive\n\n"
            quiet = clock()
        sleep(tick)


@app.route("/api/stream")
def api_stream():
    if DEMO:
        # 204 tells EventSource not to reconnect; the page keeps its 1 s fetch
        # of the canned record, which re-stamps the clock on every request.
        return Response(status=204)
    return Response(stream_with_context(stream_events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/history")
def api_history():
    if DEMO:
        return jsonify(_demo("history.json", []))
    minutes = request.args.get("minutes", type=int)
    if not minutes or minutes <= 0:
        minutes = None
    max_points = max(2, min(request.args.get("max", 1500, type=int), 5000))
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
            off = _offset_or_none(raw)
            if off is None:
                return jsonify({"error": "no current reading yet"}), 409
            cal["current_offset_a"] = off
            cal["current_zeroed_at"] = st.get("timestamp")
        if "current_offset_a" in body and body["current_offset_a"] is not None and not body.get("zero_current"):
            off = _offset_or_none(body["current_offset_a"])
            if off is None:
                return jsonify({"error": f"current_offset_a must be a number within ±{MAX_OFFSET_A:g} A"}), 400
            cal["current_offset_a"] = off
        return jsonify(save_calibration(cal))
    return jsonify(load_calibration())


# A current-sensor zero offset is a few amps at most; anything outside this is a
# typo (or not a number at all) and is refused rather than stored.
MAX_OFFSET_A = 50.0


def _offset_or_none(v):
    """A finite offset within ±MAX_OFFSET_A, rounded to the mA; None otherwise."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, 3) if math.isfinite(v) and abs(v) <= MAX_OFFSET_A else None


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
        name = layout_name(name)
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
        # `can_id` is the id this item puts on the wire (a passive item's own,
        # a UDS item's response header): the raw output console's "known ids
        # only" toggle is built from it, so that list is the profile's.
        "items": {k: {"label": v["label"], "period": v["period"], "kind": v["kind"],
                      "can_id": reader.item_can_id(k)} for k, v in reader.ITEMS.items()},
        "tile_defaults": reader.DEFAULT_SPAN,
        "tile_signals": signals.tile_signals(reader.TILES),
        "vehicle": {"name": reader.VEHICLE.NAME, "title": reader.VEHICLE.TITLE},
        "demo": bool(DEMO),
    })


# ── the raw output console (docs/CONSOLE.md) ─────────────────────────────

@app.route("/api/console")
def api_console():
    """Console entries after an opaque cursor.

    `since` is a cursor and not a timestamp, because two frames can share a
    millisecond. `kind` and `ids` are comma-separated filters; `limit` caps the
    answer to the newest matches, so a pane that fell behind gets the end of the
    stream rather than the start of a backlog. The reader writes the file
    (web/console.jsonl, gitignored, size-capped) only while the tile is on; when
    it is off the file is absent and this answers an empty window, which is the
    honest thing rather than a 404.

    `stats` rides along from the state file — kept, dropped, dropped per id, and
    the `partial` line when the transport can only show part of the bus. A pane
    that silently thinned its own data would be worse than no pane.
    """
    if DEMO:
        return jsonify({"entries": [], "cursor": "0", "stats": {"on": False, "demo": True}})
    since = request.args.get("since", 0, type=int) or 0
    limit = min(max(request.args.get("limit", consolelog.LIMIT_DEFAULT, type=int), 1),
                consolelog.LIMIT_MAX)
    kinds = [k for k in (request.args.get("kind") or "").split(",") if k in consolelog.KINDS] or None
    ids = [i.strip().upper() for i in (request.args.get("ids") or "").split(",") if i.strip()] or None
    log = consolelog.ConsoleLog(reader.CONSOLE_FILE)
    entries = log.read(since=since, kind=kinds, ids=ids, limit=limit)
    try:
        with open(STATE_FILE) as f:
            stats = json.load(f).get("console") or {"on": False}
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        stats = {"on": False}
    # The cursor to ask with next time: the newest entry served, or the caller's
    # own — never the ring's, which may be ahead of what this file holds.
    cursor = str(entries[-1]["seq"]) if entries else str(since)
    return jsonify({"entries": entries, "cursor": cursor, "stats": stats,
                    "kinds": list(consolelog.KINDS)})


@app.route("/api/cells")
def api_cells():
    if DEMO:
        return jsonify(_demo("cells.json", []))
    limit = max(1, min(request.args.get("limit", 30, type=int), 500))
    return jsonify(store().cell_history(limit=limit))


# ── timeline bookmarks: flags the owner drops on a moment (docs/PLAYBACK.md) ──

def _mine(items):
    return [b for b in items if b.get("vehicle") in ("", reader.VEHICLE.NAME)]


@app.route("/api/bookmarks", methods=["GET", "PUT", "DELETE"])
def api_bookmarks():
    """GET ?from&to lists flags (epoch seconds); PUT {t?, label?} adds one (t defaults
    to now); DELETE ?t= removes one. Kept in web/bookmarks.json, gitignored."""
    if DEMO:
        if request.method != "GET":
            return jsonify({"error": "demo mode is read-only"}), 403
        return jsonify({"bookmarks": _demo("bookmarks.json", [])})
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        t = body.get("t")
        try:
            t = float(t) if t is not None else time.time()
        except (TypeError, ValueError):
            return jsonify({"error": "t must be epoch seconds"}), 400
        items = reader.add_bookmark(t, body.get("label", ""))
        return jsonify({"bookmarks": _mine(items), "added": round(t, 3)})
    if request.method == "DELETE":
        t = request.args.get("t", type=float)
        if t is None:
            return jsonify({"error": "t is required"}), 400
        return jsonify({"deleted": reader.delete_bookmark(t)})
    t0 = request.args.get("from", type=float)
    t1 = request.args.get("to", type=float)
    items = _mine(reader.load_bookmarks())
    if t0 is not None:
        items = [b for b in items if b["t"] >= t0]
    if t1 is not None:
        items = [b for b in items if b["t"] <= t1]
    return jsonify({"bookmarks": items})


@app.route("/api/bookmarks/auto")
def api_bookmarks_auto():
    """Discharge pulls found in the readings (runs below -amps A, default 40),
    as candidate flags for the timeline."""
    if DEMO:
        return jsonify({"pulls": _demo("pulls.json", []), "amps": 40})
    t0 = request.args.get("from", type=float)
    t1 = request.args.get("to", type=float)
    if t0 is None or t1 is None or t1 < t0:
        return jsonify({"error": "from and to are required epoch seconds, from <= to"}), 400
    amps = min(max(request.args.get("amps", 40.0, type=float), 5.0), 500.0)
    return jsonify({"pulls": store().pulls(t0, t1, amps=amps), "amps": amps})


# ── playback: the dashboard replaying what it recorded (docs/PLAYBACK.md) ──

@app.route("/api/sessions")
def api_sessions():
    """Recorded sessions, newest first, derived from gaps in the data."""
    if DEMO:
        return jsonify(_demo("sessions.json", []))
    gap = min(max(request.args.get("gap", 600, type=int), 60), 86400)
    return jsonify(store().sessions(gap_s=gap))


@app.route("/api/playback/frames")
def api_playback_frames():
    """Stored readings in an epoch range as playback frames: status-shaped
    records, history-shaped rows, and which frames carry cells. `max` thins to
    the last real row per bucket (never an average); `cells=1` joins the cell
    voltages, which multiply the payload by six, so ask only when a cells
    consumer is on screen."""
    if DEMO:
        return jsonify(_demo("frames.json", {"t": [], "records": [], "hist": [], "cells_at": []}))
    t0 = request.args.get("from", type=float)
    t1 = request.args.get("to", type=float)
    if t0 is None or t1 is None or t1 < t0:
        return jsonify({"error": "from and to are required epoch seconds, from <= to"}), 400
    max_points = min(max(request.args.get("max", 3600, type=int), 1), 3600)
    cells = request.args.get("cells", 0, type=int) == 1
    return jsonify(store().frames(t0, t1, max_points=max_points, cells=cells))


READER = os.path.join(DIR, "reader.py")
PAUSE_FILE = os.path.join(DIR, "reader.pause")
_child = {"proc": None}


def run_reader_supervised(interval, adapter_pref, fast, budget=1.5, vehicle=None,
                          fixture=None, speed=None, db=None, scenario=None,
                          seed=None, knobs=None, sim_control=None, sim_serial=None,
                          sim_can=None):
    """Run reader.py as a child process and restart it whenever it exits.

    The reader lives in its own process because CoreBluetooth callbacks on a
    background thread segfaulted the combined process (2026-08-24). A crash
    now costs a few seconds of data, not the dashboard.
    """
    args = [sys.executable, "-u", READER, "--budget", str(budget)]
    if interval is not None:
        args += ["--interval", str(interval)]
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
    if sim_can:
        args += ["--sim-can", sim_can]
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
    ap.add_argument("--interval", type=float, default=None,
                    help="Minimum seconds per reader cycle (default: the adapter's own — 0.5 for an ELM327, 0 for native CAN)")
    ap.add_argument("--budget", type=float, default=1.5, help="Slow-lane seconds per cycle (default: 1.5)")
    ap.add_argument("--adapter", choices=["auto", "usb", "ble", "replay", "sim", "can", "mqtt"], default="auto")
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
    ap.add_argument("--sim-can", nargs="?", const="car", choices=["car", "ev"], default=None,
                    metavar="BUS",
                    help="With --adapter sim: the model's ECUs on an in-process virtual CAN bus "
                         "behind the native CAN façade, at the real frame rate (≈1,700 frames/s); "
                         "'ev' also starts the EV-CAN channel. docs/SIMULATOR.md")
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
            if args.sim_can:
                print(f"  transport: simulated CAN bus (python-can virtual, the model's ECUs at the "
                      f"real frame rate; channels car{' + ev' if args.sim_can == 'ev' else ''})")
            print(f"  scenario: {args.scenario or 'default'}   seed: {args.seed}")
            print(f"  database: {db} (throwaway — the real one is untouched)")
        print("Starting reader in background...")
        atexit.register(_kill_child)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _shutdown)
        threading.Thread(target=run_reader_supervised,
                         args=(args.interval, pref, args.fast, args.budget, args.vehicle,
                               args.fixture, args.speed, db, args.scenario, args.seed,
                               args.knob, args.sim_control, args.sim_serial, args.sim_can),
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
