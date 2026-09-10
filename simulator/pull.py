# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The simulated pull, written down three ways — `hakake_sim.py --pull`.

One scenario (`simulator/scenarios/pull.json`: a standing-start acceleration
to ~50 mph, a hold, a regen coast, a brake to a stop) run offline, with no
wall clock, through the same `FrameSchedule` and encoders the live rig uses,
producing in one command:

  (a) a **sim database** run of the pull with the cell log armed — a row
      with all 96 cells every `row_period` seconds of simulated time — so
      the dashboard's Playback shows the timeline, the cells sagging and an
      auto-detected pull flag (`Store.pulls()` keys on current below −40 A);
  (b) the **raw CAN frame stream** of both channels, Car-CAN and EV-CAN, as
      JSON lines in the MQTT `rx/#` message format (docs/MQTT.md §3.1, plus
      the `tx/uds` request / ack pairs of §3.3–3.4 so the LBC's answers are
      in it) → `research/sim_pull_<date>.jsonl`, gitignored;
  (c) a **replay fixture** derived from (b) by `record_session.py --from-mqtt`,
      thinned to the ids the profile decodes plus `0x1DB` →
      `tests/fixtures/session_leaf_ze0_pull_sim.json`, `synthetic: true`.

(c) is the *expected* half of the comparison the owner runs when the CANable
arrives (`tools/compare_sessions.py expected.json observed.json`). Every
EV-CAN byte in it is ASSERTED from public documentation — the whole point of
the comparison is to find out where those bytes are wrong. Nothing here is
a reading from any vehicle, and every artefact says so: the database's
`meta` table, every row's `simulated` stamp, the fixture's `synthetic` flag
and `notes`.

Timestamps in the stream and the fixture are offsets from zero (a fixture
is published; an epoch would date a drive). The database gets wall-clock
timestamps ending at "now" so Playback's session picker finds it.
"""

import datetime as dt
import json
import os
import sys

from . import make_sim, load_scenario_file
from . import canbus
from .history import _refuse_real_db, state_path_for

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_PREFIX = "hakake/sim"
ADAPTER = "sim"
FIXTURE_NAME = "session_{vehicle}_pull_sim.json"
FIXTURE_LINES_PER_BUCKET = 10      # per id per timeline frame, after thinning
ALWAYS_KEEP = ("1DB",)             # EV-CAN ids the fixture keeps beyond what the profile decodes

# The UDS the reader would be sending: item → (tx, rx, command, period s).
# lbc02 every half second is the cell log armed. Taken from the profile's
# ITEMS at run time (below); this is the fallback shape.
UDS_PERIODS = {"lbc01": 0.5, "lbc02": 0.5, "lbc04": 2.0, "lbc05": 2.0, "hvac10": 5.0}


def default_jsonl(date=None):
    d = (date or dt.date.today()).strftime("%Y%m%d")
    return os.path.join(_ROOT, "research", f"sim_pull_{d}.jsonl")


def default_fixture(vehicle="leaf_ze0"):
    return os.path.join(_ROOT, "tests", "fixtures", FIXTURE_NAME.format(vehicle=vehicle))


def default_db(vehicle="leaf_ze0"):
    return os.path.join(_ROOT, "web", f"sim_{vehicle}.db")


def _uds_items(profile):
    """(item, tx, rx, cmd, period) for the UDS items the pull stream carries."""
    out = []
    items = getattr(profile, "ITEMS", {}) or {}
    targets = getattr(profile, "TARGETS", {}) or {}
    for name, per in UDS_PERIODS.items():
        it = items.get(name)
        if not it or not it.get("cmd"):
            continue
        pair = targets.get(it["kind"])
        if not pair:
            continue
        out.append((name, pair[0].upper(), pair[1].upper(), it["cmd"].upper(), per))
    return out


def _passive_ids(profile):
    items = getattr(profile, "ITEMS", {}) or {}
    return sorted({it["id"].upper() for it in items.values() if it.get("kind") == "passive" and it.get("id")})


class StreamWriter:
    """JSON lines in the MQTT message format, one per frame or request."""

    def __init__(self, path, prefix=DEFAULT_PREFIX):
        self.path = path
        self.prefix = prefix
        self.f = None
        self.lines = 0
        self.by_bus = {"car": 0, "ev": 0}

    def __enter__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.f = open(self.path, "w")
        return self

    def __exit__(self, *a):
        self.f.close()

    def _emit(self, topic, payload):
        self.f.write(json.dumps({"topic": topic, "payload": payload}, separators=(",", ":")) + "\n")
        self.lines += 1

    def frame(self, bus, t, id_hex, data):
        self.by_bus[bus] += 1
        self._emit(f"{self.prefix}/{bus}/rx/{id_hex}",
                   {"t": round(t, 6), "id": id_hex,
                    "d": " ".join(f"{b:02X}" for b in data)})

    def uds(self, bus, t, req, tx, rx, cmd, frames):
        """A request, its response frames a quarter-millisecond apart, its ack."""
        self._emit(f"{self.prefix}/{bus}/tx/uds",
                   {"req": req, "tx": tx, "rx": rx, "data": " ".join(cmd[i:i + 2] for i in range(0, len(cmd), 2)),
                    "bs": 0, "stmin": 0, "timeout": 2.0})
        for n, (cid, data) in enumerate(frames):
            self.frame(bus, t + 0.00025 * (n + 1), f"{cid:03X}", data)
        self._emit(f"{self.prefix}/{bus}/tx/uds/{req}", {"req": req, "ok": True, "frames": len(frames)})


def _stride_table(profile_ids, bucket=0.5, per_bucket=FIXTURE_LINES_PER_BUCKET):
    """Stride per id so that at most `per_bucket` lines survive per bucket."""
    strides = {}
    for bus in ("car", "ev"):
        for cid, ms in canbus.PERIODS[bus].get("leaf_ze0", {}).items():
            n = bucket * 1000.0 / ms
            strides[cid] = max(1, int(-(-n // per_bucket)))
    return {i: strides.get(i, 1) for i in profile_ids}


def thin_stream_strided(src, dst, strides):
    """Like thin_stream, with an explicit stride per id (every k-th frame kept)."""
    seen = {}
    kept = 0
    with open(src) as f, open(dst, "w") as out:
        for raw in f:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            topic = msg.get("topic", "")
            if "/tx/uds" in topic:
                out.write(raw)
                kept += 1
                continue
            p = msg.get("payload") or {}
            cid = str(p.get("id", topic.rsplit("/", 1)[-1])).upper()
            if cid not in strides:
                continue
            n = seen[cid] = seen.get(cid, 0) + 1
            if (n - 1) % strides[cid] == 0:
                out.write(raw)
                kept += 1
    return kept


def run_pull(scenario="pull", seed=1, vehicle=None, out_db=None, jsonl=None, fixture=None,
             bus_load=1.0, tick=0.01, row_period=0.5, tail_s=3.0, prefix=DEFAULT_PREFIX,
             write_fixture=True, log=None):
    """Run the pull offline and write the database, the stream and the fixture.

    Returns a summary dict (paths, counts, the peak). Deterministic for a
    given seed — the stream and the fixture come out byte-identical on every
    run; only the database's wall-clock timestamps differ.
    """
    from store import Store                       # web/ is on sys.path above
    from vehicles import get_vehicle
    import record_session

    log = log or (lambda *a: None)
    sim = make_sim(vehicle=vehicle, seed=seed, scenario=scenario)
    vehicle = sim.vehicle
    profile = get_vehicle(vehicle)
    data = load_scenario_file(scenario)
    duration = max((float(e.get("t", 0.0)) for e in data.get("timeline") or []), default=10.0) + tail_s

    out_db = os.path.abspath(out_db or default_db(vehicle))
    jsonl = os.path.abspath(jsonl or default_jsonl())
    fixture = os.path.abspath(fixture or default_fixture(vehicle))
    _refuse_real_db(out_db)

    store = Store(out_db, vehicle=vehicle)
    store.conn.execute("PRAGMA synchronous=OFF")
    _stamp(store, scenario, seed, vehicle, duration)
    session = store.start_session(ADAPTER, note=f"simulated pull (hakake_sim --pull, scenario {scenario}, seed {seed})")

    sched = {"car": canbus.FrameSchedule("car", vehicle, bus_load=bus_load, t0=0.0),
             "ev": canbus.FrameSchedule("ev", vehicle, bus_load=bus_load, t0=0.0)}
    uds = _uds_items(profile)
    uds_next = {name: 0.25 for name, *_ in uds}
    base = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(seconds=duration)
    rows = cells_seq = 0
    peak = {"t": None, "current_a": 0.0, "speed_mph": None, "power_kw": None}
    next_row = 0.0
    counter = 0
    watch = {}
    req_n = 0
    log(f"  running scenario {scenario!r} for {duration:.1f} s of simulated time "
        f"(tick {tick * 1000:.0f} ms, bus load {bus_load:g}) ...")
    with StreamWriter(jsonl, prefix) as sw:
        t = 0.0
        while t <= duration + 1e-9:
            st = sim.state()
            counter = (counter + 1) & 0x0F
            for bus, sc in sched.items():
                for cid in sc.due(t):
                    b = canbus.frame_bytes(bus, cid, st, vehicle, counter)
                    if b is not None:
                        sw.frame(bus, t, cid, b)
            for name, tx, rx, cmd, per in uds:
                if t + 1e-9 >= uds_next[name]:
                    uds_next[name] += per
                    lines = sim.respond(cmd, tx, rx)
                    frames = canbus.frames_of_lines(lines if lines and lines != ["NO DATA"] else [])
                    if frames:
                        req_n += 1
                        sw.uds("car", t, f"{req_n:04x}", tx, rx, cmd, frames)
            if t + 1e-9 >= next_row:
                next_row += row_period
                rec = sim.record(cells=True)
                cells_seq += 1
                when = base + dt.timedelta(seconds=t)
                rec.update({"simulated": True, "sim_scenario": scenario, "sim_seed": seed,
                            "sim_vehicle": vehicle, "sim_source": "hakake_sim --pull",
                            "sim_transport": "offline stream (simulator/pull.py)",
                            "cells_seq": cells_seq, "celllog": True,
                            "timestamp": when.isoformat().replace("+00:00", "Z")})
                store.insert_reading(rec, ts=when, adapter=ADAPTER)
                rows += 1
                for name in getattr(profile, "WATCH", ()):
                    cur = rec.get(name)
                    prev = watch.get(name, "__unset__")
                    if cur is not None and cur != prev:
                        store.insert_event(name, cur, None if prev == "__unset__" else prev, ts=when)
                        watch[name] = cur
                if st["current_a"] < peak["current_a"]:
                    peak = {"t": round(t, 2), "current_a": st["current_a"], "speed_mph": st["speed_mph"],
                            "power_kw": st["power_kw"], "cell_min": st["cell_min"], "pack_v": st["pack_v"]}
            sim.step(tick)
            t = round(t + tick, 6)
        last = sim.record(cells=False)
    store.end_session(session)
    _write_state(store, last, rows, scenario, seed, vehicle, base + dt.timedelta(seconds=duration))
    store.conn.commit()
    store.close()

    summary = {"simulated": True, "scenario": scenario, "seed": seed, "vehicle": vehicle,
               "duration_s": round(duration, 1), "rows": rows, "cell_rows": rows * 96,
               "db": out_db, "state": state_path_for(out_db), "jsonl": jsonl,
               "stream_lines": sw.lines, "frames_car": sw.by_bus["car"], "frames_ev": sw.by_bus["ev"],
               "uds_requests": req_n, "peak": peak, "bus_load": bus_load,
               "fixture": None, "fixture_frames": None,
               "playback": f"python web/app.py --db {os.path.relpath(out_db, _ROOT)} --no-reader",
               "warning": "SIMULATED DATA — not a reading from any vehicle; every EV-CAN byte is ASSERTED"}
    if write_fixture:
        keep = set(_passive_ids(profile)) | set(ALWAYS_KEEP)
        strides = _stride_table(keep)
        # the UDS answers ride on the ECUs' response ids: keep every frame of those
        for pair in (getattr(profile, "TARGETS", {}) or {}).values():
            if pair:
                strides[pair[1].upper()] = 1
        tmp = fixture + ".stream.tmp"
        try:
            thin_stream_strided(jsonl, tmp, strides)
            notes = (f"SYNTHETIC — generated by hakake_sim --pull from simulator/scenarios/{scenario}.json "
                     f"(seed {seed}); not a recording from any vehicle. Car-CAN lines come from the "
                     f"simulator's encoder (the inverse of docs/SIGNALS.md); every EV-CAN byte "
                     f"({', '.join(ALWAYS_KEEP)} here) is ASSERTED from public documentation "
                     f"(dalathegreat leaf_can_bus_messages DBC, OVMS vehicle_nissanleaf.cpp) and has never "
                     f"been checked against this car — this fixture is the EXPECTED half of "
                     f"tools/compare_sessions.py, to be compared with a real capture when the CANable "
                     f"arrives. Frames thinned to at most {FIXTURE_LINES_PER_BUCKET} lines per id per "
                     f"timeline frame; timestamps are offsets from the start of the run.")
            record_session.from_mqtt(tmp, out=fixture, vehicle=vehicle, period=row_period, notes=notes,
                                     synthetic=True, adapter="hakake-sim (simulated CAN rig)",
                                     log=lambda *a: None)
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
        with open(fixture) as f:
            summary["fixture_frames"] = len(json.load(f)["frames"])
        summary["fixture"] = fixture
    return summary


def _stamp(store, scenario, seed, vehicle, duration):
    rows = {
        "synthetic": "true",
        "warning": "SYNTHETIC DATA — generated by hakake_sim --pull. Not a reading from any "
                   "vehicle. Do not cite, do not merge into web/leaf_battery.db.",
        "generated_by": "simulator/pull.py",
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "pull_scenario": scenario, "pull_seed": str(seed), "pull_vehicle": vehicle,
        "pull_duration_s": f"{duration:.1f}",
    }
    with store.conn:
        store.conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(rows.items()))


def _write_state(store, rec, rows, scenario, seed, vehicle, when):
    """A `_state.json` beside the database so `web/app.py --db … --no-reader`
    has a /api/status to serve while Playback is opened."""
    iso = when.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    latest = dict(rec)
    latest.update({"status": "ok", "timestamp": iso, "last_ok": iso, "state_time": iso,
                   "readings": rows, "cycle_s": 0.5, "adapter_type": ADAPTER,
                   "adapter_name": f"simulated pull ({scenario}, seed {seed})",
                   "adapter_port": "sim:pull", "replay": False, "simulated": True,
                   "sim_scenario": scenario, "sim_seed": seed, "sim_vehicle": vehicle,
                   "message": "SIMULATED DATA — a generated pull, not a reading from any vehicle"})
    path = state_path_for(store.path)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(latest, f)
    os.replace(tmp, path)
