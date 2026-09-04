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
  python reader.py --interval 1       # minimum seconds per cycle (default 0.5)
  python reader.py --budget 1.5       # seconds of slow-lane work per cycle
  python reader.py --vehicle lancer_2009   # a different vehicle profile
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from elm327 import detect_adapter, set_uds_target, passive_capture  # noqa: E402
from store import Store, utc_now_iso, ev_norm                       # noqa: E402
from vehicles import get_vehicle                                    # noqa: E402
import signals                                                      # noqa: E402

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "battery_state.json")
PAUSE_FILE = os.path.join(DIR, "reader.pause")
TILES_FILE = os.path.join(DIR, "tiles.json")
CALIB_FILE = os.path.join(DIR, "calibration.json")   # per-car offsets (gitignored)
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
# The simulator cockpit (/sim) keeps its own arrangement here (gitignored).
# It is not web/tiles.json because that store is vehicle-shaped: _clean_tile()
# drops any id the active profile's TILES does not declare, and the cockpit's
# cards (cluster, knob categories, time, state) are the page's, not a car's.
SIM_TILES_FILE = os.path.join(DIR, "sim_tiles.json")

ASLEEP_INTERVAL = 60
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


async def configure_vehicle(elm):        # module-level so tests can monkeypatch it
    await VEHICLE.configure(elm)


def set_vehicle(name=None):
    """Bind a vehicle profile (vehicles/<name>.py) to this module and the signal registry."""
    global VEHICLE, ITEMS, TILES, DEFAULT_SPAN, DEFAULT_TILES, ITEM_KEYS
    global WATCH, KIND_ORDER, TARGETS, FAST_ONLY
    VEHICLE = get_vehicle(name)
    ITEMS = VEHICLE.ITEMS
    TILES = VEHICLE.TILES
    DEFAULT_SPAN = VEHICLE.DEFAULT_SPAN
    DEFAULT_TILES = {"tiles": [dict(t) for t in VEHICLE.DEFAULT_TILES]}
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
    for k, lo, hi in (("x", 0, 10), ("y", 0, 10000), ("h", 2, 200)):
        if k in out:
            try:
                out[k] = min(hi, max(lo, int(out[k])))
            except (TypeError, ValueError):
                del out[k]
    if "x" in out:
        out["x"] = min(out["x"], 12 - out["span"])
    if not isinstance(out.get("opts", {}), dict):
        out["opts"] = {}
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
    tmp = TILES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"tiles": ordered}, f, indent=1)
    os.replace(tmp, TILES_FILE)
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
    if "opts" in out and not isinstance(out["opts"], dict):
        out["opts"] = {}
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


def save_sim_tiles(cfg):
    out = _sim_tiles_from((cfg or {}).get("tiles", []) if isinstance(cfg, dict) else [])
    tmp = SIM_TILES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, SIM_TILES_FILE)
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
    tmp = LAYOUTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, LAYOUTS_FILE)


def list_layouts():
    d = _read_layouts()
    return sorted(({"name": k, "saved": v.get("saved"), "tiles": len(v.get("tiles", []))} for k, v in d.items()),
                  key=lambda x: x["name"].lower())


def save_layout(name, cfg=None):
    """Store a layout under `name` (current web/tiles.json when cfg is None)."""
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("layout name required")
    tiles = save_tiles(cfg)["tiles"] if cfg is not None else load_tiles()["tiles"]
    d = _read_layouts()
    d[name] = {"saved": utc_now_iso(), "tiles": tiles}
    _write_layouts(d)
    return d[name]


def load_layout(name):
    """Make a saved layout the active one (writes web/tiles.json)."""
    d = _read_layouts()
    if name not in d:
        raise KeyError(name)
    return save_tiles({"tiles": d[name].get("tiles", [])})


def delete_layout(name):
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


def load_calibration():
    try:
        with open(CALIB_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_calibration(cal):
    tmp = CALIB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cal, f, indent=1)
    os.replace(tmp, CALIB_FILE)
    return cal


def write_state(record):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, STATE_FILE)


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


class Reader:
    def __init__(self, interval, adapter_pref, fast=False, store=None, budget=1.5):
        self.interval = interval          # minimum cycle period
        self.budget = budget              # slow-lane seconds per cycle
        self.adapter_pref = adapter_pref
        self.fast = fast
        self.store = store or Store()
        self.readings = 0
        self.cycle = 0
        self.cache = {}                   # latest decoded value of every key
        self.item_last = {}               # item → loop time of last successful run
        self.item_age = {}                # item → seconds since last run (published)
        self.last_good = {k: v for k, v in load_state().items() if k not in ("status",)}
        self.session_id = None
        self.target = None                # "lbc" / "hvac" / "passive"
        self._tiles_mtime = None
        self._items = set()
        self._last_store = 0.0
        self._calib_mtime = None
        self.calib = {}
        self.prev_watch = {}          # last-seen state of each watched signal (for events)
        self.policy_state = {}            # vehicle-owned state for apply_policy (e.g. Leaf sensor fusion)
        self.speed = 1.0                  # transport cost multiplier; see estimate()

    # ── config ───────────────────────────────────────────────────────────

    def refresh_items(self):
        try:
            m = os.path.getmtime(TILES_FILE)
        except OSError:
            m = None
        if m != self._tiles_mtime or not self._items:
            self._tiles_mtime = m
            new = enabled_items(load_tiles(), self.fast)
            if new != self._items:
                for it in self._items - new:
                    for k in ITEM_KEYS.get(it, ()):
                        self.cache.pop(k, None)
                    self.item_last.pop(it, None)
                print(f"  [reader] polling {sorted(new)}", flush=True)
            self._items = new

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

    # ── scheduling ───────────────────────────────────────────────────────

    def estimate(self, i):
        """What one poll of item `i` is expected to cost, in seconds.

        The `est` numbers in a vehicle profile are BLE seconds — that is the
        link they were timed on, and they stay that way so a profile never has
        to know which adapter is plugged in. `self.speed` is the transport's
        own multiplier (SPEED on the transport class): 1.0 for BLE, ~0.15 for
        USB, where the same command costs tens of milliseconds instead of
        hundreds. Without it a USB cycle spent its whole slow-lane budget on
        two items it had already finished, and the slow lane starved.

        A passive capture is the exception: ATMA runs for a wall-clock `secs`
        no matter how fast the link is, so only the per-command overhead
        scales.
        """
        it = ITEMS[i]
        if TARGETS[it["kind"]] is None and "est" not in it:
            return it["secs"] + 0.25 * self.speed
        est = it["est"] if "est" in it else 0.35
        return est * self.speed

    def plan(self, now):
        """Ordered item ids for this cycle: fast lane + most-overdue slow items within budget."""
        fast = [i for i in self._items if ITEMS[i]["period"] == 0]
        due = []
        for i in self._items:
            p = ITEMS[i]["period"]
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
            p = ITEMS[i]["period"]
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

    async def switch(self, elm, kind):
        if kind == self.target:
            return
        tgt = TARGETS[kind]
        if tgt:
            await set_uds_target(elm, tgt[0], tgt[1])
        else:
            await elm.send("ATCAF0", wait=0)
        self.target = kind

    # ── one cycle ────────────────────────────────────────────────────────

    async def probe_alive(self, elm):
        """Is the adapter itself responding? The ELM327 is powered from OBD pin 16
        (always on), so it answers ATI even when the car is asleep; a dead BLE
        link (lid closed / slept) times out. Distinguishes asleep from dropped."""
        try:
            r = await elm.send("ATI", wait=0.1, timeout=3.0)
        except Exception:
            return False
        return bool(r) and any(c.isdigit() for c in " ".join(r))

    async def poll_once(self, elm):
        self.cycle += 1
        self.refresh_items()
        loop = asyncio.get_event_loop()
        timing = {}
        responses = {}
        for i in self.plan(loop.time()):
            it = ITEMS[i]
            t = loop.time()
            await self.switch(elm, it["kind"])
            if TARGETS[it["kind"]] is None:
                responses[i] = await passive_capture(elm, it["id"], it["secs"], set_caf=False)
            else:
                responses[i] = await elm.send(it["cmd"], wait=0.05, timeout=it.get("timeout", 8.0))
            timing[i] = round(loop.time() - t, 2)
            self.item_last[i] = loop.time()

        alive = True
        if responses:
            rec, a = VEHICLE.decode(responses)
            self.cache.update(rec)
            if a is not None:
                alive = a

        self.item_age = {i: round(loop.time() - self.item_last[i], 1) for i in self.item_last}
        self.apply_policy()
        self.emit_events()
        merged = dict(self.cache)
        merged["timing"] = timing
        merged["item_age"] = self.item_age
        merged["items"] = sorted(self._items)
        return merged, alive

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
                self.log(f"[reader] detecting adapter (attempt {attempt}, prefer={self.adapter_pref or 'auto'})")
                self.publish("connecting", f"Detecting adapter ({self.adapter_pref or 'auto'})…")
                elm = await detect_adapter(prefer=self.adapter_pref, log=self.log)
                await configure_vehicle(elm)
                self.target = None
                self.speed = float(getattr(elm, "SPEED", 1.0) or 1.0)
                self.session_id = self.store.start_session(elm.adapter_type)
                self.refresh_items()
                self.log(f"[reader] configured {elm.adapter_name} via {elm.adapter_type}; "
                         f"min period {self.interval}s, slow budget {self.budget}s"
                         + (f", est x{self.speed:g}" if self.speed != 1.0 else "")
                         + (f" — RECONNECTED after {attempt - 1} failed attempt(s)" if attempt > 1 else ""))
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
                self.publish("reconnecting", f"{type(e).__name__}: {e}", retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            finally:
                if self.session_id:
                    self.store.end_session(self.session_id)
                    self.session_id = None
                if elm:
                    try:
                        await elm.close()
                    except Exception:
                        pass

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
                    raise ConnectionError("adapter not responding — link dropped")
                if not asleep:
                    print(f"  [{now.strftime('%H:%M:%S')}] adapter OK but no CAN data — car asleep? polling every {ASLEEP_INTERVAL}s")
                asleep = True
                self.publish("asleep", "Adapter connected, no CAN data — car off?")
                await asyncio.sleep(ASLEEP_INTERVAL)
                continue
            if asleep:
                print("  car awake again")
                asleep = False

            self.readings += 1
            rec.update({
                "timestamp": utc_now_iso(),
                "readings": self.readings,
                "cycle_s": round(elapsed, 1),
                "adapter_type": elm.adapter_type,
                "adapter_name": elm.adapter_name,
                "adapter_port": elm.adapter_port,
                # Replayed data must never be mistakable for a live car. The
                # flag rides in every record so the API (and any UI built on
                # it) can say so without guessing from the adapter name.
                "replay": bool(getattr(elm, "replay", False)),
                "simulated": bool(getattr(elm, "simulated", False)),
            })
            if getattr(elm, "replay", False):
                rec["replay_fixture"] = getattr(elm, "fixture_name", "")
                rec["replay_synthetic"] = bool(getattr(elm, "synthetic", False))
            # Simulated data carries the same kind of stamp, and carries it
            # in every record: scenario and seed included, so a screenshot of
            # the dashboard can always be traced back to what generated it.
            if getattr(elm, "simulated", False):
                rec.update(elm.marker() if hasattr(elm, "marker") else {"simulated": True})
            if loop.time() - self._last_store >= STORE_PERIOD:
                self.store.insert_reading(rec, ts=now, adapter=elm.adapter_type)
                self._last_store = loop.time()
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
    ap.add_argument("--interval", type=float, default=0.5, help="Minimum seconds per cycle (default: 0.5)")
    ap.add_argument("--budget", type=float, default=1.5, help="Slow-lane seconds per cycle (default: 1.5)")
    ap.add_argument("--adapter", choices=["auto", "usb", "ble", "replay", "sim"], default="auto")
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
