#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
bench_transport.py — how fast can the USB ELM327 actually go?

A developer tool, not part of the dashboard. It opens the serial adapter
directly (no project transport in the hot path, so the numbers are the
adapter's, not ours), times round-trips, and prints a table.

What it separates:

  * **chip time** — how long the adapter takes to answer, measured with a
    blocking `read_until('>')`, i.e. the floor the hardware imposes.
  * **our time** — the same command through the sleep-polling loop the
    transport used to use (`--compare-poll`), which rounds every answer up
    to the next 50 ms tick and then sleeps `wait` on top.
  * **wire time** — bytes returned ÷ (baud / 10), printed alongside, so a
    long answer at a low baud is visibly wire-limited rather than mysterious.

Safety: read-only. It sends AT commands, UDS *read* service 0x21 and monitor
mode (ATMA) only — never a write, control, routine or security service. It is
safe with the car asleep: the AT round-trip is the measurement that matters
most and the UDS reads simply answer NO DATA.

Usage:
  python tools/bench_transport.py                       # default sweep
  python tools/bench_transport.py --port /dev/tty.usbserial-XXX
  python tools/bench_transport.py --bauds 38400,115200,500000 -n 30
  python tools/bench_transport.py --no-uds              # AT only (car asleep)
  python tools/bench_transport.py --compare-poll        # include the old loop

The port comes from --port, else HAKAKE_SERIAL_PORT, else the usual glob —
the device path is machine-specific and never belongs in a committed file.

The timing self-test (docs/TIMING.md) is a different measurement: it runs the
reader's own `poll_once` over any adapter — the real one, or replay / sim
with no car — against a throwaway in-memory store, and reports what the
timestamps are made of: cycle jitter, the per-item `timing` distribution,
and how the source's clock drifts against this machine's (native CAN, MQTT).
The numbers the docs quote come from here, never from a model.

  python tools/bench_transport.py --timing --adapter replay --seconds 20
  python tools/bench_transport.py --timing --adapter usb --seconds 600
  python tools/bench_transport.py --timing --adapter mqtt --seconds 600 --json
"""

import argparse
import glob
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ELM327 ATBRD divisors: baud = 4_000_000 / divisor
BRD_DIVISOR = {
    9600: 0x1A0, 19200: 0xD0, 38400: 0x68, 57600: 0x45,
    115200: 0x23, 230400: 0x11, 250000: 0x10, 500000: 0x08, 1000000: 0x04,
}


def find_port(explicit=None):
    if explicit:
        return explicit
    forced = os.environ.get("HAKAKE_SERIAL_PORT")
    if forced:
        return forced
    for pattern in ("/dev/tty.usbserial-*", "/dev/cu.usbserial-*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


class Link:
    """A bare pyserial link with two timing modes, for comparison."""

    def __init__(self, port, baud):
        import serial as pyserial
        self.ser = pyserial.Serial(
            port=port, baudrate=baud, timeout=1, write_timeout=1)
        self.baud = baud
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def set_baud(self, baud):
        self.ser.baudrate = baud
        self.baud = baud

    # — blocking read: latency bounded by the adapter, not by a poll tick —
    def cmd(self, text, timeout=4.0, wait=0.0):
        self.ser.reset_input_buffer()
        self.ser.write((text + "\r").encode("ascii"))
        self.ser.timeout = timeout
        t0 = time.perf_counter()
        data = self.ser.read_until(b">")
        el = time.perf_counter() - t0
        if wait:
            time.sleep(wait)
        return el, data

    # — the old transport loop, replicated exactly, for the before/after —
    def cmd_polled(self, text, timeout=4.0, wait=0.0, tick=0.05):
        self.ser.reset_input_buffer()
        self.ser.write((text + "\r").encode("ascii"))
        t0 = time.perf_counter()
        data = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting > 0:
                data += self.ser.read(self.ser.in_waiting)
                if b">" in data:
                    break
            else:
                time.sleep(tick)
        el = time.perf_counter() - t0
        if wait:
            time.sleep(wait)
        return el, data

    # — monitor mode: ATMA for `secs`, then a byte to stop it —
    def monitor(self, can_id, secs=0.2):
        self.cmd("ATCRA " + can_id, timeout=1.0)
        self.ser.reset_input_buffer()
        self.ser.write(b"ATMA\r")
        t0 = time.perf_counter()
        self.ser.timeout = secs
        data = self.ser.read_until(b">")
        self.ser.write(b"\r")
        self.ser.timeout = 1.0
        data += self.ser.read_until(b">")
        return time.perf_counter() - t0, data


def switch_baud(link, target, log=print):
    """ATBRD to `target`, verified. Returns True if the adapter really moved.

    The ELM327 handshake: it answers OK at the old rate, sends its ID at the
    new rate, we echo a CR, it answers OK. Anything missing and we put the
    host back where it was — the adapter reverts on its own after the
    timeout, so a failed negotiation is not a lost link.
    """
    div = BRD_DIVISOR.get(target)
    if div is None:
        return False
    old = link.baud
    if target == old:
        return True
    link.ser.reset_input_buffer()
    link.ser.write(f"ATBRD {div:02X}\r".encode("ascii"))
    link.ser.timeout = 1.0
    # Read only the OK line, NOT the prompt: after ATBRD the adapter sends its
    # ID at the *new* rate and expects a CR back within ~75 ms. Waiting for a
    # '>' here spends that window and the negotiation fails every time — which
    # is exactly why the first run of this tool concluded the clone could not
    # switch. It can.
    ack = b""
    for _ in range(3):
        ack = link.ser.read_until(b"\r")
        if not ack.strip() or b"ATBRD" not in ack.upper():
            break
    if b"OK" not in ack.upper():
        log(f"    ATBRD {div:02X}: adapter said {ack!r} — not supported")
        return False
    link.set_baud(target)
    link.ser.timeout = 0.3
    ident = link.ser.read_until(b"\r")
    if not ident.strip():
        link.set_baud(old)
        time.sleep(0.3)
        link.ser.reset_input_buffer()
        log(f"    ATBRD {div:02X}: silence at {target} — reverted to {old}")
        return False
    link.ser.write(b"\r")
    link.ser.timeout = 0.5
    ok = link.ser.read_until(b">")
    if b"OK" not in ok.upper():
        link.set_baud(old)
        time.sleep(0.3)
        link.ser.reset_input_buffer()
        log(f"    ATBRD {div:02X}: no confirm at {target} — reverted to {old}")
        return False
    link.ser.reset_input_buffer()
    return True


def revert_baud(link):
    """ATZ at whatever rate we are on: the ELM forgets ATBRD on reset."""
    if link.baud == 38400:
        return
    try:
        link.ser.write(b"ATZ\r")
        time.sleep(1.2)
        link.set_baud(38400)
        time.sleep(0.3)
        link.ser.reset_input_buffer()
    except Exception:
        pass


def stats(samples):
    if not samples:
        return None
    s = sorted(samples)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    return (s[0], statistics.median(s), p95, s[-1])


def fmt(st):
    if st is None:
        return "        —"
    return f"{st[0]*1000:5.0f}/{st[1]*1000:5.0f}/{st[2]*1000:5.0f}"


def configure(link):
    """Project UDS setup — exactly elm327.configure_uds()'s command list."""
    link.cmd("ATZ", timeout=3.0)
    time.sleep(0.5)
    link.ser.reset_input_buffer()
    for c in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6", "ATSH 79B", "ATCRA 7BB",
              "ATCAF1", "ATFCSH 79B", "ATFCSD 30 00 20", "ATFCSM1"):
        link.cmd(c, timeout=2.0)


def bytes_of(data):
    return len(data)


def run_case(port, baud, at, st, n, do_uds, do_passive, compare_poll, log=print):
    link = Link(port, 38400)
    try:
        # ATZ first, THEN the baud switch: ATZ is a reset and puts the chip
        # straight back to 38400. Doing it the other way round made the first
        # run of this tool time out on every command at 115200.
        configure(link)
        if baud != 38400:
            if not switch_baud(link, baud, log=log):
                link.close()
                return None
        if at is not None:
            link.cmd(f"ATAT{at}", timeout=2.0)
        if st is not None:
            link.cmd(f"ATST {st:02X}", timeout=2.0)

        rows = []

        def bench(name, fn, reps, polled_fn=None):
            times, size = [], 0
            for _ in range(reps):
                el, data = fn()
                times.append(el)
                size = max(size, bytes_of(data))
            ptimes = []
            if polled_fn and compare_poll:
                for _ in range(reps):
                    el, _d = polled_fn()
                    ptimes.append(el)
            rows.append((name, stats(times), size, stats(ptimes)))

        bench("ATI", lambda: link.cmd("ATI", timeout=2.0), n,
              lambda: link.cmd_polled("ATI", timeout=2.0))
        bench("ATRV", lambda: link.cmd("ATRV", timeout=2.0), n,
              lambda: link.cmd_polled("ATRV", timeout=2.0))
        # A big answer with no car involved: ATPPS dumps every programmable
        # parameter, ~420 bytes, which is the same order as the 29-frame 2102
        # the Leaf's cell-voltage read returns. It is the only way to see wire
        # time without a woken car, and it is read-only.
        bench("ATPPS (420 B)", lambda: link.cmd("ATPPS", timeout=4.0), max(3, n // 2),
              lambda: link.cmd_polled("ATPPS", timeout=4.0))
        if do_uds:
            bench("2101", lambda: link.cmd("2101", timeout=8.0), max(3, n // 3),
                  lambda: link.cmd_polled("2101", timeout=8.0))
            bench("2102", lambda: link.cmd("2102", timeout=10.0), max(3, n // 5),
                  lambda: link.cmd_polled("2102", timeout=10.0))
        if do_passive:
            link.cmd("ATCAF0", timeout=2.0)
            bench("ATMA 421 (0.2s)", lambda: link.monitor("421", 0.2), max(3, n // 5))
            link.cmd("ATCAF1", timeout=2.0)
        return rows
    except Exception as e:
        log(f"    {type(e).__name__}: {e} — case abandoned")
        return None
    finally:
        # Leave the adapter at its default rate so the next open just works.
        revert_baud(link)
        link.close()


def model_cycle(rows, baud, uds_short=7, uds_long=1, long_bytes=750,
                short_bytes=60, switches=3, passive_secs=3.1, passive_items=11):
    """A full Leaf cycle, built from the measured numbers rather than guessed.

    Defaults describe the leaf_ze0 profile with every tile on: 8 UDS reads (one
    of them 2102, 29 frames ≈ 750 bytes), 3 ECU switches of 4 AT commands each,
    and 11 passive captures whose ATMA dwell adds up to 3.1 s. The dwell is
    wall-clock by definition — no transport can make it shorter — so it is
    reported separately from the part we control.
    """
    byte_s = 10.0 / baud
    # Fixed per-command cost = measured time minus the wire time of the bytes
    # that actually came back. The big answer is the best anchor: its wire time
    # dominates, so the remainder is the chip + USB + our code.
    seen = {name: (st[1], sz) for name, st, sz, _ps in rows if st}
    anchor = seen.get("ATPPS (420 B)") or seen.get("2101") or seen.get("ATI")
    if anchor is None:
        return None
    fixed = max(0.0, anchor[0] - anchor[1] * byte_s)
    def cost(nbytes):
        return fixed + nbytes * byte_s
    uds = uds_short * cost(short_bytes) + uds_long * cost(long_bytes)
    at = switches * 4 * cost(20)
    passive = passive_secs + passive_items * cost(20)
    return uds, at, passive, fixed


# ── the timing self-test (docs/TIMING.md §5) ─────────────────────────────

def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, max(0, int(round((len(sorted_vals) - 1) * p))))
    return sorted_vals[k]


def _dist(vals):
    """{n, min, med, p95, max} of a list of seconds (None-free), rounded to ms."""
    v = sorted(x for x in vals if x is not None)
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": round(v[0], 4), "med": round(statistics.median(v), 4),
            "p95": round(_pct(v, 0.95), 4), "max": round(v[-1], 4)}


async def timing_run(adapter=None, seconds=20.0, interval=0.5, budget=1.5, fast=False, log=print):
    """Run the reader's poll_once over `adapter` for `seconds` and measure.

    Returns a dict: `adapter` (type / name / ts_source / clock the rows use),
    `cycles` (count, the cycle-period distribution and its jitter = stdev),
    `items` ({item: distribution of `timing`}), `ages` ({item: distribution of
    item_age at emission}), `offset` (clock_offset_s samples over the run:
    first / last / min / max / drift per minute — only a transport with a
    source clock has one) and `rows` (how many the throwaway store took).
    Nothing here touches the real database or the state file.
    """
    import asyncio
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))
    import reader as rd                                  # noqa: E402
    from store import Store                              # noqa: E402
    from elm327 import detect_adapter                    # noqa: E402

    class Quiet(rd.Reader):
        def publish(self, status, message=None, **fields):   # no state file, no MQTT
            pass

    store = Store(":memory:")
    r = Quiet(interval, adapter, fast=fast, store=store, budget=budget)
    elm = await detect_adapter(prefer=adapter, log=log)
    try:
        await rd.configure_vehicle(elm)
        r.target = None
        r.speed = float(getattr(elm, "SPEED", 1.0) or 1.0)
        r.passive_instant = bool(getattr(elm, "PASSIVE_INSTANT", False))
        r.ts_source = str(getattr(elm, "ts_source", None) or "laptop")
        r.session_id = store.start_session(elm.adapter_type, note="timing self-test")
        r.refresh_items()
        loop = asyncio.get_event_loop()
        t_end = loop.time() + seconds
        starts, periods, timings, ages, offsets = [], [], {}, {}, []
        last_start = None
        rows = 0
        last_store = loop.time()
        while loop.time() < t_end:
            t0 = loop.time()
            rec, alive = await r.poll_once(elm)
            if not rec.get("timing"):
                await asyncio.sleep(min(0.5, max(0.05, r.next_due(loop.time()))))
                continue
            if last_start is not None:
                periods.append(t0 - last_start)
            last_start = t0
            starts.append(t0)
            for i, t in rec["timing"].items():
                timings.setdefault(i, []).append(t)
            for i, a in (rec.get("item_age") or {}).items():
                ages.setdefault(i, []).append(a)
            if rec.get("clock_offset_s") is not None:
                offsets.append((t0, rec["clock_offset_s"]))
            if loop.time() - last_store >= rd.STORE_PERIOD:
                row = dict(rec)
                row.update(r.take_peaks())
                store.insert_reading(row, adapter=elm.adapter_type, ts_source=r.ts_source)
                rows += 1
                last_store = loop.time()
            await asyncio.sleep(max(0.0, interval - (loop.time() - t0)))
    finally:
        try:
            await elm.close()
        except Exception:
            pass
        store.close()

    out = {
        "adapter": {"type": getattr(elm, "adapter_type", "?"), "name": getattr(elm, "adapter_name", ""),
                    "ts_source": r.ts_source, "passive_instant": r.passive_instant, "speed": r.speed},
        "seconds": round(seconds, 1),
        "cycles": {"n": len(starts), "period": _dist(periods),
                   "jitter_s": round(statistics.pstdev(periods), 4) if len(periods) > 1 else None},
        "items": {i: _dist(v) for i, v in sorted(timings.items())},
        "ages": {i: _dist(v) for i, v in sorted(ages.items())},
        "rows": rows,
    }
    if offsets:
        first, last = offsets[0], offsets[-1]
        span = last[0] - first[0]
        vals = [o for _, o in offsets]
        out["offset"] = {"n": len(offsets), "first_s": first[1], "last_s": last[1],
                         "min_s": min(vals), "max_s": max(vals),
                         "drift_s_per_min": round((last[1] - first[1]) / span * 60, 5) if span > 0 else None}
    else:
        out["offset"] = None
    return out


def print_timing(res):
    a = res["adapter"]
    print(f"adapter: {a['type']} ({a['name']})  rows stamped by the {a['ts_source']} clock"
          f"{'  passive instant' if a['passive_instant'] else ''}  est x{a['speed']:g}")
    c = res["cycles"]
    p = c["period"]
    if p.get("n"):
        print(f"cycles:  {c['n']} in {res['seconds']}s — period min/med/p95/max "
              f"{p['min']*1000:.0f}/{p['med']*1000:.0f}/{p['p95']*1000:.0f}/{p['max']*1000:.0f} ms, "
              f"jitter (stdev) {c['jitter_s']*1000:.0f} ms")
    else:
        print(f"cycles:  {c['n']} (not enough for a period)")
    print(f"{'item':<10} {'n':>4} {'min':>7} {'med':>7} {'p95':>7} {'max':>7}   {'age at emission (med/max)':>26}")
    for i, d in res["items"].items():
        ag = res["ages"].get(i) or {}
        age = f"{ag.get('med', 0):.1f}s / {ag.get('max', 0):.1f}s" if ag.get("n") else ""
        print(f"{i:<10} {d['n']:>4} {d['min']*1000:7.0f} {d['med']*1000:7.0f} {d['p95']*1000:7.0f} "
              f"{d['max']*1000:7.0f}   {age:>26}")
    o = res.get("offset")
    if o:
        print(f"source clock vs this machine: offset {o['first_s']:+.4f}s → {o['last_s']:+.4f}s over "
              f"{o['n']} cycles (min {o['min_s']:+.4f}, max {o['max_s']:+.4f}), "
              f"drift {o['drift_s_per_min']:+.5f} s/min")
    else:
        print("source clock: none (an ELM adapter has no timestamps of its own; rows use this machine's clock)")
    print(f"rows the throwaway store took: {res['rows']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timing", action="store_true",
                    help="the timing self-test: the reader's own cycle over --adapter (docs/TIMING.md)")
    ap.add_argument("--adapter", default=None,
                    help="--timing: usb | ble | replay | sim | can | mqtt (default auto)")
    ap.add_argument("--seconds", type=float, default=20.0, help="--timing: how long to run (default 20)")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="--timing: minimum seconds per cycle, the reader's own default (0.5)")
    ap.add_argument("--fast", action="store_true", help="--timing: fast-lane primary item only")
    ap.add_argument("--json", action="store_true", help="--timing: print the result as JSON")
    ap.add_argument("--port", help="serial device (default: $HAKAKE_SERIAL_PORT or glob)")
    ap.add_argument("--bauds", default="38400,115200,500000",
                    help="comma-separated baud rates to try (default 38400,115200,500000)")
    ap.add_argument("--at", default="1", help="comma-separated ATAT values, or 'skip'")
    ap.add_argument("--st", default="", help="comma-separated ATST values in ms (e.g. 32,100)")
    ap.add_argument("-n", type=int, default=20, help="repetitions per command (default 20)")
    ap.add_argument("--no-uds", action="store_true", help="AT commands only")
    ap.add_argument("--no-passive", action="store_true", help="skip ATMA capture")
    ap.add_argument("--compare-poll", action="store_true",
                    help="also time the old 50 ms sleep-polling loop")
    args = ap.parse_args()

    if args.timing:
        import asyncio
        import json
        pref = None if args.adapter in (None, "auto") else args.adapter
        res = asyncio.run(timing_run(pref, seconds=args.seconds, interval=args.interval, fast=args.fast))
        if args.json:
            print(json.dumps(res, indent=1))
        else:
            print_timing(res)
        return 0

    port = find_port(args.port)
    if not port:
        print("No serial port found. Pass --port or set HAKAKE_SERIAL_PORT.")
        return 1
    print(f"Adapter port: {os.path.basename(port)}   (path hidden — machine-specific)")

    bauds = [int(b) for b in args.bauds.split(",") if b.strip()]
    ats = [] if args.at == "skip" else [int(a) for a in args.at.split(",") if a.strip()]
    sts = [None] if not args.st else [int(s) for s in args.st.split(",")]

    header = f"{'case':>26}  {'command':<16} {'min/med/p95 ms':>19} {'bytes':>6} {'wire ms':>8}"
    if args.compare_poll:
        header += f" {'polled min/med/p95':>21} {'cost':>7}"
    print()
    print(header)
    print("-" * len(header))

    for baud in bauds:
        for at in (ats or [None]):
            for st in sts:
                label = f"{baud} AT{at if at is not None else '-'}" + (f" ST{st}" if st else "")
                rows = run_case(port, baud, at, st, args.n,
                                not args.no_uds, not args.no_passive,
                                args.compare_poll)
                if rows is None:
                    print(f"{label:>26}  (adapter would not switch to this baud — skipped)")
                    continue
                m = model_cycle(rows, baud)
                for name, s, size, ps in rows:
                    wire = size * 10 / baud * 1000
                    line = (f"{label:>26}  {name:<16} {fmt(s):>19} {size:6d} {wire:8.1f}")
                    if args.compare_poll:
                        cost = ""
                        if ps and s:
                            cost = f"{(ps[1]-s[1])*1000:+.0f}ms"
                        line += f" {fmt(ps):>21} {cost:>7}"
                    print(line)
                    label = ""
                if m:
                    uds, at, passive, fixed = m
                    print(f"{'':>26}  modelled full Leaf cycle: "
                          f"{uds:.2f}s UDS + {at:.2f}s ECU switches + {passive:.2f}s passive "
                          f"= {uds+at+passive:.2f}s   (per-command fixed cost {fixed*1000:.0f} ms; "
                          f"{passive:.2f}s of the passive figure is ATMA dwell, not transport)")
                print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
