#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
bench_canrate.py — can the reader keep up with a native CAN adapter's frame rate?

    python tools/bench_canrate.py                    # 0.1 / 0.5 / 1.0 bus load, 8 s each
    python tools/bench_canrate.py --seconds 20 --loads 1.0 --log research/bench.md
    python tools/bench_canrate.py --json

The CANable had not arrived when this was written (2026-09-09). This bench
runs the reader — `Reader.poll_once()`, the real scheduler, the real
profile, the real decoders — over the native CAN façade
(`cantransport.CanFacade`) against the simulated bus (`simulator/canbus.py`:
the model's ECUs on an in-process python-can `virtual` channel at the
surveyed frame rates, ≈1,700 frames/s on Car-CAN at bus load 1.0), and
measures, per bus load:

  * reader cycle time, median and p90 — for the *scheduled* cycle (what the
    reader actually does: only items whose period is due) and the *full*
    cycle (every enabled item polled, the worst case)
  * intake frames/s seen by the façade — the broadcast traffic the ECUs put
    on the bus, and the total including the reader's own UDS answers
  * process CPU %, split into the ECU threads' share (the simulated car,
    which a real board would not cost) and the rest (the reader's own)
  * ATMA hit rate per passive item: in what fraction of cycles had the
    façade seen a frame of that id within the item's own `secs` window,
    and within 0.2 s

**MEASURED ON THE LAPTOP, VIRTUAL BUS**: no wire, no bit errors, no LBC
pacing (the simulated LBC answers a 29-frame cell read in about 0.14 s at
the façade's STmin 5, all of it our requested gap; the real one paces itself
and took 0.29 s over the CANable, 0.36 s over USB), no USB stack, no slcan byte
parser. The numbers bound the reader's own cost, not the adapter's. Nothing
here is a reading from any vehicle, and nothing is written outside a
temporary directory.
"""

import argparse
import asyncio
import datetime as dt
import os
import platform
import statistics
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_LOADS = (0.1, 0.5, 1.0)
DEFAULT_SECONDS = 8.0
DEFAULT_INTERVAL = 0.5          # the reader's own --interval floor


def parse_loads(text):
    """`"0.1,0.5,1"` → [0.1, 0.5, 1.0]; each clamped to 0..1, in order given."""
    out = []
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        v = float(part)
        out.append(max(0.0, min(1.0, v)))
    return out or list(DEFAULT_LOADS)


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
    return s[k]


def summarise(load, sched_cycles, full_cycles, frames, wall, cpu_total, cpu_ecu, hits, expected_fps,
              misses=0, broadcast=None):
    """One table row from raw samples. Pure, so the tests can pin it.

    `frames` is everything the façade received; `broadcast` is what the ECUs
    put on the bus unasked (the reader's UDS answers are the difference)."""
    ms = lambda v: None if v is None else round(v * 1000.0, 2)          # noqa: E731
    row = {
        "bus_load": load,
        "expected_fps": expected_fps,
        "intake_fps": round(frames / wall, 1) if wall > 0 else None,
        "broadcast_fps": round(broadcast / wall, 1) if (wall > 0 and broadcast is not None) else None,
        "cycles": len(sched_cycles),
        "sched_median_ms": ms(statistics.median(sched_cycles)) if sched_cycles else None,
        "sched_p90_ms": ms(percentile(sched_cycles, 0.9)),
        "full_cycles": len(full_cycles),
        "full_median_ms": ms(statistics.median(full_cycles)) if full_cycles else None,
        "full_p90_ms": ms(percentile(full_cycles, 0.9)),
        "cpu_total_pct": round(100.0 * cpu_total / wall, 1) if wall > 0 else None,
        "cpu_ecu_pct": round(100.0 * cpu_ecu / wall, 1) if wall > 0 else None,
        "cpu_reader_pct": round(100.0 * (cpu_total - cpu_ecu) / wall, 1) if wall > 0 else None,
        "uds_misses": misses,
        "hits": {},
    }
    for item, (n, hit_secs, hit_02) in sorted(hits.items()):
        row["hits"][item] = {"n": n,
                             "at_secs": round(100.0 * hit_secs / n, 1) if n else None,
                             "at_0_2s": round(100.0 * hit_02 / n, 1) if n else None}
    return row


def format_table(rows, items=None):
    """The rows as a markdown table (one line per bus load), plus the hit-rate table."""
    out = ["| bus load | expected fps | broadcast fps | intake fps (incl. UDS) | sched. cycle med / p90 (ms) "
           "| full cycle med / p90 (ms) | CPU total / ECU / reader (%) | UDS misses |",
           "|---|---|---|---|---|---|---|---|"]
    f = lambda v: "—" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))   # noqa: E731
    for r in rows:
        out.append(f"| {f(r['bus_load'])} | {f(r['expected_fps'])} | {f(r.get('broadcast_fps'))} | {f(r['intake_fps'])} "
                   f"| {f(r['sched_median_ms'])} / {f(r['sched_p90_ms'])} "
                   f"| {f(r['full_median_ms'])} / {f(r['full_p90_ms'])} "
                   f"| {f(r['cpu_total_pct'])} / {f(r['cpu_ecu_pct'])} / {f(r['cpu_reader_pct'])} "
                   f"| {f(r['uds_misses'])} |")
    names = items or sorted({i for r in rows for i in r["hits"]})
    if names:
        out.append("")
        out.append("ATMA hit rate, % of cycles with a frame of the id inside the window (item's own `secs` / 0.2 s):")
        out.append("")
        out.append("| bus load | " + " | ".join(names) + " |")
        out.append("|---|" + "---|" * len(names))
        for r in rows:
            cells = []
            for n in names:
                h = r["hits"].get(n)
                cells.append("—" if not h else f"{f(h['at_secs'])} / {f(h['at_0_2s'])}")
            out.append(f"| {f(r['bus_load'])} | " + " | ".join(cells) + " |")
    return "\n".join(out)


async def bench_one(load, seconds, scenario, seed, log, interval=DEFAULT_INTERVAL):
    import reader as rd
    from store import Store
    from cantransport import open_sim_can
    from simulator import make_sim
    from simulator.canbus import expected_fps

    tmp = tempfile.mkdtemp(prefix="hakake-bench-")
    # the reader's on-disk state, all in the temp dir: never web/ files
    for attr, name in (("STATE_FILE", "state.json"), ("PAUSE_FILE", "reader.pause"),
                       ("TILES_FILE", "tiles.json"), ("CALIB_FILE", "calibration.json")):
        setattr(rd, attr, os.path.join(tmp, name))
    sim = make_sim(vehicle=rd.VEHICLE.NAME, scenario=scenario, seed=seed)
    elm = await open_sim_can(log=lambda *a: None, mode="car", sim=sim, control_port=None, bus_load=load)
    store = Store(os.path.join(tmp, "bench.db"))
    try:
        await rd.configure_vehicle(elm)
        r = rd.Reader(interval=interval, adapter_pref="sim", store=store, budget=1.5)
        r.speed = elm.SPEED
        r.passive_instant = elm.PASSIVE_INSTANT
        r.refresh_items()
        passive = {i: rd.ITEMS[i] for i in r._items if rd.ITEMS[i]["kind"] == "passive"}
        await asyncio.sleep(0.5)                          # let the table fill
        ecus = elm.source.ecus
        sched, full = [], []
        hits = {i: [0, 0, 0] for i in passive}
        frames0, cpu0, ecu0, misses0 = elm.frames, time.process_time(), sum(e.cpu_s for e in ecus), len(elm.misses)
        sent0 = sum(e.sent for e in ecus)
        t_start = time.monotonic()
        n = 0
        while time.monotonic() - t_start < seconds:
            n += 1
            is_full = n % 4 == 0                           # every fourth cycle: poll everything
            if is_full:
                r.item_last.clear()
            t = time.monotonic()
            await r.poll_once(elm)
            el = time.monotonic() - t
            (full if is_full else sched).append(el)
            fresh = elm.freshness()
            for i, it in passive.items():
                age = fresh.get(it["id"].upper())
                hits[i][0] += 1
                if age is not None and age <= it["secs"]:
                    hits[i][1] += 1
                if age is not None and age <= 0.2:
                    hits[i][2] += 1
            await asyncio.sleep(max(0.005, interval - el))   # the reader's own pacing (--interval)
        wall = time.monotonic() - t_start
        row = summarise(load, sched, full, elm.frames - frames0, wall, time.process_time() - cpu0,
                        sum(e.cpu_s for e in ecus) - ecu0, {i: tuple(v) for i, v in hits.items()},
                        expected_fps("car", rd.VEHICLE.NAME, load), misses=len(elm.misses) - misses0,
                        broadcast=sum(e.sent for e in ecus) - sent0)
        row["items"] = sorted(r._items)
        log(f"  bus load {load:g}: {row['broadcast_fps']} fps broadcast, {row['intake_fps']} in, "
            f"sched {row['sched_median_ms']} ms, "
            f"full {row['full_median_ms']} ms, CPU {row['cpu_total_pct']} % ({row['cpu_ecu_pct']} % ECU)")
        return row
    finally:
        await elm.close()
        store.close()


def run(loads, seconds, scenario="drive", seed=1, log=print, interval=DEFAULT_INTERVAL):
    rows = []
    for load in loads:
        rows.append(asyncio.run(bench_one(load, seconds, scenario, seed, log, interval=interval)))
    return rows


def header(seconds, scenario, interval=DEFAULT_INTERVAL):
    return (f"bench_canrate — {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}, "
            f"{platform.system()} {platform.machine()}, Python {platform.python_version()}, "
            f"{seconds:g} s per load, reader interval {interval:g} s, scenario {scenario}. "
            f"MEASURED ON THE LAPTOP, VIRTUAL BUS: "
            f"no wire, no bit errors, no LBC pacing, no USB. Not evidence about a board or a car.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reader cycle time and intake over the simulated CAN bus.")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help=f"per bus load (default {DEFAULT_SECONDS:g})")
    ap.add_argument("--loads", default=",".join(str(v) for v in DEFAULT_LOADS),
                    help="comma list of bus_load values 0-1 (default 0.1,0.5,1.0)")
    ap.add_argument("--scenario", default="drive", help="simulator scenario (default drive)")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help=f"minimum seconds per reader cycle, as web/reader.py --interval (default {DEFAULT_INTERVAL:g})")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--log", default=None, metavar="PATH", help="append the markdown table to this file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    loads = parse_loads(args.loads)
    quiet = args.json
    rows = run(loads, args.seconds, args.scenario, args.seed, log=(lambda *a: None) if quiet else print,
               interval=args.interval)
    head = header(args.seconds, args.scenario, args.interval)
    table = format_table(rows)
    if args.json:
        import json
        print(json.dumps({"header": head, "rows": rows}, indent=1))
    else:
        print()
        print(head)
        print()
        print(table)
    if args.log:
        with open(args.log, "a") as f:
            f.write(f"\n### {head}\n\n{table}\n")
        if not quiet:
            print(f"\n  appended to {args.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
