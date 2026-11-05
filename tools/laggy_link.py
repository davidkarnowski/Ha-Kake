#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Laggy link — a loopback TCP proxy that delays, stalls or holds a connection on demand.

A reader on one side of a broker and a bridge on the other meet a real network
in between: a tunnel, a cellular hop, a link that goes quiet for twelve
seconds and then delivers everything at once. Nothing on a desk does that by
itself, so the faults that only appear on the road cannot be reproduced there.
This is the missing piece: point a client at this proxy instead of at the
broker and its connection gets the delay you ask for.

    python tools/laggy_link.py --listen 18884 --target 18883 --delay-ms 200
    python tools/laggy_link.py --listen 18884 --target 18883 --hold '"data":"21 05"' --hold-for 12.5

Three controls, all per direction (``up`` = client to target, ``down`` = target to client):

  delay   every chunk leaves this much later than it arrived (plus optional jitter);
  stall   nothing leaves for N seconds, then everything leaves in the order it
          arrived, which is what a TCP path that stopped and recovered looks like;
  hold    the next chunk that carries a byte pattern starts a stall, with that
          chunk held too. This is how one particular request is made late.

Bytes are never dropped, changed or reordered: it is a slow link, not a lossy one.

While it runs it reads commands from standard input:

    stall 12 up        delay 200 both        hold "data":"21 05" 12.5        stats        quit

It listens on the loopback address only, and by default it only connects to a
loopback target, so it cannot become a relay to anything else by accident.
``LaggyLink`` is the asyncio class; ``LaggyLinkThread`` runs one on its own
event loop for callers that are not async (tools/desk_rig.py).
"""
import argparse
import asyncio
import ipaddress
import random
import socket
import sys
import threading
import time

UP, DOWN, BOTH = "up", "down", "both"
CHUNK = 65536


def is_loopback(host):
    if str(host).strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).strip()).is_loopback
    except ValueError:
        return False


def directions(which):
    if which == BOTH:
        return (UP, DOWN)
    if which in (UP, DOWN):
        return (which,)
    raise ValueError(f"direction must be up, down or both, not {which!r}")


class LaggyLink:
    """One listening port, forwarded to one target port, through the controls above."""

    def __init__(self, listen_port, target_port, listen_host="127.0.0.1", target_host="127.0.0.1",
                 delay_up=0.0, delay_down=0.0, jitter=0.0, allow_remote_target=False, seed=None, log=None):
        if not is_loopback(listen_host):
            raise ValueError(f"laggy link listens on the loopback address only, not {listen_host!r}")
        if not is_loopback(target_host) and not allow_remote_target:
            raise ValueError(f"target {target_host!r} is not a loopback address; pass allow_remote_target "
                             "(--allow-remote-target) if you mean to delay a real link")
        self.listen_host, self.listen_port = listen_host, int(listen_port)
        self.target_host, self.target_port = target_host, int(target_port)
        self.delay = {UP: max(0.0, float(delay_up)), DOWN: max(0.0, float(delay_down))}
        self.jitter = max(0.0, float(jitter))
        self.log = log or (lambda *a: None)
        self.stats = {"connections": 0, "bytes_up": 0, "bytes_down": 0, "stalls": 0, "holds_fired": 0}
        self.events = []                       # (wall time, text): what was done to the link, and when
        self._stall_until = {UP: 0.0, DOWN: 0.0}   # event-loop time
        self._holds = []                       # dicts: direction, pattern, seconds, once
        self._tail = {}                        # (connection, direction) → last bytes, for patterns split across chunks
        self._rng = random.Random(seed)
        self._server = None
        self._conns = set()

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self):
        self._server = await asyncio.start_server(self._accept, self.listen_host, self.listen_port,
                                                  family=socket.AF_INET)
        self.listen_port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)

    # ── controls (call on the event loop) ────────────────────────────────

    def _note(self, text):
        self.events.append((time.time(), text))
        self.log(text)

    def set_delay(self, seconds, direction=BOTH):
        for d in directions(direction):
            self.delay[d] = max(0.0, float(seconds))
        self._note(f"delay {float(seconds) * 1000:.0f} ms {direction}")

    def stall(self, seconds, direction=BOTH):
        """Nothing leaves in `direction` for `seconds`; then it all leaves, in order."""
        now = asyncio.get_running_loop().time()
        for d in directions(direction):
            self._stall_until[d] = max(self._stall_until[d], now + max(0.0, float(seconds)))
        self.stats["stalls"] += 1
        self._note(f"stall {float(seconds):.1f} s {direction}")

    def hold_on(self, pattern, seconds, direction=UP, once=True):
        """Stall `direction` for `seconds` starting with the next chunk that carries `pattern`."""
        if isinstance(pattern, str):
            pattern = pattern.encode()
        if not pattern:
            raise ValueError("hold needs a non-empty pattern")
        for d in directions(direction):
            self._holds.append({"direction": d, "pattern": bytes(pattern), "seconds": float(seconds),
                                "once": bool(once)})
        self._note(f"hold armed: {pattern!r} for {float(seconds):.1f} s {direction}")

    def stalled(self, direction):
        return self._stall_until[direction] > asyncio.get_running_loop().time()

    # ── the proxy ────────────────────────────────────────────────────────

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self._conns.add(task)
        t_writer = None
        try:
            try:
                t_reader, t_writer = await asyncio.open_connection(self.target_host, self.target_port,
                                                                   family=socket.AF_INET)
            except OSError as e:
                self.log(f"target {self.target_host}:{self.target_port} refused: {e}")
                return
            self.stats["connections"] += 1
            key = id(task)
            pumps = [asyncio.ensure_future(self._pump(reader, t_writer, UP, key)),
                     asyncio.ensure_future(self._pump(t_reader, writer, DOWN, key))]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for p in pumps:
                    p.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            for w in (writer, t_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass
            for d in (UP, DOWN):
                self._tail.pop((id(task), d), None)
            self._conns.discard(task)

    def _check_holds(self, direction, data, key):
        if not self._holds:
            return
        tail = self._tail.get((key, direction), b"")
        window = tail + data
        longest = max(len(h["pattern"]) for h in self._holds)
        self._tail[(key, direction)] = window[-(longest - 1):] if longest > 1 else b""
        for h in list(self._holds):
            if h["direction"] == direction and h["pattern"] in window:
                if h["once"]:
                    self._holds.remove(h)
                self.stats["holds_fired"] += 1
                self._note(f"hold fired on {h['pattern']!r}")
                self.stall(h["seconds"], direction)

    async def _pump(self, reader, writer, direction, key):
        """Read as fast as the sender sends; release each chunk after its delay
        and after any stall, in arrival order."""
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()

        async def intake():
            last = 0.0
            while True:
                try:
                    data = await reader.read(CHUNK)
                except (ConnectionError, OSError):
                    data = b""
                now = loop.time()
                if data:
                    self._check_holds(direction, data, key)      # may stall this very chunk
                    wait = self.delay[direction] + (self._rng.uniform(0.0, self.jitter) if self.jitter else 0.0)
                    last = max(now + wait, last)                 # never overtake an earlier chunk
                await queue.put((last, data))
                if not data:
                    return

        async def outlet():
            while True:
                release, data = await queue.get()
                while True:
                    wait = max(release, self._stall_until[direction]) - loop.time()
                    if wait <= 0:
                        break
                    await asyncio.sleep(min(wait, 0.02))
                if not data:
                    return
                writer.write(data)
                await writer.drain()
                self.stats["bytes_" + direction] += len(data)

        feeder = asyncio.ensure_future(intake())
        try:
            await outlet()
        except (ConnectionError, OSError):
            pass
        finally:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)


class LaggyLinkThread:
    """A LaggyLink on its own event-loop thread, with thread-safe controls."""

    def __init__(self, **kw):
        self.kw = kw
        self.link = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._error = None

    def start(self, timeout=5.0):
        def main():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                self.link = LaggyLink(**self.kw)
                loop.run_until_complete(self.link.start())
            except Exception as e:                       # bad arguments, port in use
                self._error = e
                self._ready.set()
                loop.close()
                return
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(self.link.stop())
                loop.close()

        self._thread = threading.Thread(target=main, name="laggy-link", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("laggy link did not start")
        if self._error is not None:
            raise self._error
        return self

    @property
    def port(self):
        return self.link.listen_port

    @property
    def stats(self):
        return dict(self.link.stats)

    @property
    def events(self):
        return list(self.link.events)

    def _call(self, fn, *args, **kw):
        done = threading.Event()
        err = []

        def run():
            try:
                fn(*args, **kw)
            except Exception as e:
                err.append(e)
            done.set()

        self._loop.call_soon_threadsafe(run)
        done.wait(2.0)
        if err:
            raise err[0]

    def stall(self, seconds, direction=BOTH):
        self._call(self.link.stall, seconds, direction)

    def set_delay(self, seconds, direction=BOTH):
        self._call(self.link.set_delay, seconds, direction)

    def hold_on(self, pattern, seconds, direction=UP, once=True):
        self._call(self.link.hold_on, pattern, seconds, direction, once)

    def stop(self):
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)


# ── command line ─────────────────────────────────────────────────────────

def run_command(link, line, log=print):
    """One control line. Returns False when it asks to quit."""
    parts = line.split()
    if not parts:
        return True
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd == "stats":
            log(f"{link.stats}  delay {link.delay}")
        elif cmd == "stall":
            link.stall(float(args[0]), args[1] if len(args) > 1 else BOTH)
        elif cmd == "delay":
            link.set_delay(float(args[0]) / 1000.0, args[1] if len(args) > 1 else BOTH)
        elif cmd == "hold":
            # the pattern is taken verbatim (it is usually JSON, quotes and all); the last word is the seconds
            pattern, _, seconds = line.strip()[len(parts[0]):].strip().rpartition(" ")
            link.hold_on(pattern.strip(), float(seconds))
        else:
            log("commands: stall <s> [up|down|both] | delay <ms> [up|down|both] | hold <text> <s> | stats | quit")
    except (IndexError, ValueError) as e:
        log(f"not understood ({e}): {line.strip()}")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--listen", type=int, required=True, help="loopback port clients connect to")
    ap.add_argument("--target", type=int, required=True, help="port the real service listens on")
    ap.add_argument("--target-host", default="127.0.0.1")
    ap.add_argument("--allow-remote-target", action="store_true",
                    help="let --target-host be a non-loopback address (you are delaying a real link)")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="delay in both directions")
    ap.add_argument("--delay-up-ms", type=float, default=None)
    ap.add_argument("--delay-down-ms", type=float, default=None)
    ap.add_argument("--jitter-ms", type=float, default=0.0, help="extra random delay, 0 to this, per chunk")
    ap.add_argument("--hold", default=None, metavar="TEXT", help="stall the upward direction when a chunk carries TEXT")
    ap.add_argument("--hold-for", type=float, default=12.5, help="seconds a --hold lasts (default 12.5)")
    args = ap.parse_args(argv)
    say = lambda s: print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

    async def serve():
        up = args.delay_up_ms if args.delay_up_ms is not None else args.delay_ms
        down = args.delay_down_ms if args.delay_down_ms is not None else args.delay_ms
        link = LaggyLink(args.listen, args.target, target_host=args.target_host, delay_up=up / 1000.0,
                         delay_down=down / 1000.0, jitter=args.jitter_ms / 1000.0,
                         allow_remote_target=args.allow_remote_target, log=say)
        await link.start()
        say(f"127.0.0.1:{link.listen_port} -> {args.target_host}:{args.target} "
            f"(delay up {up:.0f} ms, down {down:.0f} ms, jitter {args.jitter_ms:.0f} ms)")
        if args.hold:
            link.hold_on(args.hold, args.hold_for)
        loop = asyncio.get_running_loop()
        lines = asyncio.Queue()

        def feed():
            for line in sys.stdin:
                loop.call_soon_threadsafe(lines.put_nowait, line)
            loop.call_soon_threadsafe(lines.put_nowait, None)

        threading.Thread(target=feed, daemon=True).start()
        try:
            while True:
                line = await lines.get()
                if line is None:                       # stdin closed: keep serving until interrupted
                    await asyncio.Event().wait()
                if not run_command(link, line, say):
                    break
        finally:
            await link.stop()
            say(f"stopped: {link.stats}")

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
