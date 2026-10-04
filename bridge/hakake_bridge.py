#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
hakake-bridge — the process on the car's CAN bus, on behalf of a reader elsewhere.

Runs on a Raspberry Pi (Zero 2 W or 4) plugged into the OBD port, and speaks
the wire protocol in ``docs/MQTT.md`` (the copy of that file next to this one
on the Pi is the authority; keep them the same):

    <prefix>/<bus>/rx/<ID>        one CAN frame per message   {"t","id","d"}
    <prefix>/<bus>/rx/_batch      a window of frames          {"t0","frames":[[dt_ms,id,d],…]}
    <prefix>/<bus>/tx/uds         a UDS request from a reader (we consume)
    <prefix>/<bus>/tx/uds/<req>   our ack
    <prefix>/<bus>/status         retained; LWT sets online:false

Read-only, enforced HERE — this is the process that can put frames on the
wire, so it does not trust the reader: a ``tx/uds`` request whose service byte
is not in ``READ_SERVICES`` ({0x21, 0x01, 0x03, 0x07}) is refused and logged;
mode 0x04 (clear DTCs) never leaves; ``--listen-only`` refuses every request.
No other transmit path exists in this file.

Built for a Pi Zero 2 W (four slow cores, 512 MB, WiFi):

  * SocketCAN is the input. A CANable on candleLight firmware (``gs_usb``
    kernel driver) and an MCP2515 SPI HAT both appear as ``can0``; the kernel
    does the framing and, with an ``ids`` list, the filtering
    (``bus.set_filters`` → ``CAN_RAW_FILTER``), so unwanted frames never reach
    Python. ``slcan`` works as a fallback and is documented as slow: python-can
    parses it one byte per syscall.
  * Serialisation is a hand-built string of the fixed schema with a 256-entry
    hex table — no ``json.dumps`` per frame (see ``frame_payload``).
  * A bounded publish queue: when WiFi or the broker stalls, the oldest frames
    are dropped and counted (``status.dropped``); memory never grows.
  * ``batch_ms`` bundles a whole bus into 50 ms windows when mirroring
    everything — the recommended setting on a Zero.

Standalone on purpose: depends on python-can, can-isotp and paho-mqtt only,
nothing from the Ha-Kake tree, so it can be copied to the Pi alone. No
vehicle is named here: ids come from the config file.
"""

import argparse
import json
import logging
import math
import os
import queue
import signal
import sys
import threading
import time

VERSION = "0.1"
BRIDGE = f"hakake-bridge {VERSION}"
PROTOCOL_VERSION = 1
READ_SERVICES = frozenset({0x21, 0x01, 0x03, 0x07})
HEX = [f"{i:02X}" for i in range(256)]
MAX_UDS_TIMEOUT = 10.0
MAX_PAYLOAD = 64 * 1024        # a tx/uds request is a few hundred bytes; anything larger is dropped
UDS_QUEUE = 32                 # requests waiting for the bus; beyond this a request is acked "busy"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 1883,
    "prefix": "hakake/leaf",
    "bus": "car",                # which bus the wiring reaches; never guessed
    "interface": "socketcan",    # python-can interface: socketcan | slcan | gs_usb | virtual
    "channel": "can0",
    "bitrate": 500000,
    "listen_only": False,
    "ids": "all",                # "all" or a list of hex ids; applied as a kernel filter
    "batch_ms": 0,               # 0 = one message per frame; 50 = 50 ms bundles
    "status_s": 2.0,
    "stats_s": 10.0,             # console line period; 0 = off
    "queue": 2000,               # publish queue depth (frames or batches)
    "uds_timeout": 5.0,          # cap on a request's own timeout
    # The only request/response id pairs this bridge will put a request on: a
    # request naming any other id is refused before a frame is built. The
    # default is the Leaf profile's TARGETS (battery controller, HVAC amp).
    "uds_targets": ["79B:7BB", "744:764"],
}

log = logging.getLogger("hakake-bridge")


# ── config ───────────────────────────────────────────────────────────────

def load_config(path=None, overrides=None):
    cfg = dict(DEFAULTS)
    if path:
        with open(path) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    for k, v in (overrides or {}).items():
        if v is not None and k in DEFAULTS:
            cfg[k] = v
    cfg["prefix"] = str(cfg["prefix"]).strip("/")
    cfg["bus"] = str(cfg["bus"]).strip("/")
    cfg["port"] = int(cfg["port"])
    cfg["bitrate"] = int(cfg["bitrate"])
    cfg["batch_ms"] = max(0, int(cfg["batch_ms"] or 0))
    cfg["queue"] = max(10, int(cfg["queue"]))
    # EV-CAN is opened listen-only whatever the config says (as cantransport.py does)
    cfg["listen_only"] = bool(cfg["listen_only"]) or cfg["bus"] == "ev"
    cfg["uds_targets"] = parse_targets(cfg["uds_targets"])
    ids = cfg["ids"]
    if isinstance(ids, str):
        cfg["ids"] = "all" if ids.strip().lower() in ("", "all", "*") else [ids]
    if isinstance(cfg["ids"], list):
        cfg["ids"] = sorted({int(str(i), 16) for i in cfg["ids"]})
        if not cfg["ids"]:
            cfg["ids"] = "all"
    return cfg


def parse_targets(items):
    """["79B:7BB", ...] -> frozenset {(0x79B, 0x7BB), ...}. A pair names the id a
    request is sent on and the id its answer comes back on; both must be valid
    11- or 29-bit ids and differ."""
    if isinstance(items, str):
        items = [items]
    out = set()
    for it in items or []:
        tx_s, sep, rx_s = str(it).partition(":")
        if not sep:
            raise ValueError(f"uds_targets entry {it!r} is not TX:RX")
        tx, rx = int(tx_s, 16), int(rx_s, 16)
        if not (0 <= tx <= 0x1FFFFFFF and 0 <= rx <= 0x1FFFFFFF) or tx == rx:
            raise ValueError(f"uds_targets entry {it!r} is not a usable id pair")
        out.add((tx, rx))
    return frozenset(out)


# ── frames ───────────────────────────────────────────────────────────────

def id_hex(arbitration_id, extended=False):
    """ELM style: 3 upper-case hex digits for 11-bit ids, 8 for 29-bit."""
    return f"{arbitration_id:08X}" if extended or arbitration_id > 0x7FF else f"{arbitration_id:03X}"


def fmt_data(data):
    return " ".join([HEX[b] for b in data])


def frame_payload(t, id_s, d, err=False, ext=False):
    """The rx/<ID> message: a hand-built string of the fixed schema (docs/MQTT.md
    "frame"), which is what json.dumps would produce for the same object."""
    flags = ""
    if err:
        flags += ',"err":true'
    if ext:
        flags += ',"ext":true'
    return f'{{"t":{t:.6f},"id":"{id_s}","d":"{d}"{flags}}}'


def batch_payload(t0, frames):
    """frames: list of (dt_ms:int, id_s, d, flags_str)."""
    body = ",".join([f'[{dt},"{i}","{d}"{fl}]' for dt, i, d, fl in frames])
    return f'{{"t0":{t0:.6f},"frames":[{body}]}}'


def is_read_request(data):
    return bool(data) and data[0] in READ_SERVICES


def parse_hex(s):
    parts = str(s).split()
    if any(len(p) != 2 for p in parts):
        raise ValueError(f"bad hex bytes {s!r}")
    return bytes(int(p, 16) for p in parts)


# ── the bridge ───────────────────────────────────────────────────────────

class Stats:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.frames_in = 0
        self.frames_out = 0       # frames (not messages) handed to the client
        self.msgs_out = 0
        self.dropped = 0
        self.errors = 0           # CAN error frames + bus errors
        self.tx = 0               # frames we put on the bus (UDS only)
        self.refused = 0
        self._win_t = clock()
        self._win_in = 0
        self._win_out = 0
        self.fps_in = 0.0
        self.fps_out = 0.0

    def tick(self):
        """Roll the rate window; call once per status period."""
        now = self.clock()
        dt = now - self._win_t
        if dt >= 0.5:
            self.fps_in = round((self.frames_in - self._win_in) / dt, 1)
            self.fps_out = round((self.frames_out - self._win_out) / dt, 1)
            self._win_t, self._win_in, self._win_out = now, self.frames_in, self.frames_out


class _Listener:
    """The Notifier's listener: frames go to Bridge.on_can; a receive error (USB
    unplugged, interface down) marks the bridge offline instead of silently
    killing the Notifier thread while the status still says online."""

    def __init__(self, bridge):
        self.bridge = bridge

    def __call__(self, msg):
        self.bridge.on_can(msg)

    def on_error(self, exc):
        self.bridge.bus_error(exc)


def socketcan_is_listen_only(channel):
    """True when `ip -details link show <channel>` reports LISTEN-ONLY (the
    kernel enforces it). Copied from cantransport.py: the bridge is standalone."""
    import subprocess
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return False
    return "LISTEN-ONLY" in out.upper()


def _silent_slcan_class():
    """python-can's slcan with silent mode set the way the stock CANable 2
    firmware understands it: M1 before O (python-can sends L, which that firmware
    does not implement). Copied from cantransport.HakakeSlcanBus."""
    from can.interfaces.slcan import slcanBus

    class SilentSlcan(slcanBus):
        def open(self):
            if self._listen_only:
                self._write("M1")
            self._write("O")
    return SilentSlcan


def SilentSlcanBus(**kw):
    return _silent_slcan_class()(**kw)


class Bridge:
    """CAN ↔ MQTT. Construct with a python-can ``bus`` and a paho-like ``client``
    (tests inject a ``virtual`` bus and a fake client); ``run()`` builds both
    from the config for real."""

    def __init__(self, cfg, bus=None, client=None, clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self.bus = bus
        self.client = client
        self.listen_only = bool(cfg["listen_only"]) or cfg["bus"] == "ev"
        self.targets = cfg.get("uds_targets") or frozenset()
        self.online = True                        # False once the CAN side reports an error
        self.error = None
        self.root = f"{cfg['prefix']}/{cfg['bus']}"
        self.t_status = f"{self.root}/status"
        self.t_batch = f"{self.root}/rx/_batch"
        self.t_uds = f"{self.root}/tx/uds"
        self._topic = {}                          # id_s → rx topic, precomputed
        self.stats = Stats(clock)
        self.q = queue.Queue(maxsize=cfg["queue"])
        self.batch_ms = cfg["batch_ms"]
        self._batch = []
        self._batch_t0 = None
        self._batch_lock = threading.Lock()
        self._filters = list(cfg["ids"]) if isinstance(cfg["ids"], list) else None
        self._uds_lock = threading.Lock()         # one request on the bus at a time
        self._uds_rx = None                       # arbitration id we are collecting
        self._uds_q = queue.Queue()
        self._uds_frames = 0
        self._stop = threading.Event()
        self._notifier = None
        self._pub_thread = None
        self._uds_thread = None
        self._uds_requests = queue.Queue(maxsize=UDS_QUEUE)   # (payload, monotonic time received)
        self.connected = False

    # ── CAN side ─────────────────────────────────────────────────────────

    def open_bus(self):
        import can
        iface, channel = self.cfg["interface"], self.cfg["channel"]
        kw = {"interface": iface, "channel": channel}
        if iface != "socketcan":
            kw["bitrate"] = self.cfg["bitrate"]
        if iface == "slcan":
            log.warning("slcan: python-can parses it one byte per syscall; prefer candleLight "
                        "firmware (gs_usb) or an SPI HAT so the kernel exposes can0")
        if self.listen_only:
            # The controller itself must be silent, not just this program.
            if iface == "socketcan":
                if not socketcan_is_listen_only(channel):
                    raise SystemExit(f"{channel} is not in listen-only mode; run: sudo ip link set "
                                     f"{channel} down && sudo ip link set {channel} up type can bitrate "
                                     f"{self.cfg['bitrate']} listen-only on restart-ms 100")
            elif iface == "slcan":
                self.bus = SilentSlcanBus(channel=channel, bitrate=self.cfg["bitrate"], listen_only=True,
                                          sleep_after_open=0.5)
                log.warning("slcan: asked for silent mode with M1 before O; the stock firmware does not "
                            "acknowledge it, so it cannot be verified from here — this bridge never "
                            "transmits while listen-only regardless")
                self.apply_filters()
                return self.bus
            elif iface != "virtual":
                raise SystemExit(f"listen-only on interface {iface!r} cannot be set or verified here; "
                                 "use socketcan (candleLight boards appear as can0 through the kernel "
                                 "gs_usb driver) or slcan")
        self.bus = can.Bus(**kw)
        self.apply_filters()
        return self.bus

    def bus_error(self, exc):
        """The CAN side failed: say so on the status topic and stop pretending."""
        self.stats.errors += 1
        self.online = False
        self.error = f"{type(exc).__name__}: {exc}"
        log.error("CAN receive failed (%s); mirroring stopped, status online: false", self.error)
        if self.client is not None:
            try:
                self.publish_status()
            except Exception:
                pass

    def apply_filters(self):
        """Kernel-level filtering when an ids list is configured: unwanted
        frames never reach Python. No-op for 'all'."""
        if self._filters is None or self.bus is None:
            return
        flt = [{"can_id": i, "can_mask": 0x7FF if i <= 0x7FF else 0x1FFFFFFF,
                "extended": i > 0x7FF} for i in self._filters]
        self.bus.set_filters(flt)

    def _ensure_rx_filter(self, rx_id):
        """A request's response id must pass the filter, or the answer never
        arrives; add it once and keep it."""
        if self._filters is not None and rx_id not in self._filters:
            self._filters.append(rx_id)
            log.info("filter: added response id %s", id_hex(rx_id))
            self.apply_filters()

    def on_can(self, msg):
        """python-can listener: every frame the kernel lets through."""
        t = msg.timestamp or self.clock()
        self.stats.frames_in += 1
        if msg.is_error_frame:
            self.stats.errors += 1
        ext = bool(msg.is_extended_id)
        id_s = id_hex(msg.arbitration_id, ext)
        d = fmt_data(msg.data)
        if self._uds_rx is not None and msg.arbitration_id == self._uds_rx and not msg.is_error_frame:
            self._uds_frames += 1
            self._uds_q.put(msg)                  # the ISO-TP layer's copy; the mirror still gets it
        if self.batch_ms:
            fl = ""
            if ext or msg.is_error_frame:
                fl = "," + json.dumps({k: True for k, v in (("err", msg.is_error_frame), ("ext", ext)) if v},
                                      separators=(",", ":"))
            with self._batch_lock:
                if self._batch_t0 is None:
                    self._batch_t0 = t
                self._batch.append((max(0, int(round((t - self._batch_t0) * 1000))), id_s, d, fl))
                if (t - self._batch_t0) * 1000 >= self.batch_ms:
                    self._flush_batch_locked()
        else:
            topic = self._topic.get(id_s)
            if topic is None:
                topic = self._topic[id_s] = f"{self.root}/rx/{id_s}"
            self._enqueue(topic, frame_payload(t, id_s, d, msg.is_error_frame, ext), 1)

    def _flush_batch_locked(self):
        if not self._batch:
            return
        payload = batch_payload(self._batch_t0, self._batch)
        n = len(self._batch)
        self._batch = []
        self._batch_t0 = None
        self._enqueue(self.t_batch, payload, n)

    def flush_batch(self, force=False):
        with self._batch_lock:
            if self._batch_t0 is not None and (force or (self.clock() - self._batch_t0) * 1000 >= self.batch_ms):
                self._flush_batch_locked()

    def _enqueue(self, topic, payload, frames):
        """Bounded: drop the oldest when full, count it, never block the CAN thread."""
        item = (topic, payload, frames)
        try:
            self.q.put_nowait(item)
        except queue.Full:
            try:
                _, _, lost = self.q.get_nowait()
                self.stats.dropped += lost
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(item)
            except queue.Full:
                self.stats.dropped += frames

    def drain(self, max_items=None):
        """Publish queued frames. The publisher thread calls this; tests call it directly."""
        n = 0
        while max_items is None or n < max_items:
            try:
                topic, payload, frames = self.q.get_nowait()
            except queue.Empty:
                break
            self.client.publish(topic, payload, qos=0, retain=False)
            self.stats.msgs_out += 1
            self.stats.frames_out += frames
            n += 1
        return n

    def _publisher(self):
        while not self._stop.is_set():
            if self.drain(500) == 0:
                time.sleep(0.005)
            if self.batch_ms:
                self.flush_batch()

    # ── MQTT side ────────────────────────────────────────────────────────

    def make_client(self):
        import paho.mqtt.client as mqtt
        # Plain MQTT: no credentials, no TLS (owner's decision, 2026-09-09). The
        # broker is LAN-only; beyond the LAN the path is Tailscale or a private VPN.
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"hakake-bridge-{os.getpid()}")

    def attach_client(self, client):
        self.client = client
        client.will_set(self.t_status, self.offline_payload(), qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code != 0)
        if failed:
            log.error("broker refused the connection: %s", reason_code)
            return
        self.connected = True
        client.subscribe(self.t_uds, qos=1)
        self.publish_status()
        log.info("connected to %s:%s as %s/", self.cfg["host"], self.cfg["port"], self.root)

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None):
        self.connected = False
        log.warning("disconnected from the broker (%s); paho reconnects", reason_code)

    def _on_message(self, client, userdata, msg):
        # paho runs this on its network thread: nothing here may raise, or the
        # thread dies and the bridge stops hearing the broker.
        try:
            if msg.topic != self.t_uds:
                return
            raw = msg.payload or b""
            if len(raw) > MAX_PAYLOAD:
                log.warning("tx/uds: %d-byte message dropped (limit %d)", len(raw), MAX_PAYLOAD)
                return
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:                    # not JSON, nested too deep, not bytes …
            log.warning("tx/uds: unreadable message ignored (%s)", type(e).__name__)
            return
        try:
            self._uds_requests.put_nowait((payload, time.monotonic()))  # the UDS thread runs it
        except queue.Full:
            self._ack(payload, {"ok": False, "error": "busy", "reason": "request queue full"})

    def _uds_worker(self):
        while not self._stop.is_set():
            try:
                payload, t_in = self._uds_requests.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                waited = time.monotonic() - t_in
                limit = float(payload.get("timeout", 2.0)) if isinstance(payload, dict) else 2.0
                if not math.isfinite(limit) or waited > limit:
                    self._ack(payload, {"ok": False, "error": "expired",
                                        "reason": f"waited {waited:.1f} s for the bus"})
                    continue
                self.handle_uds(payload)
            except Exception as e:                # one bad request never stops the worker
                self.stats.errors += 1
                log.warning("tx/uds: request failed (%s: %s)", type(e).__name__, e)
                self._ack(payload, {"ok": False, "error": "error", "reason": "bad request"})

    def _ack(self, payload, body):
        """Publish an ack for `payload` when it carries a usable req id."""
        req = str(payload.get("req", "")) if isinstance(payload, dict) else ""
        if not req or "/" in req or "+" in req or "#" in req or len(req) > 64:
            return None
        ack = dict(body, req=req)
        self.client.publish(f"{self.t_uds}/{req}", json.dumps(ack, separators=(",", ":")), qos=1, retain=False)
        return ack

    # ── status ───────────────────────────────────────────────────────────

    def status_payload(self):
        s = self.stats
        s.tick()
        adapter = f"{self.cfg['interface']} {self.cfg['channel']}"
        out = {"v": PROTOCOL_VERSION, "online": self.online, "bus": self.cfg["bus"],
                           "bitrate": self.cfg["bitrate"], "listen_only": self.listen_only,
                           "fps": s.fps_in, "fps_out": s.fps_out, "dropped": s.dropped,
                           "queue": self.q.qsize(), "errors": s.errors, "refused": s.refused,
                           "adapter": adapter, "bridge": BRIDGE,
                           "filter": "all" if self._filters is None else [id_hex(i) for i in self._filters],
                           "batch_ms": self.batch_ms, "t": round(self.clock(), 3)}
        if self.error:
            out["error"] = self.error
        return json.dumps(out, separators=(",", ":"))

    def offline_payload(self):
        return json.dumps({"v": PROTOCOL_VERSION, "online": False, "bus": self.cfg["bus"],
                           "bridge": BRIDGE}, separators=(",", ":"))

    def publish_status(self):
        self.client.publish(self.t_status, self.status_payload(), qos=1, retain=True)

    def stats_line(self):
        s = self.stats
        return (f"in {s.fps_in:.0f} fps, out {s.fps_out:.0f} fps, dropped {s.dropped}, "
                f"queue {self.q.qsize()}/{self.cfg['queue']}, errors {s.errors}, tx {s.tx}, refused {s.refused}")

    # ── UDS requests ─────────────────────────────────────────────────────

    def handle_uds(self, payload):
        """Run one request end to end and publish its ack. Returns the ack dict."""
        req = str(payload.get("req", "")) if isinstance(payload, dict) else ""
        if not req or "/" in req or "+" in req or "#" in req:
            log.warning("tx/uds: request without a usable req id, ignored")
            return None
        ack = self._run_uds(req, payload)
        self.flush_batch(force=True)              # response frames go out before the ack
        self.drain()
        self.client.publish(f"{self.t_uds}/{req}", json.dumps(ack, separators=(",", ":")), qos=1, retain=False)
        return ack

    def _refuse(self, req, why, data=""):
        self.stats.refused += 1
        log.warning("REFUSED tx/uds %s (%s): %s", req, data or "-", why)
        return {"req": req, "ok": False, "error": "refused", "reason": why}

    def _run_uds(self, req, p):
        try:
            tx = int(str(p["tx"]), 16)
            rx = int(str(p["rx"]), 16)
            data = parse_hex(p["data"])
            bs = int(p.get("bs", 0))
            stmin = int(p.get("stmin", 0))
            timeout = float(p.get("timeout", 2.0))
        except (KeyError, TypeError, ValueError) as e:
            return {"req": req, "ok": False, "error": "error", "reason": f"bad request: {e}"}
        if not (0 <= bs <= 0xFF and 0 <= stmin <= 0xFF) or not math.isfinite(timeout):
            return {"req": req, "ok": False, "error": "error", "reason": "bad request: bs/stmin 0-255, finite timeout"}
        if (tx, rx) not in self.targets:
            return self._refuse(req, f"{id_hex(tx, tx > 0x7FF)}/{id_hex(rx, rx > 0x7FF)} is not a configured "
                                     "uds_targets pair", fmt_data(data))
        if not is_read_request(data):
            return self._refuse(req, "service not in the read-only set", fmt_data(data))
        if self.listen_only:
            return self._refuse(req, "bridge is listen-only", fmt_data(data))
        if self.bus is None:
            return {"req": req, "ok": False, "error": "error", "reason": "no CAN bus"}
        timeout = min(max(0.1, timeout), min(MAX_UDS_TIMEOUT, float(self.cfg["uds_timeout"])))
        with self._uds_lock:
            return self._isotp_request(req, tx, rx, data, bs, stmin, timeout)

    def _isotp_request(self, req, tx, rx, data, bs, stmin, timeout):
        import isotp
        self._ensure_rx_filter(rx)
        ext = tx > 0x7FF or rx > 0x7FF
        mode = isotp.AddressingMode.Normal_29bits if ext else isotp.AddressingMode.Normal_11bits
        params = {
            "stmin": stmin,                       # what WE ask the ECU for (ELM: ATFCSD 30 <bs> <stmin>)
            "blocksize": bs,
            "tx_padding": 0x00,                   # pad to 8 bytes with 00, as the ELM327 (ATV0) and cantransport.py do
            "rx_flowcontrol_timeout": 1000,
            "rx_consecutive_frame_timeout": 1000,
            "wftmax": 0,
            "max_frame_size": 4095,
            "can_fd": False,
        }
        errors = []
        with self._uds_q.mutex:
            self._uds_q.queue.clear()
        self._uds_frames = 0
        self._uds_rx = rx

        def rxfn(t):
            try:
                m = self._uds_q.get(timeout=t)
            except queue.Empty:
                return None
            return isotp.CanMessage(arbitration_id=m.arbitration_id, dlc=m.dlc, data=bytes(m.data),
                                    extended_id=bool(m.is_extended_id))

        def txfn(m):
            import can
            self.bus.send(can.Message(arbitration_id=m.arbitration_id, data=bytes(m.data),
                                      is_extended_id=bool(m.is_extended_id), is_fd=False))
            self.stats.tx += 1

        layer = None
        try:
            address = isotp.Address(mode, txid=tx, rxid=rx)
            layer = isotp.TransportLayer(rxfn=rxfn, txfn=txfn, address=address, params=params,
                                         error_handler=errors.append, read_timeout=0.005)
            layer.send(data)
            deadline = self.clock() + timeout
            answer = None
            while self.clock() < deadline:
                layer.process()
                answer = layer.recv()
                if answer is not None:
                    break
                if errors:
                    break
                time.sleep(min(layer.sleep_time(), 0.005))
        except Exception as e:                    # bus-off, device gone
            self.stats.errors += 1
            return {"req": req, "ok": False, "error": "bus-off", "reason": str(e)}
        finally:
            self._uds_rx = None
            try:
                if layer is not None:
                    layer.reset()
            except Exception:
                pass
        if answer is not None:
            return {"req": req, "ok": True, "frames": self._uds_frames, "bytes": len(answer)}
        if errors:
            return {"req": req, "ok": False, "error": "error",
                    "reason": type(errors[0]).__name__, "frames": self._uds_frames}
        return {"req": req, "ok": False, "error": "timeout", "frames": self._uds_frames}

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Threads: python-can Notifier (CAN → queue), publisher (queue → broker),
        UDS worker. Call after open_bus()/attach_client()."""
        import can
        self._stop.clear()
        self._notifier = can.Notifier(self.bus, [_Listener(self)], timeout=0.1)
        self._pub_thread = threading.Thread(target=self._publisher, name="publisher", daemon=True)
        self._pub_thread.start()
        self._uds_thread = threading.Thread(target=self._uds_worker, name="uds", daemon=True)
        self._uds_thread.start()

    def stop(self):
        self._stop.set()
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None
        for t in (self._pub_thread, self._uds_thread):
            if t is not None:
                t.join(timeout=1.0)
        self.flush_batch(force=True)
        if self.client is not None:
            self.drain()
            try:
                self.client.publish(self.t_status, self.offline_payload(), qos=1, retain=True)
            except Exception:
                pass

    def run(self):
        """The real thing: open the bus, connect, serve until SIGTERM/SIGINT."""
        self.open_bus()
        log.info("CAN %s %s (%s, %s bit/s%s), filter %s, batch %s ms", self.cfg["interface"],
                 self.cfg["channel"], self.cfg["bus"], self.cfg["bitrate"],
                 ", LISTEN-ONLY" if self.listen_only else "",
                 "all" if self._filters is None else [id_hex(i) for i in self._filters], self.batch_ms)
        client = self.attach_client(self.make_client())
        client.connect_async(self.cfg["host"], self.cfg["port"], keepalive=30)
        client.loop_start()
        self.start()
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *a: stop.set())
        last_status = last_stats = 0.0
        try:
            while not stop.is_set():
                now = self.clock()
                if now - last_status >= float(self.cfg["status_s"]):
                    last_status = now
                    if self.connected:
                        self.publish_status()
                if self.cfg["stats_s"] and now - last_stats >= float(self.cfg["stats_s"]):
                    last_stats = now
                    self.stats.tick()
                    log.info("stats: %s", self.stats_line())
                stop.wait(0.1)
        finally:
            log.info("stopping")
            self.stop()
            client.loop_stop()
            client.disconnect()
            try:
                self.bus.shutdown()
            except Exception:
                pass


# ── CLI ──────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Ha-Kake CAN → MQTT bridge (read-only)")
    ap.add_argument("--config", default=None, help="JSON config (see config.example.json)")
    ap.add_argument("--host"), ap.add_argument("--port", type=int)
    ap.add_argument("--prefix"), ap.add_argument("--bus", choices=["car", "ev"])
    ap.add_argument("--interface"), ap.add_argument("--channel")
    ap.add_argument("--bitrate", type=int)
    ap.add_argument("--ids", help='comma-separated hex ids, or "all"')
    ap.add_argument("--batch-ms", type=int, dest="batch_ms")
    ap.add_argument("--listen-only", action="store_true", default=None, dest="listen_only",
                    help="never transmit: every tx/uds is refused (EV-CAN)")
    ap.add_argument("--stats", type=float, dest="stats_s", help="stats line period in s (0 = off)")
    ap.add_argument("--uds-target", action="append", dest="uds_targets", metavar="TX:RX",
                    help="a request/response id pair this bridge may send requests on (repeatable; "
                         "replaces the default 79B:7BB and 744:764)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    over = {k: v for k, v in vars(args).items() if k in DEFAULTS}
    if args.ids:
        over["ids"] = "all" if args.ids.strip().lower() == "all" else [s for s in args.ids.split(",") if s.strip()]
    cfg = load_config(args.config, over)
    log.info("%s — read-only: services %s only", BRIDGE, ", ".join(f"0x{s:02X}" for s in sorted(READ_SERVICES)))
    Bridge(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
