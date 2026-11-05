#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Desk rig — the whole MQTT chain on one laptop, with a link that can be made late.

    simulated ECUs ── virtual CAN ── bridge ──▶ mosquitto ◀── laggy link ◀── reader + dashboard
                                              (loopback)

Everything a remote car puts between its CAN bus and the page, with nothing
real in it: the simulator's ECUs answer on an in-process virtual bus, the real
bridge (bridge/hakake_bridge.py) forwards it to a throwaway mosquitto on the
loopback address, and the real application (web/app.py --adapter mqtt) reads
it through tools/laggy_link.py, which can delay the link or hold one request
back. That last part is the point: faults that need a slow network to appear
can be produced here on demand.

    python tools/desk_rig.py                          # bring the chain up; start the dashboard yourself
    python tools/desk_rig.py --scenario stale-reply   # reproduce the stale-reply fault, report, exit
    python tools/desk_rig.py --scenario stale-reply --expect clean    # the same, as a check (exit 1 if not)

The stale-reply scenario holds one request back for longer than the reader
waits for it. The reader gives up and asks for something else; the bridge then
runs both; the first reply lands in the second request's window. It watches
the dashboard's live state and the stored rows for values no car can produce
and says what it saw.

Kept apart from any real car, three ways. The rig refuses the default topic
prefix (it uses ``hakake/desk``), so it cannot share a topic with a live
bridge even on the same broker. It gives the application its own data folder
(``HAKAKE_DATA_DIR``), so no row reaches a real database. And every port is on
the loopback address. Needs ``mosquitto`` installed (``brew install mosquitto``,
``apt install mosquitto``); nothing else beyond the project's requirements.

SIMULATED DATA throughout: the simulator is a fixture, not a verifier.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "bridge"), os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DESK_PREFIX = "hakake/desk"
DB_FILE = "leaf_battery.db"

# Values no car can produce, by the registry's own keys: (low, high), inclusive.
LIMITS = {"soc": (0.0, 100.0), "lv_volts": (6.0, 18.0), "pack_v": (200.0, 450.0),
          "cell_min": (2500, 4300), "cell_max": (2500, 4300)}


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── small pieces (tested without a broker) ───────────────────────────────

def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_mosquitto():
    found = shutil.which("mosquitto")
    if found:
        return found
    for d in ("/opt/homebrew/sbin", "/usr/local/sbin", "/usr/sbin", "/opt/homebrew/opt/mosquitto/sbin"):
        p = os.path.join(d, "mosquitto")
        if os.path.exists(p):
            return p
    return None


def broker_conf(port):
    return (f"listener {int(port)} 127.0.0.1\n"
            "allow_anonymous true\n"
            "persistence false\n")


def implausible(rec):
    """The keys of `rec` whose values no car can produce: {key: value}."""
    out = {}
    for key, (lo, hi) in LIMITS.items():
        v = (rec or {}).get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if not lo <= v <= hi:
            out[key] = v
    return out


def spaced(cmd):
    """'2105' -> '21 05', the way a request's data travels."""
    cmd = cmd.replace(" ", "")
    return " ".join(cmd[i:i + 2] for i in range(0, len(cmd), 2))


def scenario_plan(mod, hold_item=None, grace=1.0):
    """What the stale-reply scenario needs from a vehicle profile: the item the
    reader asks for every cycle, a second request item on the same ECU to hold
    back, the built-in tiles that poll just those two, and how long to hold."""
    fast = sorted(getattr(mod, "FAST_ONLY", ()) or ())
    if not fast:
        raise SystemExit("this profile has no fast-lane item; the scenario needs one")
    fast = fast[0]
    kind = mod.ITEMS[fast]["kind"]
    if not mod.TARGETS.get(kind):
        raise SystemExit(f"the fast-lane item {fast!r} is not a request item")
    if hold_item is None:
        others = [i for i, it in mod.ITEMS.items() if it["kind"] == kind and i != fast and it.get("period")]
        if not others:
            raise SystemExit(f"this profile has no second request item on the same ECU as {fast!r}")
        hold_item = min(others, key=lambda i: (mod.ITEMS[i]["period"], i))
    elif hold_item not in mod.ITEMS or mod.ITEMS[hold_item]["kind"] != kind or hold_item == fast:
        raise SystemExit(f"--hold-item must be another request item on the same ECU as {fast!r}")
    wanted = {fast, hold_item}
    tiles = [t["id"] for t in mod.TILES if set(t["items"]) <= wanted]
    if not any(hold_item in t["items"] for t in mod.TILES if t["id"] in tiles):
        tiles += [t["id"] for t in mod.TILES if hold_item in t["items"]][:1]
    timeout = float(mod.ITEMS[hold_item].get("timeout", 8.0))
    return {"fast": fast, "hold": hold_item, "tiles": tiles,
            "pattern": f'"data":"{spaced(mod.ITEMS[hold_item]["cmd"])}"',
            "hold_s": round(timeout + grace + 1.5, 1)}


def tiles_config(mod, enabled, celllog=()):
    """A <data>/tiles.json that enables exactly `enabled` (ids), every other default tile off."""
    from vehicles import default_tiles
    out = []
    for t in default_tiles(mod):
        t = dict(t, enabled=t["id"] in enabled)
        if t["id"] in celllog:
            t["opts"] = dict(t.get("opts") or {}, celllog=True)
        out.append(t)
    return {"tiles": out}


def dashboard_env(data_dir, port, prefix, bus="car", base=None):
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k.startswith(("HAKAKE_MQTT_", "HAKAKE_SIM_", "HAKAKE_ADAPTER")):
            del env[k]
    env.update({"HAKAKE_DATA_DIR": data_dir, "HAKAKE_MQTT_HOST": "127.0.0.1", "HAKAKE_MQTT_PORT": str(port),
                "HAKAKE_MQTT_PREFIX": prefix, "HAKAKE_MQTT_BUS": bus})
    return env


def check_prefix(prefix):
    import mqttsource
    live = mqttsource.DEFAULTS["prefix"].strip("/")
    p = prefix.strip("/")
    if p == live or p.startswith(live + "/"):
        raise SystemExit(f"the rig will not use the prefix {prefix!r}: that is where a real car publishes. "
                         f"Use {DESK_PREFIX!r} or another prefix of your own.")
    return p


def stored_rows(db_path):
    """Every stored row of the run, oldest first, as dicts (read-only)."""
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in con.execute("pragma table_info(readings)")]
        keep = ["id", "ts_epoch"] + [c for c in LIMITS if c in cols]
        return [dict(r) for r in con.execute(f"select {', '.join(keep)} from readings order by ts_epoch")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


# ── the chain ────────────────────────────────────────────────────────────

class Chain:
    """Broker, simulated ECUs, bridge and laggy link, started in that order and
    stopped in reverse. `leg` says whose connection goes through the link:
    the reader's (a slow path to the laptop) or the bridge's (a slow path to the car)."""

    def __init__(self, scratch, prefix=DESK_PREFIX, vehicle="leaf_ze0", leg="reader", sim_scenario=None,
                 seed=1, bus_load=0.0, delay_ms=0.0, link_log=None):
        self.scratch, self.prefix, self.vehicle, self.leg = scratch, check_prefix(prefix), vehicle, leg
        self.link_log = link_log
        self.sim_scenario, self.seed, self.bus_load, self.delay_ms = sim_scenario, seed, bus_load, delay_ms
        self.broker_port, self.link_port = free_port(), None
        self.broker = self.ecu = self.bridge = self.link = self.client = None
        self._status_stop = threading.Event()
        self._buses = []

    @property
    def reader_port(self):
        return self.link_port if self.leg == "reader" else self.broker_port

    def start(self):
        import can
        import hakake_bridge as hb
        from laggy_link import LaggyLinkThread
        from simulator import make_sim
        from simulator.canbus import SimCanEcu

        exe = find_mosquitto()
        if not exe:
            raise SystemExit("mosquitto is not installed (brew install mosquitto / apt install mosquitto)")
        conf = os.path.join(self.scratch, "mosquitto.conf")
        with open(conf, "w") as f:
            f.write(broker_conf(self.broker_port))
        self._broker_log = open(os.path.join(self.scratch, "mosquitto.log"), "w")
        self.broker = subprocess.Popen([exe, "-c", conf], stdout=self._broker_log, stderr=subprocess.STDOUT)
        self._wait_port(self.broker_port, "mosquitto")

        self.link = LaggyLinkThread(listen_port=0, target_port=self.broker_port,
                                    delay_up=self.delay_ms / 1000.0, delay_down=self.delay_ms / 1000.0,
                                    log=self.link_log).start()
        self.link_port = self.link.port

        channel = f"hakake-desk-{os.getpid()}"
        sim = make_sim(vehicle=self.vehicle, scenario=self.sim_scenario, seed=self.seed)
        self.ecu = SimCanEcu(sim, bus="car", channel=channel, bus_load=self.bus_load).start()

        bridge_port = self.link_port if self.leg == "bridge" else self.broker_port
        cfg = hb.load_config(None, {"host": "127.0.0.1", "port": bridge_port, "prefix": self.prefix, "bus": "car",
                                    "interface": "virtual", "channel": channel, "ids": "all", "stats_s": 0})
        bus = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
        self._buses.append(bus)
        self.bridge = hb.Bridge(cfg, bus=bus)
        self.bridge.apply_filters()
        self.client = self.bridge.attach_client(self.bridge.make_client())
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.connect_async("127.0.0.1", bridge_port, keepalive=15)
        self.client.loop_start()
        self.bridge.start()
        deadline = time.time() + 10.0
        while not self.bridge.connected and time.time() < deadline:
            time.sleep(0.05)
        if not self.bridge.connected:
            raise SystemExit("the bridge did not reach the broker")
        threading.Thread(target=self._status, name="desk-status", daemon=True).start()
        return self

    def _status(self):
        period = float(self.bridge.cfg["status_s"])
        while not self._status_stop.wait(period):
            if self.bridge.connected:
                try:
                    self.bridge.publish_status()
                except Exception:
                    pass

    def _wait_port(self, port, what, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.broker is not None and self.broker.poll() is not None:
                raise SystemExit(f"{what} exited at once; see {os.path.join(self.scratch, 'mosquitto.log')}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                return
            except OSError:
                time.sleep(0.1)
        raise SystemExit(f"{what} did not open port {port}")

    def stop(self):
        self._status_stop.set()
        for step in (lambda: self.bridge and self.bridge.stop(),
                     lambda: self.client and self.client.loop_stop(),
                     lambda: self.client and self.client.disconnect(),
                     lambda: self.ecu and self.ecu.stop(),
                     lambda: [b.shutdown() for b in self._buses],
                     lambda: self.link and self.link.stop()):
            try:
                step()
            except Exception:
                pass
        if self.broker is not None and self.broker.poll() is None:
            self.broker.terminate()
            try:
                self.broker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.broker.kill()
        try:
            self._broker_log.close()
        except Exception:
            pass


class Dashboard:
    """The real application as a child process, in its own process group so
    the reader it starts is stopped with it."""

    def __init__(self, data_dir, port, prefix, vehicle, web_port):
        self.data_dir, self.web_port = data_dir, web_port
        self.cmd = [sys.executable, os.path.join(ROOT, "web", "app.py"), "--adapter", "mqtt",
                    "--vehicle", vehicle, "--port", str(web_port)]
        self.env = dashboard_env(data_dir, port, prefix)
        self.proc = None
        self.log_path = os.path.join(data_dir, "dashboard.log")

    def start(self):
        self._log = open(self.log_path, "w")
        self.proc = subprocess.Popen(self.cmd, cwd=ROOT, env=self.env, stdout=self._log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        return self

    def stop(self):
        if self.proc is None:
            return
        for sig, wait in ((signal.SIGINT, 8), (signal.SIGTERM, 4), (signal.SIGKILL, 2)):
            if self.proc.poll() is not None:
                break
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)       # the reader, if it outlived its parent
        except (ProcessLookupError, PermissionError):
            pass
        self._log.close()


class StateWatch:
    """Listens to the reader's own ``state`` topic, straight at the broker, and
    keeps every cycle that carries a value no car can produce. The reader
    publishes one record per cycle there, so this sees exactly what a gauge or
    the page would have drawn, including a value that lasts a single cycle
    (the state file and the 5 s store are both too coarse to promise that)."""

    def __init__(self, port, prefix):
        import paho.mqtt.client as mqtt
        self.port, self.topic = port, f"{prefix}/state"
        self.samples = 0
        self.good = 0
        self.bad = []                       # (wall time, {key: value})
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"hakake-desk-watch-{os.getpid()}")
        self.client.on_connect = lambda c, u, flags, rc, props=None: c.subscribe(self.topic, qos=0)
        self.client.on_message = self._message

    def start(self):
        self.client.connect_async("127.0.0.1", self.port, keepalive=30)
        self.client.loop_start()
        return self

    def _message(self, client, userdata, msg):
        if msg.retain:                       # a kept copy of an earlier cycle, not a new one
            return
        try:
            rec = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(rec, dict):
            return
        self.samples += 1
        wrong = implausible(rec)
        if wrong:
            self.bad.append((time.time(), wrong))
        elif isinstance(rec.get("soc"), (int, float)):
            self.good += 1

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


# ── the scenario ─────────────────────────────────────────────────────────

def run_stale_reply(args, mod, scratch, data_dir):
    plan = scenario_plan(mod, args.hold_item)
    hold_s = args.hold_for or plan["hold_s"]
    enabled = set(plan["tiles"]) | set(args.enable)
    with open(os.path.join(data_dir, "tiles.json"), "w") as f:
        json.dump(tiles_config(mod, enabled, set(args.celllog)), f)
    say(f"scenario stale-reply: hold the {plan['hold']} request {hold_s:.1f} s on the {args.leg}'s leg; "
        f"every cycle asks {plan['fast']}; tiles on: {', '.join(sorted(enabled))}")

    chain = Chain(scratch, args.prefix, args.vehicle, args.leg, args.sim_scenario, args.seed,
                  args.bus_load, args.delay_ms)
    dash = watch = None
    fired_at = None
    try:
        chain.start()
        say(f"chain up: broker :{chain.broker_port}, link :{chain.link_port} ({args.leg}'s leg), "
            f"topics under {chain.prefix}/")
        watch = StateWatch(chain.broker_port, chain.prefix).start()
        dash = Dashboard(data_dir, chain.reader_port, chain.prefix, args.vehicle, args.web_port or free_port()).start()
        deadline = time.time() + args.warmup_timeout
        while watch.good < args.warmup and time.time() < deadline:
            if dash.proc.poll() is not None:
                raise SystemExit(f"the dashboard exited during warm-up; see {dash.log_path}")
            time.sleep(0.2)
        if watch.good < args.warmup:
            raise SystemExit(f"no steady readings after {args.warmup_timeout:.0f} s; see {dash.log_path}")
        say(f"reader is steady ({watch.good} good samples, 0 bad so far)" if not watch.bad
            else f"reader is up, with {len(watch.bad)} bad sample(s) before any fault was injected")
        before = len(watch.bad)

        direction = "up" if args.leg == "reader" else "down"   # the way a request travels through the link
        chain.link.hold_on(plan["pattern"], hold_s, direction)
        deadline = time.time() + 30.0
        while chain.link.stats["holds_fired"] < 1 and time.time() < deadline:
            time.sleep(0.05)
        if chain.link.stats["holds_fired"] < 1:
            raise SystemExit(f"the {plan['hold']} request never passed the link; see {dash.log_path}")
        fired_at = time.time()
        say(f"holding the {plan['hold']} request; the reader will give up on it")
        time.sleep(hold_s + args.settle)
    finally:
        if watch is not None:
            watch.stop()
        if dash is not None:
            dash.stop()
        chain.stop()

    rows = stored_rows(os.path.join(data_dir, DB_FILE))
    bad_rows = [(r, implausible(r)) for r in rows if implausible(r)]
    drawn = watch.bad[before:]
    say(f"cycles published by the reader: {watch.samples} ({watch.good} good, {len(watch.bad)} bad); "
        f"stored rows: {len(rows)} ({len(bad_rows)} bad)")
    for t, wrong in drawn[:5]:
        say(f"  on the page {t - fired_at:+.1f} s after the hold began: {wrong}")
    for r, wrong in bad_rows[:5]:
        say(f"  stored row {r['id']} {r['ts_epoch'] - fired_at:+.1f} s after the hold began: {wrong}")
    fault = bool(drawn or bad_rows)
    verdict = "fault" if fault else "clean"
    say("FAULT REPRODUCED: a reply was decoded as the answer to a request it did not belong to" if fault
        else "CLEAN: no impossible value reached the page or the database")
    if before:
        say(f"note: {before} bad sample(s) appeared before the hold; they are not counted above")
    say(f"left for inspection: {scratch}")
    if args.expect and args.expect != verdict:
        say(f"expected {args.expect}, got {verdict}")
        return 1
    return 0


def run_serve(args, mod, scratch, data_dir):
    from laggy_link import run_command
    chain = Chain(scratch, args.prefix, args.vehicle, args.leg, args.sim_scenario, args.seed,
                  args.bus_load, args.delay_ms, link_log=lambda text: say(f"link: {text}"))
    try:
        chain.start()
        env = dashboard_env(data_dir, chain.reader_port, chain.prefix, base={})
        say(f"chain up: broker :{chain.broker_port}, link :{chain.link_port} ({args.leg}'s leg), "
            f"topics under {chain.prefix}/")
        print("\nStart the dashboard against it, from the repository, in your usual venv:\n\n  " +
              " \\\n  ".join(f"{k}={v}" for k, v in sorted(env.items())) +
              f" \\\n  ./web/app.py --adapter mqtt --vehicle {args.vehicle} --port {args.web_port or 5055}\n\n"
              "Then, here:  stall <s> [up|down|both] | delay <ms> [up|down|both] | hold <text> <s> | stats | quit\n",
              flush=True)
        for line in sys.stdin:
            keep = []
            chain.link._call(lambda: keep.append(run_command(chain.link.link, line, say)))
            if keep and keep[0] is False:
                break
    except KeyboardInterrupt:
        pass
    finally:
        chain.stop()
        say(f"stopped; data folder left at {data_dir}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--scenario", choices=["stale-reply"], default=None,
                    help="run a fault scenario and report (default: bring the chain up and wait)")
    ap.add_argument("--expect", choices=["fault", "clean"], default=None,
                    help="make the scenario a check: exit 1 unless this is the verdict")
    ap.add_argument("--vehicle", default="leaf_ze0")
    ap.add_argument("--leg", choices=["reader", "bridge"], default="reader",
                    help="whose connection goes through the laggy link (default: the reader's)")
    ap.add_argument("--hold-item", default=None, help="the request item to hold back (default: chosen from the profile)")
    ap.add_argument("--hold-for", type=float, default=None, help="seconds to hold it (default: its timeout + 2.5)")
    ap.add_argument("--enable", action="append", default=[], metavar="TILE", help="also enable this tile (repeatable)")
    ap.add_argument("--celllog", action="append", default=[], metavar="TILE", help="arm the cell log on this tile")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="steady delay on the link, each way")
    ap.add_argument("--settle", type=float, default=6.0, help="seconds to keep watching after the hold ends")
    ap.add_argument("--warmup", type=int, default=40, help="good cycles to see before injecting the fault")
    ap.add_argument("--warmup-timeout", type=float, default=60.0)
    ap.add_argument("--prefix", default=DESK_PREFIX)
    ap.add_argument("--sim-scenario", default=None, help="simulator scenario (default: the model's own)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--bus-load", type=float, default=0.0,
                    help="filler traffic on the simulated bus, 0 to 1 (default 0: decoded ids only)")
    ap.add_argument("--web-port", type=int, default=None)
    ap.add_argument("--scratch", default=None, help="folder for the run's files (default: a new temporary folder)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the bridge's own log lines")
    args = ap.parse_args(argv)

    import logging
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR,
                        format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    from vehicles import get_vehicle
    mod = get_vehicle(args.vehicle)
    scratch = os.path.abspath(args.scratch) if args.scratch else tempfile.mkdtemp(prefix="hakake-desk-")
    data_dir = os.path.join(scratch, "data")
    os.makedirs(data_dir, exist_ok=True)
    if args.scenario == "stale-reply":
        return run_stale_reply(args, mod, scratch, data_dir)
    return run_serve(args, mod, scratch, data_dir)


if __name__ == "__main__":
    sys.exit(main())
