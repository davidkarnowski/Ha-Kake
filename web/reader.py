#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Reader Daemon — vehicle-agnostic (profiles in vehicles/).

Polls the car over BLE or USB, decodes with the active vehicle profile
(default: the 2012 Leaf, vehicles/leaf_ze0.py), writes the latest
merged record to battery_state.json (for the dashboard) and periodic rows to
the SQLite store (web/leaf_battery.db — never pruned).

Scheduling
----------
Every signal source is an *item* (an LBC/HVAC UDS group or a passive Car-CAN
capture) with a period. Items with period 0 form the **fast lane** and run
every cycle; the rest are **round-robin by overdue ratio** inside a small
per-cycle time budget. Only items needed by tiles that are enabled in
web/tiles.json are polled at all — disabling a tile hands its bandwidth to
the others. The file is re-read whenever its mtime changes.

Resilience
----------
  * Supervisor loop: any transport error → status "reconnecting", exponential
    back-off (2 s → 30 s), re-detect adapter, re-run the profile's configure().
  * Car asleep (LBC silent) → status "asleep", 60 s heartbeat.
  * web/reader.pause → exit cleanly (status "paused"); app.py relaunches
    when the file is removed. Calibration tools borrow the adapter this way.
  * battery_state.json always keeps the last good reading plus `last_ok`.

Usage:
  python reader.py                    # auto-detect adapter
  python reader.py --adapter ble      # force BLE
  python reader.py --adapter replay   # no car: play back a recorded session
  python reader.py --adapter sim      # no car: run against the vehicle simulator
  python reader.py --adapter can      # a native USB-CAN adapter (CANable); config names the bus
  python reader.py --adapter mqtt     # frames from a bridge through an MQTT broker
  python reader.py --interval 1       # minimum seconds per cycle (default: the adapter's MIN_INTERVAL)
  python reader.py --budget 1.5       # seconds of slow-lane work per cycle
  python reader.py --vehicle lancer_2009   # a different vehicle profile
"""

import argparse
import asyncio
import datetime as dt
import json
import functools
import os
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from elm327 import detect_adapter, set_uds_target, passive_capture, load_local_config  # noqa: E402
from store import Store, utc_now_iso, ev_norm                       # noqa: E402
import mqttsource            # <prefix>/state + signal/<key> for gauges; no-op without mqtt.host  # noqa: E402
from vehicles import (get_vehicle, peak_keys, buses, item_bus, primary_bus, source_specs,  # noqa: E402
                      tiles as vehicle_tiles, default_span, default_tiles)
import signals                                                      # noqa: E402
import console as consolelog        # the raw output console's ring and file (docs/CONSOLE.md)  # noqa: E402

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "battery_state.json")
PAUSE_FILE = os.path.join(DIR, "reader.pause")
TILES_FILE = os.path.join(DIR, "tiles.json")
CALIB_FILE = os.path.join(DIR, "calibration.json")   # per-car offsets (gitignored)
CONSOLE_FILE = os.path.join(DIR, "console.jsonl")    # the raw output console's window (gitignored)
# Replay writes to its own files, per profile: it must never touch the real
# database (years of irreplaceable readings) or the last real state, and two
# profiles have different `readings` columns, so they get different files.


def replay_db(vehicle=None):
    return os.path.join(DIR, f"replay_{vehicle or VEHICLE.NAME}.db")


def replay_state(vehicle=None):
    return os.path.join(DIR, f"replay_{vehicle or VEHICLE.NAME}_state.json")


# Simulator mode is under the same rule, for the same reason: generated rows
# are not readings, and mixing them into web/leaf_battery.db would poison
# 12,000+ irreplaceable ones from two real cars.

def sim_db(vehicle=None):
    return os.path.join(DIR, f"sim_{vehicle or VEHICLE.NAME}.db")


def sim_state(vehicle=None):
    return os.path.join(DIR, f"sim_{vehicle or VEHICLE.NAME}_state.json")
LAYOUTS_FILE = os.path.join(DIR, "layouts.json")     # named tile layouts (gitignored)
BOOKMARKS_FILE = os.path.join(DIR, "bookmarks.json") # timeline flags (gitignored)
BOOKMARK_LABEL_MAX = 80
# The simulator cockpit (/sim) keeps its own arrangement here (gitignored).
# It is not web/tiles.json because that store is vehicle-shaped: _clean_tile()
# drops any id the active profile's TILES does not declare, and the cockpit's
# cards (cluster, knob categories, time, state) are the page's, not a car's.
SIM_TILES_FILE = os.path.join(DIR, "sim_tiles.json")

ASLEEP_INTERVAL = 60
# The minimum cycle period for a transport that does not set MIN_INTERVAL.
# 0.5 s keeps an ELM327 from spinning when only cheap items are due; the
# native CAN façade sets 0, because there the cycle IS the requests (the
# LBC's own ~10 ms frame pacing) and padding it only delays the next cell
# read. An explicit --interval overrides either.
DEFAULT_INTERVAL = 0.5


def interval_for(elm, explicit=None):
    """The minimum cycle period: `explicit` (--interval) when given, else the
    transport's MIN_INTERVAL, else DEFAULT_INTERVAL."""
    if explicit is not None:
        return float(explicit)
    v = getattr(elm, "MIN_INTERVAL", None)
    return DEFAULT_INTERVAL if v is None else float(v)
BACKOFF_MIN, BACKOFF_MAX = 2, 8
MAX_DETECT_ATTEMPTS = 1   # after this many failed reconnects, exit so app.py relaunches a
                          # FRESH process — macOS leaves CoreBluetooth broken across sleep,
                          # and only a new process can scan/see the adapter again.
STORE_PERIOD = 5.0          # seconds between SQLite rows (state file updates every cycle)

# ── Active vehicle profile ───────────────────────────────────────────────
# Everything vehicle-specific — items, tiles, signal registry, decode and
# sensor policy — lives in vehicles/<profile>.py. set_vehicle() binds the
# profile to this module's globals so app.py and the tests read them as
# reader.ITEMS, reader.TILES, ...

VEHICLE = None
ITEMS, TILES, DEFAULT_SPAN, DEFAULT_TILES, ITEM_KEYS = {}, [], {}, {"tiles": []}, {}
WATCH, KIND_ORDER, TARGETS, FAST_ONLY = (), (), {}, set()
PEAK_KEYS = []                            # record keys whose envelope is kept between rows
BUSES, PRIMARY_BUS = ("car",), "car"      # the profile's buses; the one whose silence means asleep
SOURCE_SPECS = {}                         # canonical key → {sources, tolerance} for the resolver


async def configure_vehicle(elm):        # module-level so tests can monkeypatch it
    await VEHICLE.configure(elm)


def set_vehicle(name=None):
    """Bind a vehicle profile (vehicles/<name>.py) to this module and the signal registry."""
    global VEHICLE, ITEMS, TILES, DEFAULT_SPAN, DEFAULT_TILES, ITEM_KEYS
    global WATCH, KIND_ORDER, TARGETS, FAST_ONLY, PEAK_KEYS, BUSES, PRIMARY_BUS, SOURCE_SPECS
    VEHICLE = get_vehicle(name)
    PEAK_KEYS = peak_keys(VEHICLE)
    BUSES = buses(VEHICLE)
    PRIMARY_BUS = primary_bus(VEHICLE)
    SOURCE_SPECS = source_specs(VEHICLE)
    ITEMS = VEHICLE.ITEMS
    # The profile's built-in tiles plus the framework's (vehicles/__init__.py
    # FRAMEWORK_TILES: the raw output console, which describes the transport and
    # not a car). Everything downstream — _clean_tile, enabled_items,
    # period_overrides, /api/tiles, /api/signals — reads these merged views, so
    # nothing else has to know the difference.
    TILES = vehicle_tiles(VEHICLE)
    DEFAULT_SPAN = default_span(VEHICLE)
    DEFAULT_TILES = {"tiles": [dict(t) for t in default_tiles(VEHICLE)]}
    ITEM_KEYS = VEHICLE.ITEM_KEYS
    WATCH = VEHICLE.WATCH
    KIND_ORDER = VEHICLE.KIND_ORDER
    TARGETS = VEHICLE.TARGETS
    FAST_ONLY = set(VEHICLE.FAST_ONLY)
    signals.use(VEHICLE)
    return VEHICLE


set_vehicle()

# tile-config fields persisted to web/tiles.json (vehicle-independent)
TILE_FIELDS = ("id", "enabled", "span", "kind", "signal", "type", "opts", "title", "x", "y", "h")


ALERT_REPEAT_MIN, ALERT_REPEAT_MAX, ALERT_REPEAT_DEFAULT = 1, 60, 10   # mirrors Alerts.REPEAT in alerts.js


def _num_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _clean_alerts(raw):
    """Validate a tile's `opts.alerts` — the audible threshold rules the ⋯
    menu writes, one per (tile, signal): {signal, min, max, when, tone,
    repeat, enabled}. Unknown signals, unknown keys and rules with no
    threshold at all are dropped; numbers are coerced or become None. The
    browser evaluates these; the server only keeps them well-formed."""
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or r.get("signal") not in signals.SIGNALS:
            continue
        c = {"signal": r["signal"], "min": _num_or_none(r.get("min")), "max": _num_or_none(r.get("max"))}
        if r.get("when") in ("on", "off"):
            c["when"] = r["when"]
        if c["min"] is None and c["max"] is None and "when" not in c:
            continue
        if isinstance(r.get("tone"), str):
            c["tone"] = r["tone"][:20]
        try:                                    # the menu's slider: every 1–60 s (default 10)
            c["repeat"] = min(ALERT_REPEAT_MAX, max(ALERT_REPEAT_MIN, int(r.get("repeat", ALERT_REPEAT_DEFAULT))))
        except (TypeError, ValueError):
            c["repeat"] = ALERT_REPEAT_DEFAULT
        c["enabled"] = bool(r.get("enabled", True))
        out.append(c)
    return out


def _clean_opts(out):
    """Copy a tile's opts and normalise `alerts` in place (shared by the
    dashboard and cockpit stores)."""
    opts = dict(out["opts"]) if isinstance(out.get("opts"), dict) else {}
    if "alerts" in opts:
        alerts = _clean_alerts(opts.pop("alerts"))
        if alerts:
            opts["alerts"] = alerts
    if opts or "opts" in out:
        out["opts"] = opts


# ── settings files: one writer at a time, never a half-written file ──────
# Flask serves requests on several threads, and the tile menu can save on
# every change; two saves must not interleave. Each write goes to its own
# temporary file in the same directory and replaces the target in one step,
# and every read-modify-write of a settings file holds SETTINGS_LOCK (an RLock:
# save_layout calls save_tiles). The reader process only reads these files.
SETTINGS_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with SETTINGS_LOCK:
            return fn(*a, **kw)
    return wrapper


def _atomic_write_json(path, obj, indent=1, fsync=True):
    folder, base = os.path.split(path)
    fd, tmp = tempfile.mkstemp(dir=folder or ".", prefix=base + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=indent)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Text people type (tile titles, layout names, flag labels) is stored cleaned:
# control characters, zero-width and joiner characters, and bidirectional
# controls are dropped — they change how text displays without being visible —
# runs of spaces collapse, and the result is cut to a length. Markup characters
# are kept: "pull <40 A" is a fine label, and escaping on output is what makes
# any label safe to show (web/static/html.js).
_INVISIBLE = re.compile("[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e"
                        "\u2060-\u2064\u2066-\u2069\ufeff]")


def clean_label(s, max_len):
    """One line of user text, cleaned for storage (see above); '' for None."""
    if s is None:
        return ""
    s = re.sub(r"[\t\n\r\v\f]+", " ", str(s))          # line breaks become spaces, not glue
    s = _INVISIBLE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()[:max_len].strip()


def layout_name(name):
    """The stored form of a layout name; the same rule for save, load and delete."""
    name = clean_label(name, 60)
    if not name:
        raise ValueError("layout name required")
    return name


def _clean_tile(t):
    """Validate one tile entry; returns None if it is not usable."""
    if not isinstance(t, dict) or not isinstance(t.get("id"), str):
        return None
    known = {x["id"] for x in TILES}
    out = {k: t[k] for k in TILE_FIELDS if k in t}
    out["enabled"] = bool(t.get("enabled", True))
    if out["id"] in known:
        out["kind"] = "builtin"
        out.setdefault("span", DEFAULT_SPAN[out["id"]])
    else:
        # user tile: must reference a known signal
        if t.get("kind", "signal") != "signal" or t.get("signal") not in signals.SIGNALS:
            return None
        out["kind"] = "signal"
        out.setdefault("type", "number")
        out.setdefault("span", 3)
    try:
        out["span"] = int(out["span"])
    except (TypeError, ValueError):
        out["span"] = 3
    out["span"] = min(12, max(2, out["span"]))
    if "title" in out:
        out["title"] = clean_label(out["title"], 80)
        if not out["title"]:
            del out["title"]
    for k, lo, hi in (("x", 0, 10), ("y", 0, 10000), ("h", 2, 200)):
        if k in out:
            try:
                out[k] = min(hi, max(lo, int(out[k])))
            except (TypeError, ValueError):
                del out[k]
    if "x" in out:
        out["x"] = min(out["x"], 12 - out["span"])
    _clean_opts(out)
    return out


def load_tiles():
    """web/tiles.json (v2: order, enabled, span, type, opts, user signal tiles)
    merged over the defaults; unknown built-in ids ignored, missing ones appended."""
    raw = []
    try:
        with open(TILES_FILE) as f:
            raw = json.load(f).get("tiles", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass
    ordered, seen = [], set()
    for t in raw:
        c = _clean_tile(t)
        if c and c["id"] not in seen:
            ordered.append(c)
            seen.add(c["id"])
    for t in DEFAULT_TILES["tiles"]:
        if t["id"] in seen:
            continue
        c = _clean_tile(dict(t))
        if c:
            ordered.append(c)
    return {"tiles": ordered}


@_locked
def save_tiles(cfg):
    ordered, seen = [], set()
    for t in (cfg or {}).get("tiles", []):
        c = _clean_tile(t)
        if c and c["id"] not in seen:
            ordered.append(c)
            seen.add(c["id"])
    for t in DEFAULT_TILES["tiles"]:
        if t["id"] in seen:
            continue
        c = _clean_tile(dict(t))
        if c:
            ordered.append(c)
    _atomic_write_json(TILES_FILE, {"tiles": ordered})
    return {"tiles": ordered}


# ── simulator cockpit layout ─────────────────────────────────────────────
# Shape-only validation, no id filtering: the page discovers its cards from
# the DOM (TileStudio discover:true) and may add signal tiles, so the server
# only guarantees that what comes back is a list of well-typed entries.

def _clean_sim_tile(t):
    """One cockpit tile entry with its types enforced, or None if unusable.

    Keeps the same field set web/tiles.json uses so the one Tile Studio
    engine reads both stores; the difference is that no id is rejected.
    """
    if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not t["id"]:
        return None
    out = {k: t[k] for k in TILE_FIELDS if k in t}
    out["enabled"] = bool(t.get("enabled", True))
    for k, lo, hi in (("span", 2, 12), ("x", 0, 11), ("y", 0, 10000), ("h", 1, 200)):
        if k in out:
            try:
                out[k] = min(hi, max(lo, int(out[k])))
            except (TypeError, ValueError):
                del out[k]
    if "x" in out and "span" in out:
        out["x"] = min(out["x"], 12 - out["span"])
    for k in ("kind", "signal", "type", "title"):
        if k in out and not isinstance(out[k], str):
            del out[k]
    _clean_opts(out)
    return out


def _sim_tiles_from(raw):
    ordered, seen = [], set()
    for t in raw if isinstance(raw, list) else []:
        c = _clean_sim_tile(t)
        if c and c["id"] not in seen:
            ordered.append(c)
            seen.add(c["id"])
    return {"tiles": ordered}


def load_sim_tiles():
    """web/sim_tiles.json, or an empty layout — the page then auto-places."""
    try:
        with open(SIM_TILES_FILE) as f:
            raw = json.load(f).get("tiles", [])
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        raw = []
    return _sim_tiles_from(raw)


@_locked
def save_sim_tiles(cfg):
    out = _sim_tiles_from((cfg or {}).get("tiles", []) if isinstance(cfg, dict) else [])
    _atomic_write_json(SIM_TILES_FILE, out)
    return out


# ── named layouts ────────────────────────────────────────────────────────

def _read_layouts():
    try:
        with open(LAYOUTS_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_layouts(d):
    _atomic_write_json(LAYOUTS_FILE, d)


def list_layouts():
    d = _read_layouts()
    return sorted(({"name": k, "saved": v.get("saved"), "tiles": len(v.get("tiles", []))} for k, v in d.items()),
                  key=lambda x: x["name"].lower())


@_locked
def save_layout(name, cfg=None):
    """Store a layout under `name` (current web/tiles.json when cfg is None)."""
    name = layout_name(name)
    tiles = save_tiles(cfg)["tiles"] if cfg is not None else load_tiles()["tiles"]
    d = _read_layouts()
    d[name] = {"saved": utc_now_iso(), "tiles": tiles}
    _write_layouts(d)
    return d[name]


@_locked
def load_layout(name):
    """Make a saved layout the active one (writes web/tiles.json)."""
    try:
        name = layout_name(name)
    except ValueError:
        raise KeyError(name) from None
    d = _read_layouts()
    if name not in d:
        raise KeyError(name)
    return save_tiles({"tiles": d[name].get("tiles", [])})


@_locked
def delete_layout(name):
    try:
        name = layout_name(name)
    except ValueError:
        return False
    d = _read_layouts()
    if name in d:
        del d[name]
        _write_layouts(d)
        return True
    return False


def enabled_items(tiles_cfg, fast_only=False):
    """Items needed by enabled tiles — built-in tiles via TILES, user tiles via the signal registry."""
    if fast_only:
        return set(FAST_ONLY)
    builtin = {t["id"]: t for t in TILES}
    items = set()
    for t in tiles_cfg["tiles"]:
        if not t.get("enabled", True):
            continue
        if t["id"] in builtin:
            items.update(builtin[t["id"]]["items"])
        elif t.get("signal"):
            it = signals.signal_item(t["signal"])
            if it:
                items.add(it)
    return items


# A tile option that changes *how often* an item is polled, not just whether.
# `opts.celllog` on an enabled built-in tile that polls the cell voltages puts
# lbc02 in the fast lane — every cycle instead of every 20 s — and the main loop
# stores every fresh read. It is the same read-only request, more often; it is
# how a drive log gets cell voltages at the resolution an acceleration event
# needs (docs/PACK3D.md, docs/PLAYBACK.md).
CELLLOG_OPT = "celllog"
CELLLOG_ITEM = "lbc02"


def period_overrides(tiles_cfg):
    """{item: period} overrides requested by tile options — today only the cell log."""
    builtin = {t["id"]: t for t in TILES}
    out = {}
    for t in tiles_cfg.get("tiles", []):
        if not t.get("enabled", True) or t["id"] not in builtin:
            continue
        if (t.get("opts") or {}).get(CELLLOG_OPT) and CELLLOG_ITEM in builtin[t["id"]]["items"]:
            out[CELLLOG_ITEM] = 0
    return out


# ── the raw output console (docs/CONSOLE.md) ──────────────────────────────
# A framework tile (vehicles/__init__.py FRAMEWORK_TILES), off by default. The
# reader learns from web/tiles.json that it is on — the same mtime path
# period_overrides() uses — and only then taps the transports. No tile, no
# object, no tap, no file: enabling it is the only thing that costs anything.
#
# Its two options live in the tile's `opts`, like the cell log's:
#   everything  show every id on the bus and say that it is lossy
#   rate        frames of one id kept per second (the per-id cap)

CONSOLE_TILE = "console"
CONSOLE_RATE_MIN, CONSOLE_RATE_MAX = 1, 200
# What an ELM327 can honestly show, said in the pane itself. It is not a
# limitation of this code: the adapter only sees the ids the profile filtered
# for, only while ATMA is running, plus whatever it says back.
CONSOLE_PARTIAL_ELM = ("ELM327: only the ids this profile polls, and only during each ATMA dwell, "
                       "plus the adapter's own replies. A partial view of the bus.")


def console_options(tiles_cfg):
    """The console tile's options, or None when the tile is not enabled."""
    for t in (tiles_cfg or {}).get("tiles", []):
        if t.get("id") != CONSOLE_TILE:
            continue
        if not t.get("enabled", True):
            return None
        opts = t.get("opts") or {}
        try:
            rate = int(opts.get("rate", consolelog.RATE_CAP))
        except (TypeError, ValueError):
            rate = consolelog.RATE_CAP
        return {"everything": bool(opts.get("everything")),
                "rate": min(CONSOLE_RATE_MAX, max(CONSOLE_RATE_MIN, rate))}
    return None


def item_can_id(i):
    """The CAN id an item puts on the wire: a passive item's own id, a UDS
    item's response header, "" for neither. The console filters by these, and
    /api/signals serves them so the tile's "known ids only" toggle is the
    profile's own list rather than a hardcoded one."""
    it = ITEMS.get(i)
    if not it:
        return ""
    tgt = TARGETS.get(it["kind"])
    if tgt is None:
        return str(it.get("id", "")).upper()
    return str(tgt[1]).upper() if tgt else ""


def console_ids(items):
    """What the console shows by default — the ids the enabled tiles poll,
    which is 10–15 of them rather than the whole bus."""
    return {c for c in (item_can_id(i) for i in items) if c}


# ── timeline bookmarks ────────────────────────────────────────────────────
# A flag the owner drops on a moment — "start of the pull" — kept in
# web/bookmarks.json like the other per-machine files, so playback can jump
# to it. Each is {t: epoch seconds, label, kind: "user", vehicle}.

def _clean_bookmark(b):
    if not isinstance(b, dict):
        return None
    try:
        t = float(b.get("t"))
    except (TypeError, ValueError):
        return None
    if not (0 < t < 4e9):
        return None
    label = str(b.get("label") or "")[:BOOKMARK_LABEL_MAX].strip()
    return {"t": round(t, 3), "label": label, "kind": "user",
            "vehicle": str(b.get("vehicle") or (VEHICLE.NAME if VEHICLE else ""))}


def load_bookmarks():
    try:
        with open(BOOKMARKS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    items = data.get("bookmarks", []) if isinstance(data, dict) else []
    out = [c for c in (_clean_bookmark(b) for b in items) if c]
    return sorted(out, key=lambda b: b["t"])


@_locked
def save_bookmarks(items):
    out = sorted([c for c in (_clean_bookmark(b) for b in items) if c], key=lambda b: b["t"])
    _atomic_write_json(BOOKMARKS_FILE, {"bookmarks": out})
    return out


@_locked
def add_bookmark(t, label=""):
    items = load_bookmarks()
    items.append({"t": t, "label": clean_label(label, 120)})
    return save_bookmarks(items)


@_locked
def delete_bookmark(t):
    items = load_bookmarks()
    keep = [b for b in items if abs(b["t"] - float(t)) > 0.0005]
    save_bookmarks(keep)
    return len(keep) < len(items)


def load_calibration():
    try:
        with open(CALIB_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@_locked
def save_calibration(cal):
    _atomic_write_json(CALIB_FILE, cal)
    return cal


def _iso_ms(epoch):
    """UTC ISO-8601 with milliseconds and a trailing Z — the acquisition-time
    format (rows keep whole seconds; an item's read time needs better)."""
    return (dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def write_state(record):
    # The reader's own file, rewritten every cycle by one process: a unique
    # temporary name like the settings files, but no fsync on this hot path.
    _atomic_write_json(STATE_FILE, record, indent=None, fsync=False)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ── console summary ──────────────────────────────────────────────────────
# The per-cycle log line used to be Leaf wording (SOC / power / gear); for any
# other profile that printed "SOC=?" forever. It is derived from the profile
# instead: the first few fast-lane signals the registry knows about, in
# registry order — SOC/voltage/current/power on the Leaf, RPM/speed on the
# Lancer. No profile-side hook needed.

SUMMARY_MAX = 4


def summary_signals():
    """Registry keys worth printing every cycle: signals whose item is in the
    fast lane (period 0), in the order the profile declares them."""
    out = []
    for key, sig in signals.SIGNALS.items():
        it = ITEMS.get(sig.get("item"))
        if not it or it.get("period") != 0:
            continue
        out.append(key)
        if len(out) >= SUMMARY_MAX:
            break
    return out


def summary(rec):
    """One-line, vehicle-independent digest of a decoded record."""
    parts = []
    for key in summary_signals():
        sig = signals.SIGNALS[key]
        v = signals.get_value(rec, key)
        if v is None:
            text = "?"
        elif sig.get("kind", "number") == "number" and isinstance(v, (int, float)):
            u = sig.get("unit", "")
            text = f"{v:.{sig.get('dec', 1)}f}" + (u if u in ("", "%") or u.startswith("°") else " " + u)
        elif isinstance(v, bool):
            text = "yes" if v else "no"
        else:
            text = str(v)
        parts.append(f"{sig.get('label', key)} {text}")
    return "  ".join(parts) or "no fast-lane signals"


# ── several adapters at once ─────────────────────────────────────────────
# Car-CAN and EV-CAN are two networks, so two adapters, of any kind. Each
# item names its bus (default "car"); the reader opens one adapter per bus
# from the `adapters` list and polls the buses concurrently, each with its
# own target state, liveness and reconnect. `--adapter X` stays the
# one-entry shorthand. docs/ARCHITECTURE.md "Several adapters".

ADAPTER_TYPES = ("usb", "ble", "replay", "sim", "can", "mqtt")


def adapter_entries(pref=None, cfg=None):
    """The adapters to open, one per bus, primary bus first:
    [{"type": "usb"|"ble"|"can"|"mqtt"|"replay"|"sim"|None, "bus": ..., ...}].

    `--adapter X` (pref) is the one-entry shorthand for the primary bus and
    wins over any list. Otherwise the `adapters` list — HAKAKE_ADAPTERS (a
    JSON list) over config.local.json — names them; the entry's other keys
    are per-adapter overrides in the file's own shape (`can_channel`,
    `host`, `serial_port`, …). Neither → one auto-detected adapter on the
    primary bus, which is what every run before 2026-09-09 did."""
    if pref:
        return [{"type": pref, "bus": PRIMARY_BUS, "shorthand": True}]
    raw = None
    js = os.environ.get("HAKAKE_ADAPTERS")
    if js:
        try:
            raw = json.loads(js)
        except json.JSONDecodeError as e:
            raise ValueError(f"HAKAKE_ADAPTERS is not valid JSON: {e}")
    if raw is None:
        raw = (cfg if cfg is not None else load_local_config()).get("adapters")
    if not raw:
        return [{"type": None, "bus": PRIMARY_BUS, "shorthand": True}]
    if not isinstance(raw, list):
        raise ValueError('"adapters" must be a list of {"type": ..., "bus": ...} entries')
    out, seen = [], set()
    for e in raw:
        if not isinstance(e, dict):
            raise ValueError(f'"adapters" entry must be an object, found {e!r}')
        t = e.get("type")
        t = None if t in (None, "", "auto") else str(t).lower()
        if t is not None and t not in ADAPTER_TYPES:
            raise ValueError(f"adapter type {t!r} is not one of {', '.join(ADAPTER_TYPES)}")
        bus = str(e.get("bus") or PRIMARY_BUS)
        if bus not in BUSES:
            raise ValueError(f"adapter {t or 'auto'} names bus {bus!r}; {VEHICLE.NAME} has {', '.join(BUSES)}")
        if bus in seen:
            raise ValueError(f"two adapters on bus {bus!r}; one per bus")
        seen.add(bus)
        out.append(dict(e, type=t, bus=bus))
    out.sort(key=lambda e: 0 if e["bus"] == PRIMARY_BUS else 1)
    return out


def entry_cfg(entry):
    """The detect_adapter(cfg=) overrides an entry implies, in
    config.local.json's shape: a `can` entry's bus becomes `can_bus`, an
    `mqtt` entry's bus / host / port / prefix go into an `mqtt` block, and
    everything else (`can_channel`, `serial_port`, `ble_addr`, …) passes
    through. Empty for a bare entry, and for the `--adapter X` shorthand —
    there the file's own `can_bus` / `mqtt.bus` still name the bus, as
    they did before the list existed."""
    t = entry.get("type")
    if entry.get("shorthand"):
        return {}
    over = {k: v for k, v in entry.items() if k not in ("type", "bus")}
    if t == "can":
        over["can_bus"] = entry["bus"]
    elif t == "mqtt":
        m = dict(over.pop("mqtt", None) or {})
        for k in ("host", "port", "prefix", "subscribe", "batch"):
            if k in over:
                m[k] = over.pop(k)
        m["bus"] = entry["bus"]
        over["mqtt"] = m
    return over


# ── provenance: one canonical key, several sources ───────────────────────
# A SIGNALS entry with `sources` is reachable more than one way. The resolver
# is generic — the profile only declares — and runs before apply_policy():
# a fresh source is one whose item ran within SOURCE_STALE_FACTOR × its
# period (at least SOURCE_STALE_MIN s, which is also the bound for a passive
# or instant item); precedence is a user pin (config `sources` or the tile's
# opts.source), then verified over tentative, then the fresher, then the
# faster. It writes the canonical key only when no decoder has ever written
# it in this process; otherwise `<key>_resolved`, because the stored value
# is the value the car reported. `<key>_src` is always written ("bus:item",
# or "stale" when nothing is fresh — the last value stands); two fresh
# sources further apart than `tolerance` set `<key>_disagree` and the reader
# logs a `source_disagree` event on the way in and out.

SOURCE_STALE_FACTOR = 3
SOURCE_STALE_MIN = 3.0
CONFIDENCE_RANK = {"verified": 0, "tentative": 1}


def source_freshness_limit(period):
    return max(SOURCE_STALE_MIN, SOURCE_STALE_FACTOR * float(period)) if period else SOURCE_STALE_MIN


def resolve_sources(specs, cache, item_age, period, pins=None, decoded=(), bus_of=None):
    """Pure. `specs` is vehicles.source_specs(); `cache` the merged record;
    `item_age` {item: s}; `period(item)` the item's polling period; `pins`
    {canonical: source key or "bus:item"}; `decoded` the keys decode() owns.
    Returns (updates, disagreements): the keys to merge into the record, and
    {canonical: disagree dict} for the ones in disagreement."""
    pins = pins or {}
    bus_of = bus_of or (lambda item: item_bus(VEHICLE, item))
    updates, disagreements = {}, {}
    for key, spec in specs.items():
        cands = []
        for s in spec.get("sources") or []:
            v = cache.get(s.get("key"))
            if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            age = item_age.get(s.get("item"))
            if age is None or age > source_freshness_limit(period(s["item"])):
                continue
            cands.append((s, v, age))
        if not cands:
            updates[f"{key}_src"] = "stale"
            continue
        pin = pins.get(key)

        def rank(c):
            s, v, age = c
            pinned = bool(pin) and (s["key"] == pin or f"{bus_of(s['item'])}:{s['item']}" == pin)
            return (0 if pinned else 1, CONFIDENCE_RANK.get(s.get("confidence"), 1), age, -(s.get("rate_hz") or 0))

        cands.sort(key=rank)
        s, v, age = cands[0]
        updates[f"{key}_resolved" if key in decoded else key] = v
        updates[f"{key}_src"] = f"{bus_of(s['item'])}:{s['item']}"
        tol = spec.get("tolerance")
        if tol is not None and len(cands) > 1:
            other = max(cands[1:], key=lambda c: abs(c[1] - v))
            delta = round(v - other[1], 3)
            if abs(delta) > tol:
                updates[f"{key}_disagree"] = {"a": s["key"], "b": other[0]["key"], "delta": delta}
                disagreements[key] = updates[f"{key}_disagree"]
    return updates, disagreements


class Reader:
    # Class default so a bare Reader.__new__(Reader) — how a couple of transport
    # tests borrow probe_alive() — still knows the console is off.
    console = None

    def __init__(self, interval, adapter_pref, fast=False, store=None, budget=1.5):
        # Minimum cycle period. None = the transport's own MIN_INTERVAL once it
        # is connected (0.5 s for the ELM327s, 0 for the native CAN façade);
        # an explicit --interval always wins. See interval_for().
        self.interval_arg = interval
        self.interval = DEFAULT_INTERVAL if interval is None else interval
        self.budget = budget              # slow-lane seconds per cycle
        self.adapter_pref = adapter_pref
        self.fast = fast
        self.store = store or Store()
        self.readings = 0
        self.cycle = 0
        self.cache = {}                   # latest decoded value of every key
        self.item_last = {}               # item → loop time of last successful run
        self.item_age = {}                # item → seconds since last run (published)
        self.item_dur = {}                # item → seconds its last read took (published, live only)
        self.item_gap = {}                # item → seconds between its last two reads (published, live only)
        # ── the clocks (docs/TIMING.md) ──
        # item_last / item_age are monotonic (scheduling); item_ts is the wall
        # clock (storage): when each item's answer, or its newest passive
        # frame, arrived. frame_ts is the *source's* clock for that frame
        # (bridge / driver) where the transport has one; ELM adapters do not.
        self.item_ts = {}                 # item → epoch seconds of acquisition
        self.frame_ts = {}                # item → source-clock epoch of its newest frame
        self.ts_source = "laptop"         # whose clock stamps the rows: laptop | driver | bridge
        self._peaks = {}                  # key → envelope since the last stored row
        self._stored_offset = None        # clock_offset_s last written to the session
        self.last_good = {k: v for k, v in load_state().items() if k not in ("status",)}
        self.session_id = None
        # ── several adapters (one per bus) ──
        self.entries = None               # adapter_entries(), resolved in run()
        self.transports = {}              # bus → transport; empty until run() connects
        self._targets = {}                # bus → kind its adapter is pointed at ("lbc" / "hvac" / "passive")
        self.bus_alive = {}               # bus → True | False | None (tri-state, per bus)
        self._bus_tasks = {}              # bus → reconnect task for a secondary bus that dropped
        self._bus_errors = {}             # bus → why it is not connected
        self._said = set()                # one log line per distinct complaint
        # ── provenance ──
        self._decoded = set()             # keys decode() has produced: the resolver never writes these
        self._disagree = {}               # canonical key → currently in disagreement?
        self.pins = {}                    # canonical key → pinned source (config `sources` + tile opts.source)
        self._cfg_pins = {k: v for k, v in (load_local_config().get("sources") or {}).items()
                          if isinstance(k, str) and isinstance(v, str)} if isinstance(load_local_config().get("sources"), dict) else {}
        self._tiles_mtime = None
        self._items = set()
        self._periods = {}                # item → period override from tile opts (cell log)
        self.console = None               # consolelog.Console while the raw output tile is on
        self._console_opts = None         # the options it was armed with
        self._console_text = {}           # text signal → last value put in the console
        self.cells_seq = 0                # bumps on every real cell-voltage decode
        self._stored_cells_seq = None     # the cells_seq the last stored row carried
        self._last_store = 0.0
        self._calib_mtime = None
        self.calib = {}
        self.prev_watch = {}          # last-seen state of each watched signal (for events)
        self.policy_state = {}            # vehicle-owned state for apply_policy (e.g. Leaf sensor fusion)
        self.speed = 1.0                  # transport cost multiplier; see estimate()
        self.passive_instant = False      # PASSIVE_INSTANT on the transport: ATMA has no dwell

    # ── config ───────────────────────────────────────────────────────────

    def refresh_items(self):
        try:
            m = os.path.getmtime(TILES_FILE)
        except OSError:
            m = None
        if m != self._tiles_mtime or not self._items:
            self._tiles_mtime = m
            cfg = load_tiles()
            new = enabled_items(cfg, self.fast)
            if new != self._items:
                for it in self._items - new:
                    for k in ITEM_KEYS.get(it, ()):
                        self.cache.pop(k, None)
                    self.item_last.pop(it, None)
                print(f"  [reader] polling {sorted(new)}", flush=True)
            self._items = new
            self.refresh_pins(cfg)
            periods = period_overrides(cfg)
            if periods != self._periods:
                armed = periods.get(CELLLOG_ITEM) == 0
                print(f"  [reader] cell log {'armed: cell voltages every cycle, every fresh read stored' if armed else 'off'}",
                      flush=True)
                self._periods = periods
            self.refresh_console(cfg)

    # ── the raw output console (docs/CONSOLE.md) ─────────────────────────

    def refresh_console(self, cfg):
        """Arm or disarm the console from the tile's enabled flag and options.

        Re-armed (a fresh ring, a fresh file) whenever an option changes, so
        the drop counts on screen always belong to the rules on screen.
        """
        opts = console_options(cfg)
        ids = console_ids(self._items)
        want = dict(opts, ids=sorted(ids)) if opts else None
        if want == self._console_opts:
            return
        self._console_opts = want
        if want is None:
            self.console = None
            print("  [reader] raw output console off", flush=True)
        else:
            self.console = consolelog.Console(CONSOLE_FILE, ids=ids,
                                              everything=want["everything"], rate_cap=want["rate"])
            scope = "every id on the bus (lossy)" if want["everything"] else f"{len(ids)} polled id(s)"
            print(f"  [reader] raw output console armed: {scope}, "
                  f"{want['rate']}/s per id -> {CONSOLE_FILE}", flush=True)
            self.console_event(f"console armed: {scope}, at most {want['rate']} frames per id per second")
        self.attach_console(self.transports)

    def attach_console(self, transports):
        """Point every transport's tap at the console — or at nothing.

        `tap` is a plain attribute on the CAN façade (native CAN, MQTT and the
        simulated bus all reach the reader through it); an ELM327 has none, and
        that is exactly the partial view the pane has to admit to.
        """
        c = self.console
        for bus, t in (transports or {}).items():
            if hasattr(t, "tap"):
                t.tap = c.tap(bus=bus) if c else None
            src = getattr(t, "_mqtt_source", None)
            if src is not None and hasattr(src, "event_tap"):
                src.event_tap = c.tap(bus=bus) if c else None
        if c is not None:
            primary = (transports or {}).get(PRIMARY_BUS)
            c.partial = "" if (primary is not None and hasattr(primary, "tap")) else CONSOLE_PARTIAL_ELM

    def console_event(self, text, bus=""):
        """A reader event — connect, reconnect, bus down, asleep, paused."""
        if self.console is not None:
            self.console.add("event", text, bus=bus or PRIMARY_BUS)

    def console_item(self, bus, elm, i, it, lines):
        """What one polled item put on the console.

        A passive capture on a transport whose façade is already tapped adds
        nothing — every frame went in as it arrived. On an ELM327 these lines
        *are* the only frames there will ever be, so they go in here. A UDS
        item becomes one `uds` entry per request, its answer grouped with the
        command that asked for it, which is the pairing that makes a decoded
        value trustworthy rather than magic; an answer that is not an answer
        (NO DATA, "?", silence) is what the adapter said, so it is `adapter`.
        """
        c = self.console
        if c is None:
            return
        tgt = TARGETS[it["kind"]]
        if tgt is None:
            if hasattr(elm, "tap"):
                return
            for line in lines:
                c.add("frame", line, cid=line.split()[0] if line.split() else "", bus=bus)
            if not lines:
                c.add("adapter", f"{it['id']}: nothing in a {it.get('secs', 0)}s dwell",
                      cid=str(it.get("id", "")), bus=bus)
            return
        rx = str(tgt[1]).upper()
        said = " / ".join(l for l in lines if l) or "no answer"
        empty = not any(l and not l.upper().startswith("NO DATA") and l.strip() != "?" for l in lines)
        c.add("adapter" if empty else "uds", f"{it['cmd']} -> {said}", cid=rx, bus=bus)

    def console_text(self):
        """Every `text`-kind signal the profile carries, as it changes — the
        Lancer's stored codes appear the moment mode 03 answers, with the raw
        frames that produced them directly above."""
        c = self.console
        if c is None:
            return
        for key, sig in signals.SIGNALS.items():
            if sig.get("kind") != "text":
                continue
            v = signals.get_value(self.cache, key)
            if v is None or v == "":
                continue
            if self._console_text.get(key) != v:
                self._console_text[key] = v
                c.add("text", f"{sig.get('label', key)}: {v}", bus=PRIMARY_BUS)

    def period(self, i):
        """An item's polling period: the profile's, unless a tile option overrides it."""
        return self._periods.get(i, ITEMS[i]["period"])

    @property
    def target(self):
        """The primary bus's adapter target (kept for the single-adapter
        callers; each bus has its own in `_targets`)."""
        return self._targets.get(PRIMARY_BUS)

    @target.setter
    def target(self, kind):
        if kind is None:
            self._targets.clear()
        else:
            self._targets[PRIMARY_BUS] = kind

    def refresh_pins(self, cfg):
        """User pins for the resolver: config.local.json `sources`
        ({canonical: source key}) under a tile's `opts.source` for the
        signal the tile shows."""
        pins = dict(self._cfg_pins)
        for t in cfg.get("tiles", []):
            src = (t.get("opts") or {}).get("source") if t.get("enabled", True) else None
            if t.get("signal") and isinstance(src, str) and src:
                pins[t["signal"]] = src
        self.pins = pins

    # ── state helpers ────────────────────────────────────────────────────

    def log(self, msg):
        print(f"  {dt.datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)

    # Identity of the adapter we are actually talking to. These are carried in
    # last_good like every other key, which meant a run that had not connected
    # yet republished the *previous* run's adapter: start with --adapter usb
    # after a BLE session and the dashboard showed a BLE UUID next to
    # "Detecting adapter (usb)…", with that session's stale SOC beside it.
    # Readings can be stale and say so via item_age; who we are talking to
    # cannot. Dropped until a connection actually reports them.
    ADAPTER_KEYS = ("adapter_type", "adapter_name", "adapter_port")

    def publish(self, status, message=None, **fields):
        rec = dict(self.last_good)
        if status in ("connecting", "reconnecting"):
            for k in self.ADAPTER_KEYS:
                rec.pop(k, None)
        rec.update(fields)
        rec["status"] = status
        rec["state_time"] = utc_now_iso()
        if message:
            rec["message"] = message
        elif "message" in rec:
            del rec["message"]
        write_state(rec)
        mqttsource.publish_state(rec)   # docs/MQTT.md §3.6: once per cycle and on every status change

    # ── scheduling ───────────────────────────────────────────────────────

    def estimate(self, i):
        """What one poll of item `i` is expected to cost, in seconds.

        The `est` numbers in a vehicle profile are BLE seconds — that is the
        link they were timed on, and they stay that way so a profile never has
        to know which adapter is plugged in. `self.speed` is the transport's
        own multiplier (SPEED on the transport class): 1.0 for BLE, 0.1 for
        USB and 0.05 for the native CAN façade, where the same command costs
        tens of milliseconds instead of hundreds. Without it a USB cycle spent its whole slow-lane budget on
        two items it had already finished, and the slow lane starved.

        A passive capture is the exception: ATMA runs for a wall-clock `secs`
        no matter how fast the link is, so only the per-command overhead
        scales — unless the transport declares `PASSIVE_INSTANT` (a native
        CAN controller answers ATMA from a table of frames it already
        holds), in which case the dwell is gone and only the overhead is left.
        """
        it = ITEMS[i]
        if TARGETS[it["kind"]] is None and "est" not in it:
            dwell = 0.0 if self.passive_instant else it["secs"]
            return dwell + 0.25 * self.speed
        est = it["est"] if "est" in it else 0.35
        return est * self.speed

    def plan(self, now):
        """Ordered item ids for this cycle: fast lane + most-overdue slow items within budget."""
        fast = [i for i in self._items if self.period(i) == 0]
        due = []
        for i in self._items:
            p = self.period(i)
            if p == 0:
                continue
            last = self.item_last.get(i)
            overdue = float("inf") if last is None else (now - last) / p
            if overdue >= 1.0:
                due.append((overdue, i))
        due.sort(reverse=True)
        chosen, spent = [], 0.0
        for overdue, i in due:
            est = self.estimate(i)
            if chosen and spent + est > self.budget:
                continue
            chosen.append(i)
            spent += est
        items = fast + chosen
        items.sort(key=lambda i: (KIND_ORDER.index(ITEMS[i]["kind"]), i))   # minimise ECU switches
        return items

    def next_due(self, now):
        """Seconds until the earliest slow item is due (0 if a fast item exists or nothing is known)."""
        waits = []
        for i in self._items:
            p = self.period(i)
            if p == 0:
                return 0.0
            last = self.item_last.get(i)
            waits.append(0.0 if last is None else max(0.0, last + p - now))
        return min(waits) if waits else 1.0

    def refresh_calibration(self):
        try:
            m = os.path.getmtime(CALIB_FILE)
        except OSError:
            m = None
        if m != self._calib_mtime:
            self._calib_mtime = m
            self.calib = load_calibration()
            if self.calib:
                print(f"  [reader] calibration {self.calib}", flush=True)

    def emit_events(self):
        """Record a transition event whenever a watched signal changes value.
        Runs every poll cycle so sub-5 s changes are caught; on-time is then a
        cheap events query independent of the store's 5 s row spacing."""
        c = self.cache
        for name in WATCH:
            if c.get(name) is None:
                continue
            norm = ev_norm(c[name])
            prev = self.prev_watch.get(name, "__unset__")
            if norm != prev:
                self.store.insert_event(name, c[name], None if prev == "__unset__" else prev)
                self.prev_watch[name] = norm

    def apply_policy(self):
        """Per-vehicle sensor policy (e.g. the Leaf's current fusion + zero
        calibration). web/calibration.json is generic; what it means is not."""
        self.refresh_calibration()
        fn = getattr(VEHICLE, "apply_policy", None)
        if fn:
            fn(self.cache, self.calib, self.policy_state)

    async def switch(self, elm, kind, bus=None):
        bus = bus or PRIMARY_BUS
        if kind == self._targets.get(bus):
            return
        tgt = TARGETS[kind]
        if tgt:
            await set_uds_target(elm, tgt[0], tgt[1])
        else:
            await elm.send("ATCAF0", wait=0)
        self._targets[bus] = kind

    def say_once(self, msg):
        if msg not in self._said:
            self._said.add(msg)
            self.log(f"[reader] {msg}")

    # ── one cycle ────────────────────────────────────────────────────────

    async def probe_alive(self, elm):
        """Is the adapter itself responding? The ELM327 is powered from OBD pin 16
        (always on), so it answers ATI even when the car is asleep; a dead BLE
        link (lid closed / slept) times out. Distinguishes asleep from dropped."""
        try:
            r = await elm.send("ATI", wait=0.1, timeout=3.0)
        except Exception:
            if self.console is not None:
                self.console.add("adapter", "ATI -> nothing (the link is gone)")
            return False
        if self.console is not None:
            self.console.add("adapter", f"ATI -> {' / '.join(x for x in r if x) or 'silence'}")
        return bool(r) and any(c.isdigit() for c in " ".join(r))

    async def poll_bus(self, bus, elm, items, timing, responses):
        """One bus's share of a cycle, in order, on its own adapter. Runs
        concurrently with the other buses' shares; a transport error
        propagates to poll_once, which decides per bus."""
        loop = asyncio.get_event_loop()
        got_data = polled = False
        listen_only = bool(getattr(elm, "listen_only", False))
        for i in items:
            it = ITEMS[i]
            if listen_only and TARGETS[it["kind"]] is not None:
                # A silent controller cannot ask; the transport would refuse it
                # anyway. Keep the item on its period so the plan does not
                # retry it every cycle, and leave it out of responses so the
                # profile's decode() does not read the refusal as a dead ECU.
                self.say_once(f"bus {bus!r} is listen-only: request items are not polled on it")
                self.item_last[i] = loop.time()
                continue
            polled = True
            t = loop.time()
            await self.switch(elm, it["kind"], bus)
            if TARGETS[it["kind"]] is None:
                lines = await passive_capture(elm, it["id"], it["secs"], set_caf=False)
            else:
                lines = await elm.send(it["cmd"], wait=0.05, timeout=it.get("timeout", 8.0))
            responses[i] = lines
            self.console_item(bus, elm, i, it, lines)
            done = loop.time()
            timing[i] = self.item_dur[i] = round(done - t, 3)
            prev = self.item_last.get(i)
            if prev is not None:
                self.item_gap[i] = round(done - prev, 3)
            self.item_last[i] = done
            self.stamp(elm, i, it)
            if any(l and not l.upper().startswith("NO DATA") and l.strip() != "?" for l in lines):
                got_data = True
        if polled:
            self.bus_alive[bus] = got_data

    async def poll_once(self, elm=None):
        """One cycle over every connected bus at once. `elm` alone (the
        single-adapter callers and the tests) stands for the primary bus."""
        self.cycle += 1
        self.refresh_items()
        loop = asyncio.get_event_loop()
        timing = {}
        responses = {}
        transports = self.transports or ({PRIMARY_BUS: elm} if elm is not None else {})
        self.attach_console(transports)
        by_bus = {}
        for i in self.plan(loop.time()):
            by_bus.setdefault(item_bus(VEHICLE, i), []).append(i)
        jobs = []
        for bus, items in by_bus.items():
            t = transports.get(bus)
            if t is None:
                if bus not in self._bus_tasks:      # reconnecting buses already said so
                    self.say_once(f"no adapter on bus {bus!r}: {', '.join(items)} not polled")
                continue
            jobs.append((bus, self.poll_bus(bus, t, items, timing, responses)))
        if jobs:
            results = await asyncio.gather(*(j for _, j in jobs), return_exceptions=True)
            for (bus, _), res in zip(jobs, results):
                if isinstance(res, BaseException):
                    if bus == PRIMARY_BUS or isinstance(res, (asyncio.CancelledError, KeyboardInterrupt)):
                        raise res                  # the supervisor reconnects everything
                    self.bus_failed(bus, res)      # a secondary bus reconnects on its own

        alive = True
        if responses:
            rec, a = VEHICLE.decode(responses)
            self.cache.update(rec)
            self._decoded.update(rec)
            self.track_peaks(rec)
            if "cells" in rec:
                self.cells_seq += 1           # a real cell read, as opposed to the sticky cache
            if a is not None:
                alive = a
                self.bus_alive[PRIMARY_BUS] = a
        prim = transports.get(PRIMARY_BUS)
        if getattr(prim, "listen_only", False) and any(i in responses for i in by_bus.get(PRIMARY_BUS, ())):
            # A listen-only primary never asks the primary ECU, so decode()
            # cannot say whether the car is awake. The broadcast traffic can:
            # silence there is a sleeping car, frames are a waking one.
            alive = self.bus_alive.get(PRIMARY_BUS, False)

        self.item_age = {i: round(loop.time() - self.item_last[i], 3) for i in self.item_last}
        self.resolve()
        self.apply_policy()
        self.emit_events()
        merged = dict(self.cache)
        merged["timing"] = timing
        merged["item_age"] = self.item_age
        merged["item_dur"] = dict(self.item_dur)
        merged["item_gap"] = dict(self.item_gap)
        merged["item_ts"] = {i: _iso_ms(e) for i, e in self.item_ts.items()}
        merged["item_ts_epoch"] = {i: round(e, 3) for i, e in self.item_ts.items()}
        if self.frame_ts:
            merged["frame_ts"] = {i: round(e, 3) for i, e in self.frame_ts.items()}
        merged["ts_source"] = self.ts_source
        off = elm.clock_offset() if hasattr(elm, "clock_offset") else None
        if off is not None:
            merged["clock_offset_s"] = off
        else:
            merged.pop("clock_offset_s", None)
        merged["items"] = sorted(self._items)
        merged["cells_seq"] = self.cells_seq
        merged["celllog"] = self._periods.get(CELLLOG_ITEM) == 0
        merged["bus_alive"] = dict(self.bus_alive)
        self.console_text()
        merged["console"] = self.console.stats() if self.console is not None else {"on": False}
        if self.console is not None:
            self.console.flush()           # the file is a cycle behind at worst
        return merged, alive

    # ── provenance ───────────────────────────────────────────────────────

    def resolve(self):
        """Run the generic source resolver (module docstring above) and log
        a `source_disagree` event when a canonical key's fresh sources start
        or stop disagreeing."""
        if not SOURCE_SPECS:
            return
        for key in SOURCE_SPECS:
            self.cache.pop(f"{key}_disagree", None)
        ups, dis = resolve_sources(SOURCE_SPECS, self.cache, self.item_age, self.period, self.pins, self._decoded)
        self.cache.update(ups)
        for key in SOURCE_SPECS:
            now_bad = key in dis
            was_bad = bool(self._disagree.get(key))
            if now_bad and not was_bad:
                d = dis[key]
                self.store.insert_event("source_disagree", f"{key}: {d['a']} vs {d['b']} delta {d['delta']}", None)
            elif was_bad and not now_bad:
                self.store.insert_event("source_disagree", f"{key}: agree", "disagree")
            self._disagree[key] = now_bad

    # ── the buses ────────────────────────────────────────────────────────

    async def open_entry(self, entry):
        """Detect and configure the adapter an `adapters` entry describes."""
        cfg = entry_cfg(entry)
        kw = {"cfg": cfg} if cfg else {}
        elm = await detect_adapter(prefer=entry["type"], log=self.log, **kw)
        await configure_vehicle(elm)
        return elm

    def bus_failed(self, bus, exc):
        """A secondary bus's transport raised mid-cycle: drop it, report it,
        and reconnect it in the background while the others keep polling."""
        self.log(f"[reader] bus {bus}: {type(exc).__name__}: {exc} — reconnecting it, other buses keep polling")
        self.console_event(f"bus {bus} down: {type(exc).__name__}: {exc} — reconnecting it", bus=bus)
        self.bus_alive[bus] = False
        self._bus_errors[bus] = f"{type(exc).__name__}: {exc}"
        old = self.transports.pop(bus, None)
        self._targets.pop(bus, None)
        if old is not None:
            asyncio.ensure_future(self._close_quietly(old))
        task = self._bus_tasks.get(bus)
        if task is None or task.done():
            self._bus_tasks[bus] = asyncio.ensure_future(self.reconnect_bus(bus))

    async def _close_quietly(self, elm):
        try:
            await elm.close()
        except Exception:
            pass

    async def reconnect_bus(self, bus):
        entry = next((e for e in (self.entries or []) if e["bus"] == bus), None)
        if entry is None:
            return
        backoff = BACKOFF_MIN
        while not os.path.exists(PAUSE_FILE):
            try:
                elm = await self.open_entry(entry)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                self._bus_errors[bus] = f"{type(e).__name__}: {e}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            self.transports[bus] = elm
            self._targets.pop(bus, None)
            self._bus_errors.pop(bus, None)
            self.bus_alive[bus] = None
            self.log(f"[reader] bus {bus} reconnected: {elm.adapter_name} via {elm.adapter_type}")
            self.console_event(f"bus {bus} reconnected: {elm.adapter_name} via {elm.adapter_type}", bus=bus)
            self.attach_console(self.transports)
            return

    async def close_transports(self):
        for task in self._bus_tasks.values():
            if not task.done():
                task.cancel()
        self._bus_tasks = {}
        for bus, t in list(self.transports.items()):
            await self._close_quietly(t)
        self.transports = {}
        self._targets = {}

    def adapters_info(self, static=False):
        """One entry per bus for the record (and, with `static`, for the
        session row: no liveness): bus, type, name, port, listen_only,
        speed, plus the transport's marker (`can_bus`, `remote`, …)."""
        out = []
        for e in self.entries or []:
            bus = e["bus"]
            t = self.transports.get(bus)
            d = {"bus": bus,
                 "type": getattr(t, "adapter_type", None) if t is not None else e.get("type"),
                 "name": getattr(t, "adapter_name", "") if t is not None else "",
                 "port": getattr(t, "adapter_port", "") if t is not None else "",
                 "listen_only": bool(getattr(t, "listen_only", False)),
                 "speed": float(getattr(t, "SPEED", 1.0) or 1.0) if t is not None else None}
            if t is not None and hasattr(t, "marker"):
                d.update(t.marker())
            if not static:
                d["connected"] = t is not None
                d["alive"] = self.bus_alive.get(bus)
                if t is not None and hasattr(t, "clock_offset"):
                    off = t.clock_offset()
                    if off is not None:
                        d["clock_offset_s"] = off
                if t is None and self._bus_errors.get(bus):
                    d["error"] = self._bus_errors[bus]
            out.append(d)
        return out

    # ── acquisition time and the envelope (docs/TIMING.md) ───────────────

    def stamp(self, elm, i, it):
        """Record when item `i`'s value was actually acquired. A UDS answer is
        stamped as it returns. A passive item on a transport that keeps a
        frame table (`source_times()`: the native CAN façade, MQTT) is stamped
        with its newest frame's *arrival* time — which may be older than this
        cycle when nothing new came in — and its source-clock time goes to
        `frame_ts`. On an ELM the frames arrived during the ATMA dwell that
        just ended, so the return time is within `secs` of the truth."""
        now = time.time()
        self.item_ts[i] = now
        if TARGETS[it["kind"]] is None and hasattr(elm, "source_times"):
            pair = elm.source_times().get(str(it["id"]).upper())
            if pair:
                wall, src = pair
                self.item_ts[i] = float(wall)
                if src and src > 0:
                    self.frame_ts[i] = float(src)
                else:
                    self.frame_ts.pop(i, None)

    def track_peaks(self, rec):
        """Keep min / max / time-of-each for the profile's `peak` keys over the
        values decode() produced this cycle — never the sticky cache, so a
        value read once is counted once. Reset by take_peaks() on store."""
        if not PEAK_KEYS:
            return
        for k in PEAK_KEYS:
            v = rec.get(k)
            if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            it = signals.signal_item(k)
            t = self.item_ts.get(it) if it else None
            t = round(t if t is not None else time.time(), 3)
            p = self._peaks.get(k)
            if p is None:
                self._peaks[k] = {"min": v, "max": v, "tmin": t, "tmax": t, "n": 1}
            else:
                p["n"] += 1
                if v < p["min"]:
                    p["min"], p["tmin"] = v, t
                if v > p["max"]:
                    p["max"], p["tmax"] = v, t

    def take_peaks(self):
        """The envelope since the last stored row, as record keys
        (`<key>_min/_max/_tmin/_tmax/_n`), and start a new one."""
        out = {}
        for k, p in self._peaks.items():
            out[f"{k}_min"], out[f"{k}_max"] = p["min"], p["max"]
            out[f"{k}_tmin"], out[f"{k}_tmax"], out[f"{k}_n"] = p["tmin"], p["tmax"], p["n"]
        self._peaks = {}
        return out

    # ── supervisor ───────────────────────────────────────────────────────

    async def run(self):
        backoff = BACKOFF_MIN
        attempt = 0
        while True:
            if os.path.exists(PAUSE_FILE):
                self.publish("paused", "Paused for calibration")
                self.log("[reader] pause file present — exiting; supervisor relaunches when it is removed")
                return
            elm = None
            try:
                attempt += 1
                self.entries = adapter_entries(self.adapter_pref)
                what = ", ".join(f"{e['type'] or 'auto'}@{e['bus']}" for e in self.entries)
                self.log(f"[reader] detecting adapter(s) (attempt {attempt}: {what})")
                self.publish("connecting", f"Detecting adapter ({what})…")
                self.transports = {}
                self._targets = {}
                self.bus_alive = {}
                for entry in self.entries:           # primary bus first; any failure retries them all
                    self.transports[entry["bus"]] = await self.open_entry(entry)
                elm = self.transports[PRIMARY_BUS]
                self.speed = float(getattr(elm, "SPEED", 1.0) or 1.0)
                self.passive_instant = bool(getattr(elm, "PASSIVE_INSTANT", False))
                self.interval = interval_for(elm, self.interval_arg)
                self.ts_source = str(getattr(elm, "ts_source", None) or "laptop")
                self.frame_ts = {}
                self._stored_offset = None
                self.session_id = self.store.start_session(elm.adapter_type, adapters=self.adapters_info(static=True))
                self.refresh_items()
                self.log(f"[reader] configured {elm.adapter_name} via {elm.adapter_type}; "
                         f"min period {self.interval}s, slow budget {self.budget}s"
                         + (f", est x{self.speed:g}" if self.speed != 1.0 else "")
                         + (f" — RECONNECTED after {attempt - 1} failed attempt(s)" if attempt > 1 else ""))
                for bus, t in self.transports.items():
                    if bus != PRIMARY_BUS:
                        self.log(f"[reader] bus {bus}: {t.adapter_name} via {t.adapter_type}"
                                 + (" (listen-only)" if getattr(t, "listen_only", False) else ""))
                self.attach_console(self.transports)
                self.console_event(f"connected: {elm.adapter_name} via {elm.adapter_type}"
                                   + (f" (reconnected after {attempt - 1} failed attempt(s))" if attempt > 1 else ""))
                attempt = 0
                backoff = BACKOFF_MIN
                if await self.poll_loop(elm) == "paused":
                    continue
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:  # transport-level failure → reconnect
                if attempt >= MAX_DETECT_ATTEMPTS:
                    # In-process reconnect keeps failing (typically a post-sleep
                    # CoreBluetooth stall). Exit; app.py relaunches a fresh process,
                    # which can see the adapter again.
                    self.log(f"[reader] {attempt} reconnects failed ({type(e).__name__}: {e}); "
                             f"exiting for a fresh process (clears CoreBluetooth after sleep)")
                    self.publish("reconnecting", "Restarting reader (Bluetooth reset after sleep)…")
                    return True
                self.log(f"[reader] {type(e).__name__}: {e} — retrying in {backoff}s")
                self.console_event(f"transport lost: {type(e).__name__}: {e} — retrying in {backoff}s")
                self.publish("reconnecting", f"{type(e).__name__}: {e}", retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            finally:
                if self.session_id:
                    self.store.end_session(self.session_id)
                    self.session_id = None
                await self.close_transports()      # every bus, reconnect tasks included

    async def poll_loop(self, elm):
        asleep = False
        loop = asyncio.get_event_loop()
        while True:
            if os.path.exists(PAUSE_FILE):
                return "paused"
            now = dt.datetime.now(dt.timezone.utc)
            t0 = loop.time()
            rec, alive = await self.poll_once(elm)
            elapsed = loop.time() - t0

            if not rec["timing"]:
                # nothing was due (only slow items enabled) — wait for the next one instead of spinning
                await asyncio.sleep(min(2.0, max(0.2, self.next_due(loop.time()))))
                continue

            if not alive:
                if not await self.probe_alive(elm):
                    # adapter not answering at all → link dropped (sleep/lid). Reconnect.
                    self.log("[reader] adapter stopped responding (ATI silent) — link dropped, reconnecting")
                    self.console_event("adapter stopped responding (ATI silent) — link dropped, reconnecting")
                    raise ConnectionError("adapter not responding — link dropped")
                if not asleep:
                    print(f"  [{now.strftime('%H:%M:%S')}] adapter OK but no CAN data — car asleep? polling every {ASLEEP_INTERVAL}s")
                    self.console_event(f"adapter answers but the bus is silent — car asleep? "
                                       f"polling every {ASLEEP_INTERVAL}s")
                asleep = True
                self.publish("asleep", "Adapter connected, no CAN data — car off?")
                await asyncio.sleep(ASLEEP_INTERVAL)
                continue
            if asleep:
                print("  car awake again")
                self.console_event("the bus is talking again")
                asleep = False

            self.readings += 1
            rec.update({
                "timestamp": utc_now_iso(),
                "readings": self.readings,
                "cycle_s": round(elapsed, 3),
                "adapter_type": elm.adapter_type,
                "adapter_name": elm.adapter_name,
                "adapter_port": elm.adapter_port,
                # Replayed data must never be mistakable for a live car. The
                # flag rides in every record so the API (and any UI built on
                # it) can say so without guessing from the adapter name.
                "replay": bool(getattr(elm, "replay", False)),
                "simulated": bool(getattr(elm, "simulated", False)),
                # every bus, one entry each; the adapter_* keys above are the
                # primary bus's, so nothing downstream changes
                "adapters": self.adapters_info(),
            })
            if getattr(elm, "replay", False):
                rec["replay_fixture"] = getattr(elm, "fixture_name", "")
                rec["replay_synthetic"] = bool(getattr(elm, "synthetic", False))
            # Simulated data carries the same kind of stamp, and carries it
            # in every record: scenario and seed included, so a screenshot of
            # the dashboard can always be traced back to what generated it.
            if getattr(elm, "simulated", False):
                rec.update(elm.marker() if hasattr(elm, "marker") else {"simulated": True})
            elif hasattr(elm, "marker"):
                # Any other transport with something to say about itself — the
                # native CAN façade stamps which bus it is on and whether it
                # could transmit (`can_bus`, `listen_only`).
                rec.update(elm.marker())
            # A row every STORE_PERIOD, plus one for every fresh cell read while the
            # cell log is armed. The cache is sticky, so cells that have not been
            # re-read since the last row are left out of it: one cell set per read,
            # never the same 96 values four times over.
            fresh_cells = rec.get("cells_seq") != self._stored_cells_seq
            due = loop.time() - self._last_store >= STORE_PERIOD
            if due or (rec.get("celllog") and fresh_cells):
                row = dict(rec) if fresh_cells else {k: v for k, v in rec.items() if k != "cells"}
                row.update(self.take_peaks())      # the envelope since the previous row
                self.store.insert_reading(row, ts=now, adapter=elm.adapter_type, ts_source=self.ts_source)
                self._stored_cells_seq = rec.get("cells_seq")
                self._last_store = loop.time()
                off = rec.get("clock_offset_s")
                if self.session_id and off is not None and off != self._stored_offset:
                    self.store.set_session_clock_offset(self.session_id, off)
                    self._stored_offset = off
            self.last_good = rec
            self.last_good["last_ok"] = rec["timestamp"]
            self.publish("ok")

            print(f"  [{self.readings:4d}] {now.astimezone().strftime('%H:%M:%S')}  {summary(rec)}"
                  f"  ({elapsed:.1f}s: {','.join(rec['timing'])})")

            await asyncio.sleep(max(0.0, self.interval - elapsed))


async def main(interval, adapter_pref, fast=False, budget=1.5, db=None):
    print(f"Reader — {VEHICLE.TITLE}")
    if adapter_pref == "replay":
        print("  REPLAY MODE — recorded fixture, not a car. Nothing here is a live reading.")
    if adapter_pref == "sim":
        print("  SIMULATOR MODE — a running model, not a car. Nothing here is a reading from any vehicle.")
    print(f"  state:  {STATE_FILE}")
    store = Store(db)
    print(f"  store:  {store.path} ({store.count()} readings)")
    reader = Reader(interval, adapter_pref, fast=fast, store=store, budget=budget)
    exited_for_restart = False
    try:
        exited_for_restart = await reader.run()
    finally:
        if not os.path.exists(PAUSE_FILE) and not exited_for_restart:
            reader.publish("stopped", "Reader stopped")
        store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=None,
                    help="Minimum seconds per cycle (default: the adapter's own — 0.5 for an ELM327, 0 for native CAN)")
    ap.add_argument("--budget", type=float, default=1.5, help="Slow-lane seconds per cycle (default: 1.5)")
    ap.add_argument("--adapter", choices=["auto", "usb", "ble", "replay", "sim", "can", "mqtt"], default="auto")
    ap.add_argument("--fast", action="store_true", help="Fast-lane primary item only (ignores tiles)")
    ap.add_argument("--vehicle", default=None, help="Vehicle profile in vehicles/ (default: leaf_ze0 or config.local.json)")
    ap.add_argument("--fixture", default=None, help="Replay session fixture (--adapter replay)")
    ap.add_argument("--speed", type=float, default=None, help="Replay/sim time scaling (2 = twice real time)")
    ap.add_argument("--scenario", default=None, help="Simulator scenario (--adapter sim)")
    ap.add_argument("--seed", type=int, default=None, help="Simulator RNG seed (--adapter sim)")
    ap.add_argument("--knob", action="append", default=[], metavar="NAME=VALUE",
                    help="Simulator knob at startup; repeatable (--adapter sim)")
    ap.add_argument("--sim-control", type=int, default=None, metavar="PORT",
                    help="Serve the simulator control API on 127.0.0.1:PORT (--adapter sim)")
    ap.add_argument("--sim-serial", default=None, metavar="DEV",
                    help="Talk to a simulator pty (hakake_sim.py --pty) over the real "
                         "serial transport (--adapter sim)")
    ap.add_argument("--sim-can", nargs="?", const="car", choices=["car", "ev"], default=None,
                    metavar="BUS",
                    help="Run the model's ECUs on an in-process virtual CAN bus behind the native "
                         "CAN façade at the real frame rate (--adapter sim); 'ev' also starts the "
                         "EV-CAN channel (listen-only)")
    ap.add_argument("--db", default=None, help="SQLite file (default: the profile's; replay/sim get their own)")
    args = ap.parse_args()
    if args.vehicle:
        set_vehicle(args.vehicle)
    pref = None if args.adapter == "auto" else args.adapter
    db = args.db
    if pref == "replay":
        # The real database holds years of irreplaceable readings; replay rows
        # would be indistinguishable noise in it. Replay gets its own file.
        db = db or replay_db()
        STATE_FILE = replay_state()        # keep the last real reading intact
        if args.fixture:
            os.environ["HAKAKE_REPLAY_FIXTURE"] = os.path.abspath(args.fixture)
        if args.speed:
            os.environ["HAKAKE_REPLAY_SPEED"] = str(args.speed)
    if pref == "sim":
        # Same rule as replay: generated rows get their own database and their
        # own state file. The real ones are never opened in this mode.
        db = db or sim_db()
        STATE_FILE = sim_state()
        if args.scenario:
            os.environ["HAKAKE_SIM_SCENARIO"] = args.scenario
        if args.seed is not None:
            os.environ["HAKAKE_SIM_SEED"] = str(args.seed)
        if args.speed:
            os.environ["HAKAKE_SIM_SPEED"] = str(args.speed)
        if args.sim_can and args.sim_serial:
            ap.error("--sim-can and --sim-serial are two different rigs; pick one")
        if args.sim_can:
            os.environ["HAKAKE_SIM_CAN"] = args.sim_can
        if args.sim_serial:
            # The model lives in another process (hakake_sim.py --pty), and so
            # does its control API: --sim-control here names the port THAT rig
            # owns, so the dashboard can link to it. We must not start one.
            os.environ["HAKAKE_SIM_SERIAL"] = args.sim_serial
            if args.sim_control:
                os.environ["HAKAKE_SIM_CONTROL_URL"] = f"http://127.0.0.1:{args.sim_control}"
        elif args.sim_control is not None:
            # In-process model: SimELM serves the API itself. 0 = a free port.
            os.environ["HAKAKE_SIM_CONTROL_PORT"] = str(args.sim_control)
        if args.knob:
            sys.path.insert(0, os.path.dirname(DIR))
            from hakake_sim import parse_knob_args        # noqa: E402
            os.environ["HAKAKE_SIM_KNOBS"] = json.dumps(parse_knob_args(args.knob))
    try:
        asyncio.run(main(args.interval, pref, fast=args.fast, budget=args.budget, db=db))
    except KeyboardInterrupt:
        print("\nStopped.")
