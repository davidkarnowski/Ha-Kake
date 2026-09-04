#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session recorder — capture a car (or convert an old capture) into a replay fixture.

A *session fixture* is what `--adapter replay` plays back: the raw ELM327
lines the car gave, keyed by request header and command, on a timeline. With
one, anybody can run the whole dashboard — reader, scheduler, decoders, store,
API, page — with no adapter and no car. That is what makes a vehicle profile
contributable by someone who does not own the car.

Two ways to get one:

  Record a live session (needs the adapter and the car):
      ./venv/bin/python record_session.py --seconds 60 --out my_drive.json
      ./venv/bin/python record_session.py --vehicle lancer_2009 --seconds 120

  Derive one from raw captures already in tests/fixtures (no hardware):
      ./venv/bin/python record_session.py --derive
      ./venv/bin/python record_session.py --derive --vehicle leaf_ze0

The recorder polls exactly what the active profile declares (ITEMS/TARGETS),
using the same transport helpers as the reader, and writes every line
verbatim. It never fabricates or interpolates: an item the car did not answer
is simply absent from the fixture, and replay reports it as NO DATA.

Privacy: raw frames can carry the odometer and (on some cars) the VIN. Run
`python scripts/privacy_sweep.py` before sharing a fixture, and look at it.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from elm327 import (detect_adapter, set_uds_target, passive_capture,   # noqa: E402
                    REPLAY_MARKER, load_replay_fixture)
from vehicles import get_vehicle                                       # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def default_out(vehicle):
    return os.path.join(FIXTURES, f"session_{vehicle}.json")


def write_fixture(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
    return path


def new_doc(vehicle, adapter="ELM327 v1.5", synthetic=False, source=(), notes=""):
    return {
        REPLAY_MARKER: 1,
        "vehicle": vehicle.NAME,
        "title": vehicle.TITLE,
        "adapter": adapter,
        "synthetic": bool(synthetic),
        "captured": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": list(source),
        "notes": notes,
        "frames": [],
    }


# ── live recording ───────────────────────────────────────────────────────

async def record(vehicle, elm, seconds, period, log=print):
    """Poll every item the profile declares, once per `period`, for `seconds`.

    Returns a list of frames. Each frame carries only what that pass actually
    got back — a silent ECU leaves a hole, which is the truth.
    """
    frames = []
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    target = None
    order = sorted(vehicle.ITEMS, key=lambda i: (vehicle.KIND_ORDER.index(vehicle.ITEMS[i]["kind"]), i))
    while loop.time() - t0 < seconds:
        frame = {"t": round(loop.time() - t0, 2), "uds": {}, "passive": {}}
        for iid in order:
            it = vehicle.ITEMS[iid]
            tgt = vehicle.TARGETS[it["kind"]]
            if target != it["kind"]:
                if tgt:
                    await set_uds_target(elm, tgt[0], tgt[1])
                else:
                    await elm.send("ATCAF0", wait=0)
                target = it["kind"]
            if tgt is None:
                lines = await passive_capture(elm, it["id"], it.get("secs", 0.2), set_caf=False)
                if lines:
                    frame["passive"][it["id"].upper()] = lines
            else:
                lines = await elm.send(it["cmd"], wait=0.05, timeout=it.get("timeout", 8.0))
                lines = [l for l in lines if l.strip() and "NO DATA" not in l.upper()]
                if lines:
                    frame["uds"].setdefault(tgt[0].upper(), {})[it["cmd"].upper()] = lines
        if frame["uds"] or frame["passive"]:
            frames.append(frame)
            log(f"  frame {len(frames)} at t={frame['t']}s — "
                f"{sum(len(v) for v in frame['uds'].values())} UDS, {len(frame['passive'])} monitored ids")
        else:
            log(f"  nothing answered at t={frame['t']}s (car asleep?)")
        wait = period - ((loop.time() - t0) % period)
        await asyncio.sleep(max(0.0, min(wait, period)))
    return frames


async def record_live(args, log=print):
    vehicle = get_vehicle(args.vehicle)
    log(f"Recording {vehicle.TITLE} for {args.seconds}s (a frame every {args.period}s)")
    elm = await detect_adapter(prefer=args.adapter, log=log)
    try:
        await vehicle.configure(elm)
        frames = await record(vehicle, elm, args.seconds, args.period, log=log)
    finally:
        await elm.close()
    doc = new_doc(vehicle, adapter=getattr(elm, "adapter_id", None) or elm.adapter_name,
                  source=[f"recorded live over {elm.adapter_type} by record_session.py"],
                  notes=args.notes or "")
    doc["frames"] = frames
    out = args.out or default_out(vehicle.NAME)
    write_fixture(out, doc)
    log(f"Wrote {len(frames)} frame(s) to {out}")
    log("Check it for private data before sharing:  python scripts/privacy_sweep.py")
    return out


# ── derivation from raw captures already in the repo ─────────────────────
#
# Every line below comes out of a capture that is already committed here. The
# deriver only re-keys it into the session shape; it invents nothing. Where a
# profile polls something no capture holds (the Leaf's HVAC group 00), the
# fixture simply lacks it and replay answers NO DATA, exactly as the car
# would if the ECU stayed quiet.

PASSIVE_FRAMES = 10       # how many timeline frames the passive captures fill


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def derive_leaf():
    lbc = _load("lbc_raw_20260824.json")
    probe = _load("probe_20260824_185139.json")
    vehicle = get_vehicle("leaf_ze0")

    uds = {"79B": {k.upper(): v for k, v in lbc["groups"].items()}}
    hvac = {k.upper(): v for k, v in probe["hvac"].items()
            if k.upper() in {i["cmd"].upper() for i in vehicle.ITEMS.values() if i["kind"] == "hvac"}}
    if hvac:
        uds["744"] = hvac

    # Passive captures hold consecutive real frames (the 284 counters tick).
    # Walk them one recorded line per timeline frame, in the order recorded;
    # a short capture repeats from its start rather than being padded.
    passive = {k.upper(): v for k, v in probe["passive"].items() if v}
    frames = []
    for i in range(PASSIVE_FRAMES):
        fr = {"t": float(i), "uds": uds if i == 0 else {}, "passive": {}}
        for cid, lines in passive.items():
            fr["passive"][cid] = [lines[i % len(lines)]]
        frames.append(fr)

    doc = new_doc(vehicle, adapter=lbc.get("adapter", "ELM327 v1.5"),
                  source=[
                      "tests/fixtures/lbc_raw_20260824.json — LBC groups 01-06, "
                      f"captured {lbc.get('captured')} (car IGN-ON, idle)",
                      "tests/fixtures/probe_20260824_185139.json — HVAC amp groups and "
                      f"passive Car-CAN frames, captured {probe.get('captured')} (Park, READY, A/C on)",
                  ],
                  notes=("Derived by record_session.py --derive from the two raw captures above; "
                         "every line is verbatim recorded data. The LBC/HVAC answers are one "
                         "frozen moment (frame 0); the monitored Car-CAN ids advance through "
                         "their recorded frames. HVAC group 2100 is absent from the capture, so "
                         "replay answers NO DATA for it — as the car would if the ECU stayed quiet."))
    doc["frames"] = frames
    return doc


def derive_lancer():
    idle = _load("lancer_idle_raw_20260828.json")
    dtc = _load("lancer_dtc_raw_20260828.json")
    vehicle = get_vehicle("lancer_2009")

    engine = {k.upper(): v for k, v in idle["responses"].items()}
    trans = {}
    for key, lines in dtc["responses"].items():
        ecu, _, cmd = key.partition(":")
        (engine if ecu == "engine" else trans)[cmd.upper()] = lines

    doc = new_doc(vehicle, source=[
        "tests/fixtures/lancer_idle_raw_20260828.json — mode 01 PIDs, "
        f"captured {idle.get('captured')} (engine idling)",
        "tests/fixtures/lancer_dtc_raw_20260828.json — modes 01/03/07 from both ECUs, "
        f"captured {dtc.get('captured')}",
    ], notes=("Derived by record_session.py --derive; every line is verbatim recorded data. "
              "One frozen moment — the Lancer captures are single snapshots, so replay "
              "repeats them."))
    doc["frames"] = [{"t": 0.0, "uds": {"7E0": engine, "7E1": trans}, "passive": {}}]
    return doc


DERIVERS = {"leaf_ze0": derive_leaf, "lancer_2009": derive_lancer}


def derive(names=None, out_dir=None, log=print):
    written = []
    for name in (names or sorted(DERIVERS)):
        if name not in DERIVERS:
            log(f"  no deriver for {name!r} — record a live session instead")
            continue
        doc = DERIVERS[name]()
        path = os.path.join(out_dir or FIXTURES, f"session_{name}.json")
        write_fixture(path, doc)
        load_replay_fixture(path)                     # validate what we just wrote
        log(f"  {name}: {len(doc['frames'])} frame(s) -> {path}")
        written.append(path)
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Record (or derive) a Ha-Kake replay session fixture")
    ap.add_argument("--derive", action="store_true",
                    help="build fixtures from raw captures in tests/fixtures (no hardware)")
    ap.add_argument("--vehicle", default=None, help="vehicle profile (default: the configured one)")
    ap.add_argument("--seconds", type=float, default=60.0, help="live recording length (default: 60)")
    ap.add_argument("--period", type=float, default=2.0,
                    help="seconds between frames (default: 2). NOTE: a pass polls EVERY item "
                         "the profile declares, ignoring per-item periods, so a frame costs far "
                         "more than a dashboard cycle. The Leaf's 18 items take roughly 6-9 s "
                         "over BLE — use --period 6 or higher there, or frames will overrun "
                         "and the spacing will not be what you asked for.")
    ap.add_argument("--adapter", choices=["auto", "usb", "ble"], default="auto")
    ap.add_argument("--out", default=None, help="output path (default: tests/fixtures/session_<vehicle>.json)")
    ap.add_argument("--notes", default="", help="free-text note stored in the fixture")
    args = ap.parse_args(argv)
    args.adapter = None if args.adapter == "auto" else args.adapter
    if args.derive:
        derive([args.vehicle] if args.vehicle else None)
        return 0
    asyncio.run(record_live(args))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
