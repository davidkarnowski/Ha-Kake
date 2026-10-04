#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
mqttsource — a car on the far end of an MQTT broker.

Two halves, both speaking the wire protocol in ``docs/MQTT.md`` (the public
spec; a third party can build a gauge from it without reading this file):

  MqttSource      the ingesting half. A ``FrameSource`` (plan §4 contract, shared
                  with ``cantransport.LocalSource``): subscribes to the CAN
                  frames a bridge mirrors on ``<prefix>/<bus>/rx/<ID>``, hands
                  each one to the façade as ``on_frame(t, id_hex, data, flags)``,
                  turns ``send_uds()`` into a ``tx/uds`` request and waits for
                  its ack, and reads the bridge's retained ``status`` topic for
                  ``liveness()``. ``open_mqtt()`` wraps one in
                  ``cantransport.CanFacade`` so the reader sees an ELM327.

  StatePublisher  the gauge-facing half. Publishes the reader's decoded record
                  to ``<prefix>/state`` (retained, once per cycle) and every
                  flat value to ``<prefix>/signal/<key>`` (retained, on change)
                  whenever ``mqtt`` is configured — with any adapter, so a
                  gauge on the LAN sees the numbers whether the car is read
                  over BLE, USB, a CANable or a remote bridge.

Read-only, on both ends: ``send_uds`` refuses — before publishing — any request
whose service byte is outside ``READ_SERVICES``; the bridge enforces the same
set on the bus side. There is no other way onto the wire from here.

No vehicle is named in this file. Ids are strings the profile hands over.
Machine specifics (the broker host) come from ``config.local.json``
(gitignored) or ``HAKAKE_MQTT_*``; none belong in code, fixtures or docs.
"""

import asyncio
import json
import os
import secrets
import threading
import time

from util import env

_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_CONFIG = os.path.join(_ROOT, "config.local.json")

PROTOCOL_VERSION = 1                 # "v" in status and state; docs/MQTT.md "Versioning"
READ_SERVICES = frozenset({0x21, 0x01, 0x03, 0x07})   # SECURITY.md — the whole allowed set
ACK_GRACE = 1.0                      # seconds added to a request's timeout for the round trip
FRAME_GRACE = 0.25                   # seconds to wait after an ok ack for its frames to land
CONNECT_TIMEOUT = 10.0               # seconds to wait for CONNACK in start()

DEFAULTS = {
    "host": "",
    "port": 1883,
    "prefix": "hakake/leaf",
    "bus": "car",
    "subscribe": "auto",
    "batch": False,
}


# ── configuration ────────────────────────────────────────────────────────

def _read_local_config(path=LOCAL_CONFIG):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def mqtt_config(raw=None, path=LOCAL_CONFIG):
    """The ``mqtt`` block, normalised, with ``HAKAKE_MQTT_*`` overriding the file.

    ``raw`` is the block itself (a dict) when given; otherwise it is read from
    ``config.local.json``. An empty ``host`` means "not configured".
    """
    if raw is None:
        raw = _read_local_config(path).get("mqtt") or {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    for key in ("host", "prefix", "bus"):
        v = env(f"HAKAKE_MQTT_{key.upper()}")
        if v:
            cfg[key] = v
    port = env("HAKAKE_MQTT_PORT")
    if port:
        cfg["port"] = int(port)
    cfg["host"] = str(cfg["host"] or "").strip()
    cfg["port"] = int(cfg["port"] or 1883)
    cfg["prefix"] = str(cfg["prefix"] or DEFAULTS["prefix"]).strip("/")
    cfg["bus"] = str(cfg["bus"] or DEFAULTS["bus"]).strip("/")
    cfg["batch"] = bool(cfg["batch"])
    sub = cfg["subscribe"]
    if isinstance(sub, str):
        cfg["subscribe"] = "auto" if sub.strip().lower() in ("", "auto") else [sub.strip()]
    elif isinstance(sub, (list, tuple)):
        cfg["subscribe"] = [str(s).strip() for s in sub if str(s).strip()]
    else:
        cfg["subscribe"] = "auto"
    return cfg


class Topics:
    """The topic tree of docs/MQTT.md for one ``<prefix>/<bus>``."""

    def __init__(self, prefix, bus):
        self.prefix = prefix.strip("/")
        self.bus = bus.strip("/")
        self.root = f"{self.prefix}/{self.bus}"
        self.status = f"{self.root}/status"
        self.batch = f"{self.root}/rx/_batch"
        self.uds = f"{self.root}/tx/uds"
        self.ack_all = f"{self.root}/tx/uds/+"
        self.rx_all = f"{self.root}/rx/#"
        self.state = f"{self.prefix}/state"

    def rx(self, id_hex):
        return f"{self.root}/rx/{id_hex}"

    def ack(self, req):
        return f"{self.root}/tx/uds/{req}"

    def signal(self, key):
        return f"{self.prefix}/signal/{key}"


# ── frames ───────────────────────────────────────────────────────────────

def norm_id(s):
    """ELM-style id: 3 upper-case hex digits for 11-bit, 8 for 29-bit."""
    n = int(str(s).strip(), 16)
    if n < 0:
        raise ValueError(f"negative CAN id {s!r}")
    return f"{n:03X}" if n <= 0x7FF else f"{n:08X}"


def parse_hex_bytes(d):
    """``"08 00 00"`` → ``b"\\x08\\x00\\x00"``. Whitespace-separated pairs only."""
    if isinstance(d, (bytes, bytearray)):
        return bytes(d)
    parts = str(d).split()
    out = bytearray()
    for p in parts:
        if len(p) != 2:
            raise ValueError(f"bad hex byte {p!r} in {d!r}")
        out.append(int(p, 16))
    return bytes(out)


def hex_bytes(data):
    return " ".join(f"{b:02X}" for b in bytes(data))


def frame_line(id_hex, data):
    """The ELM327 line (``ATH1``, ``ATS1``) this frame prints as: ``421 08 00 00``.

    DLC is preserved — a 3-byte frame is three bytes, never padded. This is
    the string ``leaf_decoders.parse_isotp`` and the replay fixtures consume.
    """
    body = hex_bytes(data)
    return f"{id_hex} {body}" if body else id_hex


def is_read_request(data):
    """True when the first byte is a service SECURITY.md allows."""
    data = bytes(data)
    return bool(data) and data[0] in READ_SERVICES


def parse_frame(obj, topic_id=None):
    """A ``rx/<ID>`` payload → ``(t, id_hex, data_bytes, flags)``.

    ``id`` may be omitted from the payload when the topic carries it. Unknown
    keys are ignored so a newer bridge stays readable by an older reader.
    """
    if not isinstance(obj, dict):
        raise ValueError("frame payload is not an object")
    raw_id = obj.get("id", topic_id)
    if raw_id is None:
        raise ValueError("frame has no id")
    id_hex = norm_id(raw_id)
    data = parse_hex_bytes(obj.get("d", ""))
    t = float(obj.get("t", 0.0))
    flags = {k: True for k in ("err", "ext") if obj.get(k) is True}
    if len(id_hex) == 8:
        flags["ext"] = True
    return t, id_hex, data, flags


def expand_batch(obj):
    """A ``rx/_batch`` payload → list of ``(t, id_hex, data_bytes, flags)``."""
    if not isinstance(obj, dict) or not isinstance(obj.get("frames"), list):
        raise ValueError("batch payload needs a 'frames' list")
    t0 = float(obj.get("t0", 0.0))
    out = []
    for fr in obj["frames"]:
        if not isinstance(fr, (list, tuple)) or len(fr) < 3:
            raise ValueError(f"bad batch entry {fr!r}")
        dt_ms, raw_id, d = fr[0], fr[1], fr[2]
        flags = {}
        if len(fr) > 3 and isinstance(fr[3], dict):
            flags = {k: True for k in ("err", "ext") if fr[3].get(k) is True}
        id_hex = norm_id(raw_id)
        if len(id_hex) == 8:
            flags["ext"] = True
        out.append((t0 + float(dt_ms) / 1000.0, id_hex, parse_hex_bytes(d), flags))
    return out


# ── paho ─────────────────────────────────────────────────────────────────

def paho_client(client_id):
    """A paho-mqtt 2.x client. Imported here so nothing else needs the package."""
    import paho.mqtt.client as mqtt          # lazy: tests use a fake
    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


def _configure_client(client, cfg):
    """Plain MQTT, no credentials, no TLS — by the owner's decision (2026-09-09).
    The broker lives on the car's LAN; beyond it, reach it over Tailscale or a
    private VPN, which authenticates and encrypts the whole path (docs/MQTT.md §5)."""
    return client


def _loads(payload):
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    return json.loads(payload)


# ── the frame source ─────────────────────────────────────────────────────

class MqttSource:
    """A ``FrameSource`` (plan §4) fed by a bridge over MQTT.

    Only moves frames: the façade owns the ELM state and the tables. paho's
    callbacks run on its network thread; everything handed to the façade is
    marshalled onto the asyncio loop ``start()`` was called from.
    """

    listen_only = False
    ts_source = "bridge"                   # frames' `t` is the Pi's clock (docs/TIMING.md)

    def __init__(self, cfg=None, client_factory=None, log=print):
        self.cfg = mqtt_config(cfg) if cfg is not None else mqtt_config()
        self.host = self.cfg["host"]
        self.tcp_port = self.cfg["port"]
        self.bus = self.cfg["bus"]
        # EV-CAN is listen-only on every path; the bridge's status can turn it on, never off
        self.listen_only = self.bus == "ev"
        self.topics = Topics(self.cfg["prefix"], self.bus)
        self.log = log
        self._factory = client_factory or paho_client
        self.client = None
        self.connected = False
        self.status = {}
        self.bridge = ""                       # "hakake-bridge 0.1", from status
        self.frames = 0
        self.t_last = None                     # bridge clock of the last frame
        self._t_last_local = None              # monotonic, for age
        self._on_frame = None
        self._on_status = None
        self._loop = None
        self._loop_thread = None
        self._connected_evt = None
        self._want = None                      # rx topics we want; None = not asked yet
        self._subscribed = set()               # rx topics the client holds
        self._suback = None                    # asyncio.Event: a SUBACK arrived
        self._pending = {}                     # req → Future
        self._stopped = set()                  # futures stop() cancelled (vs. the caller being cancelled)
        self._rx_counts = {}                   # id_hex → frames seen
        self._lock = threading.Lock()
        # Optional console event tap (docs/CONSOLE.md), set by the reader while
        # the raw output tile is enabled: tap(kind, text, id="", t=None). It
        # carries what the façade never sees — a bridge message this source had
        # to throw away. Frames are NOT tapped here: they reach the console
        # through CanFacade's own tap, and tapping both would print each twice.
        self.event_tap = None

    # ── identity ─────────────────────────────────────────────────────────

    @property
    def name(self):
        """Goes into adapter_name; always contains a digit (probe_alive)."""
        bridge = self.bridge or f"mqtt bridge (protocol v{PROTOCOL_VERSION})"
        return f"{bridge} @ {self.host}"

    @property
    def port(self):
        """Goes into adapter_port (the façade reads ``source.port``): a URL, not a number."""
        return f"mqtt://{self.host}:{self.tcp_port}/{self.topics.root}"

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self, on_frame, on_status=None, connect_timeout=CONNECT_TIMEOUT):
        if not self.host:
            raise ConnectionError("MQTT is not configured: set mqtt.host in config.local.json "
                                  "(see docs/MQTT.md)")
        self._on_frame = on_frame
        self._on_status = on_status
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._connected_evt = asyncio.Event()
        if self._want is None:                 # nobody narrowed it yet: mirror the bus
            self._want = self._wanted_topics(None)
        c = self._factory(f"hakake-reader-{secrets.token_hex(3)}")
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.on_subscribe = self._on_subscribe
        _configure_client(c, self.cfg)
        self.client = c
        c.connect_async(self.host, self.tcp_port, keepalive=30)
        c.loop_start()
        try:
            await asyncio.wait_for(self._connected_evt.wait(), connect_timeout)
        except asyncio.TimeoutError:
            await self.stop()
            raise ConnectionError(f"MQTT broker at {self.host}:{self.tcp_port} did not answer "
                                  f"within {connect_timeout:.0f}s")
        self.log(f"  mqtt: connected to {self.host}:{self.tcp_port}, bus {self.bus!r} "
                 f"under {self.topics.root}/")

    async def stop(self):
        c, self.client = self.client, None
        self.connected = False
        for fut in list(self._pending.values()):
            if not fut.done():
                self._stopped.add(fut)
                fut.cancel()
        self._pending.clear()
        if c is not None:
            try:
                c.disconnect()
            except Exception:
                pass
            try:
                c.loop_stop()
            except Exception:
                pass

    # ── subscriptions ────────────────────────────────────────────────────

    def _wanted_topics(self, ids):
        sub = self.cfg["subscribe"]
        if isinstance(sub, list):                    # explicit list overrides auto
            return {s if "/" in s else self.topics.rx(norm_id(s)) for s in sub}
        if self.cfg["batch"]:
            return {self.topics.batch}
        if ids is None:
            return {self.topics.rx_all}
        return {self.topics.rx(norm_id(i)) for i in ids}

    async def subscribe(self, ids, suback_timeout=1.0):
        """``ids`` is the set of CAN ids the façade needs, or None for everything.

        Auto mode follows it and re-subscribes on change; a list in config
        pins the topics regardless. Safe to call before ``start()``. When a
        new topic is added while connected, waits (briefly) for the broker's
        SUBACK so a request sent right after sees its response frames.
        """
        self._want = self._wanted_topics(ids)
        if self.client is not None:
            self._suback = asyncio.Event()     # fresh: bound to the loop that waits on it
        if self._apply_subscriptions() and self._suback is not None:
            try:
                await asyncio.wait_for(self._suback.wait(), suback_timeout)
            except asyncio.TimeoutError:
                pass

    def _apply_subscriptions(self):
        """Returns True when a SUBSCRIBE was sent."""
        if self.client is None or not self.connected or self._want is None:
            return False
        add = sorted(self._want - self._subscribed)
        drop = sorted(self._subscribed - self._want)
        if add:
            self.client.subscribe([(t, 0) for t in add])
        if drop:
            self.client.unsubscribe(drop)
        self._subscribed = set(self._want)
        return bool(add)

    # ── requests ─────────────────────────────────────────────────────────

    async def send_uds(self, tx, rx, data, bs=0, stmin=0, timeout=2.0):
        """Publish one UDS request and wait for the bridge's ack.

        The response frames arrive on ``rx/<rx>`` like any other frame — the
        façade captures them during the request window. Refused here, before
        anything is published, when the service byte is not a read.
        """
        data = bytes(data)
        req = secrets.token_hex(2)
        tx_id, rx_id = norm_id(tx), norm_id(rx)
        if not is_read_request(data):
            self.log(f"  mqtt: REFUSED request {hex_bytes(data)!r} to {tx_id}: "
                     f"service not in the read-only set")
            return {"req": req, "ok": False, "error": "refused",
                    "reason": "service not in the read-only set"}
        if self.listen_only:
            return {"req": req, "ok": False, "error": "refused",
                    "reason": "bridge is listen-only"}
        if self.client is None or not self.connected:
            return {"req": req, "ok": False, "error": "offline"}
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[req] = fut
        with self._lock:
            baseline = self._rx_counts.get(rx_id, 0)
        envelope = {"req": req, "tx": tx_id, "rx": rx_id, "data": hex_bytes(data),
                    "bs": int(bs), "stmin": int(stmin), "timeout": float(timeout)}
        self.client.publish(self.topics.uds, json.dumps(envelope, separators=(",", ":")), qos=1)
        try:
            ack = await asyncio.wait_for(fut, float(timeout) + ACK_GRACE)
        except asyncio.TimeoutError:
            ack = {"req": req, "ok": False, "error": "timeout"}
        except asyncio.CancelledError:
            if fut not in self._stopped:
                raise                          # the caller is being cancelled: let it stop
            ack = {"req": req, "ok": False, "error": "offline"}
        finally:
            self._pending.pop(req, None)
            self._stopped.discard(fut)
        want = ack.get("frames") if ack.get("ok") else None
        if isinstance(want, int) and want > 0:
            # QoS 0 frames and a QoS 1 ack are not strictly ordered; give the
            # last response frames a moment to land before the caller's
            # capture window closes.
            deadline = loop.time() + FRAME_GRACE
            while loop.time() < deadline:
                with self._lock:
                    seen = self._rx_counts.get(rx_id, 0) - baseline
                if seen >= want:
                    break
                await asyncio.sleep(0.01)
        return ack

    # ── liveness ─────────────────────────────────────────────────────────

    def liveness(self):
        age = None
        if self._t_last_local is not None:
            age = round(time.monotonic() - self._t_last_local, 3)
        bridge_online = self.status.get("online")
        online = bool(self.connected) and bridge_online is not False
        return {"online": online, "connected": bool(self.connected),
                "bridge_online": bridge_online, "fps": self.status.get("fps"),
                "t_last": self.t_last, "age_s": age, "frames": self.frames,
                "listen_only": self.listen_only, "bus": self.bus,
                "status": dict(self.status)}

    # ── paho callbacks (network thread) ──────────────────────────────────

    def _dispatch(self, fn, *args):
        """Run ``fn`` on the loop thread — directly when we are already on it
        (or the loop is idle, as in tests), else via call_soon_threadsafe."""
        if fn is None:
            return
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread or not loop.is_running():
            fn(*args)
        else:
            loop.call_soon_threadsafe(fn, *args)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code != 0)
        if failed:
            self.log(f"  mqtt: connection refused by {self.host}: {reason_code}")
            return
        self.connected = True
        self._subscribed = set()                 # a fresh session holds nothing
        client.subscribe([(self.topics.status, 1), (self.topics.ack_all, 1)])
        self._apply_subscriptions()
        self._dispatch(self._connected_evt.set)

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None):
        self.connected = False

    def _on_subscribe(self, client, userdata, mid, reason_codes=None, properties=None):
        if self._suback is not None:
            self._dispatch(self._suback.set)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        root = self.topics.root + "/"
        if not topic.startswith(root):
            return
        tail = topic[len(root):]
        try:
            if tail == "status":
                self._take_status(_loads(msg.payload))
            elif tail.startswith("tx/uds/"):
                self._take_ack(tail[len("tx/uds/"):], _loads(msg.payload))
            elif tail == "rx/_batch":
                for fr in expand_batch(_loads(msg.payload)):
                    self._deliver(*fr)
            elif tail.startswith("rx/"):
                self._deliver(*parse_frame(_loads(msg.payload), topic_id=tail[3:]))
        except Exception as e:             # paho's thread must survive any payload (deep nesting too)
            self.log(f"  mqtt: dropped message on {topic}: {type(e).__name__}: {str(e)[:120]}")
            self._tap_event(f"mqtt: dropped message on {topic}: {type(e).__name__}")

    def _tap_event(self, text):
        tap = self.event_tap
        if tap is None:
            return
        try:
            tap("event", text, "")
        except Exception:                  # a console bug is not a transport fault
            self.event_tap = None

    def _take_status(self, obj):
        if not isinstance(obj, dict):
            raise ValueError("status is not an object")
        self.status = obj
        if obj.get("bridge"):
            self.bridge = str(obj["bridge"])
        if "listen_only" in obj:
            self.listen_only = bool(obj["listen_only"]) or self.bus == "ev"
        self._dispatch(self._on_status, dict(obj))

    def _take_ack(self, req, obj):
        if not isinstance(obj, dict):
            raise ValueError("ack is not an object")
        fut = self._pending.get(req)
        if fut is None:
            return

        def resolve():
            if not fut.done():
                fut.set_result(obj)
        self._dispatch(resolve)

    def _deliver(self, t, id_hex, data, flags):
        with self._lock:
            self._rx_counts[id_hex] = self._rx_counts.get(id_hex, 0) + 1
            self.frames += 1
            self.t_last = t
            self._t_last_local = time.monotonic()
        self._dispatch(self._on_frame, t, id_hex, data, flags)


# ── the adapter the reader sees ──────────────────────────────────────────

def _mqtt_facade_class(base):
    class MqttFacade(base):
        """``cantransport.CanFacade`` over an ``MqttSource``: a remote car.

        The façade never says which ids it wants, but the profile does — every
        passive item and every UDS target goes through ``ATCRA <id>`` first.
        In auto mode that command narrows the subscription from ``rx/#`` to the
        ids actually in use, growing as tiles are enabled (the tile-driven
        scheduler's rule, applied to the subscription).
        """

        adapter_type = "mqtt"

        def __init__(self, source, **kw):
            super().__init__(source, **kw)
            self._mqtt_source = source
            self._mqtt_ids = set()

        async def send(self, cmd, wait=0.0, timeout=8.0):
            up = (cmd or "").strip().upper()
            if up.startswith("ATCRA") and len(up) > 5:
                try:
                    cid = norm_id(up[5:].strip())
                except ValueError:
                    cid = None
                if cid and cid not in self._mqtt_ids:
                    self._mqtt_ids.add(cid)
                    await self._mqtt_source.subscribe(set(self._mqtt_ids))
            return await super().send(cmd, wait=wait, timeout=timeout)

        def marker(self):
            base_marker = super().marker() if hasattr(super(), "marker") else {}
            return dict(base_marker, remote={"host": self._mqtt_source.host,
                                             "bus": self._mqtt_source.bus})

    return MqttFacade


async def open_mqtt(log=print, cfg=None, client_factory=None):
    """What ``elm327.detect_adapter(prefer="mqtt")`` calls. Never auto-detected:
    a wrong broker is a wrong car, so MQTT must be asked for by name."""
    cfg = mqtt_config(cfg) if cfg is not None else mqtt_config()
    if not cfg["host"]:
        raise ConnectionError("MQTT is not configured: set mqtt.host in config.local.json "
                              "(see docs/MQTT.md)")
    try:
        import cantransport                      # lazy: the façade is its own module
    except ImportError as e:
        raise ConnectionError(f"the MQTT adapter needs cantransport.CanFacade: {e}")
    source = MqttSource(cfg, client_factory=client_factory, log=log)
    elm = _mqtt_facade_class(cantransport.CanFacade)(source, log=log)
    await elm.connect(log=log)
    elm.adapter_name = source.name
    elm.adapter_port = source.port
    log(f"  REMOTE CAR over MQTT: {elm.adapter_name} ({elm.adapter_port})")
    return elm


# ── the gauge-facing half ────────────────────────────────────────────────

def flatten_signals(record):
    """The ``signal/<key>`` topics a record yields, as ``(suffix, value)`` pairs.

    Numbers, booleans and strings publish as themselves. A list whose members
    are all numbers/booleans publishes one topic per index
    (``cells/0`` … ``cells/95``). ``None``, objects and any other list are
    skipped — they live in ``state``.
    """
    out = []
    for key, value in record.items():
        if not isinstance(key, str) or not key or "/" in key or key.startswith("_"):
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out.append((key, value))
        elif isinstance(value, str):
            out.append((key, value))
        elif isinstance(value, list) and value and all(isinstance(v, (bool, int, float)) for v in value):
            for i, v in enumerate(value):
                out.append((f"{key}/{i}", v))
    return out


class StatePublisher:
    """Publishes the decoded record for gauges: ``state`` every call, ``signal/<key>``
    on change. Both retained, QoS 0 — the newest value is what matters, and a
    subscriber that connects late gets it at once."""

    def __init__(self, cfg=None, client_factory=None, log=print):
        self.cfg = mqtt_config(cfg) if cfg is not None else mqtt_config()
        self.topics = Topics(self.cfg["prefix"], self.cfg["bus"])
        self.log = log
        self._factory = client_factory or paho_client
        self.client = None
        self._last = {}
        self.published = 0

    def connect(self):
        c = self._factory(f"hakake-state-{secrets.token_hex(3)}")
        _configure_client(c, self.cfg)
        c.connect_async(self.cfg["host"], self.cfg["port"], keepalive=30)
        c.loop_start()
        self.client = c
        return self

    def close(self):
        c, self.client = self.client, None
        if c is not None:
            try:
                c.disconnect()
                c.loop_stop()
            except Exception:
                pass

    def _pub(self, topic, payload):
        info = self.client.publish(topic, payload, qos=0, retain=True)
        rc = getattr(info, "rc", 0)
        if rc == 0:
            self.published += 1
        return rc == 0

    def publish_state(self, record):
        """Returns the number of messages published this call."""
        if self.client is None:
            return 0
        before = self.published
        rec = dict(record)
        rec["v"] = PROTOCOL_VERSION
        self._pub(self.topics.state, json.dumps(rec, separators=(",", ":"), default=str))
        for suffix, value in flatten_signals(record):
            if suffix in self._last and self._last[suffix] == value:
                continue
            if self._pub(self.topics.signal(suffix), json.dumps(value, default=str)):
                self._last[suffix] = value
        return self.published - before


_state_publisher = None
_state_disabled = False


def publish_state(record, log=print):
    """The reader's one call site: publish the record if ``mqtt`` is configured,
    else do nothing. Never raises — a broker outage must not stop the reader."""
    global _state_publisher, _state_disabled
    if _state_disabled:
        return 0
    if _state_publisher is None:
        cfg = mqtt_config()
        if not cfg["host"]:
            _state_disabled = True
            return 0
        try:
            _state_publisher = StatePublisher(cfg, log=log).connect()
            log(f"  mqtt: publishing state to {_state_publisher.topics.state} "
                f"and {_state_publisher.topics.signal('<key>')} on {cfg['host']}")
        except Exception as e:
            log(f"  mqtt: state publishing disabled: {e}")
            _state_disabled = True
            return 0
    try:
        return _state_publisher.publish_state(record)
    except Exception as e:
        log(f"  mqtt: state publish failed: {e}")
        return 0


def reset_state_publisher():
    """Forget the module-level publisher (tests, or a config reload)."""
    global _state_publisher, _state_disabled
    if _state_publisher is not None:
        _state_publisher.close()
    _state_publisher = None
    _state_disabled = False
