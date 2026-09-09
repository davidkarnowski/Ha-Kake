# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MqttSource / StatePublisher — a car on the far side of a broker.

No broker, no network, no hardware: a fake paho client delivers messages in
process. The guarantees under test are the ones docs/MQTT.md makes to a third
party — the payload shapes, the ELM line a frame reproduces byte for byte,
what a request publishes and when it resolves, that nothing but a read ever
leaves, and what a gauge finds on ``state`` and ``signal/<key>``.
"""

import asyncio
import json
import os
import sys
import types

import pytest

from conftest import ROOT, FIXTURES  # noqa: F401  (sys.path is set up there)

import mqttsource as ms  # noqa: E402
import record_session as rec  # noqa: E402
from leaf_decoders import parse_isotp  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ── a paho look-alike ────────────────────────────────────────────────────

class FakeMsg:
    def __init__(self, topic, payload, retain=False, qos=0):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.retain = retain
        self.qos = qos


class FakeInfo:
    rc = 0


class FakeClient:
    """The slice of paho-mqtt 2.x the module touches, recording everything."""

    def __init__(self, client_id="", auto_connect=True):
        self.client_id = client_id
        self.auto_connect = auto_connect
        self.on_connect = self.on_disconnect = self.on_message = self.on_subscribe = None
        self.subscriptions = []            # every subscribe() call, flattened
        self.unsubscribed = []
        self.published = []                # (topic, payload(str), qos, retain)
        self.will = None
        self.auth = None
        self.tls = False
        self.connected = False
        self.loop_running = False
        self.host = None

    # configuration
    def username_pw_set(self, username, password=None):
        self.auth = (username, password)

    def tls_set(self, *a, **k):
        self.tls = True

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    # lifecycle
    def connect_async(self, host, port=1883, keepalive=60):
        self.host = (host, port)

    def connect(self, host, port=1883, keepalive=60):
        self.host = (host, port)

    def loop_start(self):
        self.loop_running = True
        if self.auto_connect:
            self.simulate_connect()

    def loop_stop(self):
        self.loop_running = False

    def disconnect(self):
        self.connected = False
        if self.on_disconnect:
            self.on_disconnect(self, None, None, 0, None)

    # traffic
    def subscribe(self, topic, qos=0):
        if isinstance(topic, list):
            self.subscriptions.extend(topic)
        else:
            self.subscriptions.append((topic, qos))
        if self.on_subscribe:                      # the broker's SUBACK, at once
            self.on_subscribe(self, None, 1, [0], None)
        return (0, 1)

    def unsubscribe(self, topic):
        self.unsubscribed.extend(topic if isinstance(topic, list) else [topic])
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False):
        if isinstance(payload, bytes):
            payload = payload.decode()
        self.published.append((topic, payload, qos, retain))
        return FakeInfo()

    # test-side controls
    def simulate_connect(self):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, None, {}, 0, None)

    def deliver(self, topic, payload, retain=False):
        self.on_message(self, None, FakeMsg(topic, payload, retain=retain))


CFG = {"host": "broker.example", "port": 1883, "prefix": "hakake/test", "bus": "car"}
ROOT_T = "hakake/test/car"


class Sink:
    def __init__(self):
        self.frames = []
        self.status = []

    def on_frame(self, t, id_hex, data, flags):
        self.frames.append((t, id_hex, data, flags))

    def on_status(self, obj):
        self.status.append(obj)

    @property
    def lines(self):
        return [ms.frame_line(i, d) for _, i, d, _ in self.frames]


def make_source(cfg=None, **kw):
    client = FakeClient()
    src = ms.MqttSource(dict(CFG, **(cfg or {})), client_factory=lambda cid: client, **kw)
    return src, client


async def started(cfg=None, ids=None):
    src, client = make_source(cfg)
    sink = Sink()
    await src.start(sink.on_frame, sink.on_status, connect_timeout=1.0)
    if ids is not None:
        await src.subscribe(ids)
    return src, client, sink


# ── config ───────────────────────────────────────────────────────────────

def test_config_defaults_and_normalisation(monkeypatch):
    for k in ("HOST", "PORT", "PREFIX", "BUS", "USERNAME", "PASSWORD", "TLS"):
        monkeypatch.delenv(f"HAKAKE_MQTT_{k}", raising=False)
    cfg = ms.mqtt_config({})
    assert cfg["host"] == "" and cfg["port"] == 1883 and cfg["subscribe"] == "auto"
    assert cfg["prefix"] == "hakake/leaf" and cfg["bus"] == "car" and cfg["batch"] is False
    cfg = ms.mqtt_config({"host": " b ", "prefix": "/x/y/", "subscribe": ["421", "hakake/x/car/rx/358"],
                          "batch": 1, "port": "8883", "unknown": 1})
    assert cfg["host"] == "b" and cfg["prefix"] == "x/y" and cfg["port"] == 8883
    assert cfg["subscribe"] == ["421", "hakake/x/car/rx/358"] and cfg["batch"] is True
    assert "unknown" not in cfg


def test_env_overrides_file(monkeypatch):
    monkeypatch.setenv("HAKAKE_MQTT_HOST", "envhost")
    monkeypatch.setenv("HAKAKE_MQTT_PORT", "1884")
    monkeypatch.setenv("HAKAKE_MQTT_TLS", "yes")
    cfg = ms.mqtt_config({"host": "filehost", "port": 1883})
    assert cfg["host"] == "envhost" and cfg["port"] == 1884 and cfg["tls"] is True


def test_config_file_is_read_when_no_block_given(tmp_path, monkeypatch):
    monkeypatch.delenv("HAKAKE_MQTT_HOST", raising=False)
    p = tmp_path / "config.local.json"
    p.write_text(json.dumps({"mqtt": {"host": "h", "bus": "ev"}}))
    cfg = ms.mqtt_config(path=str(p))
    assert cfg["host"] == "h" and cfg["bus"] == "ev"
    assert ms.mqtt_config(path=str(tmp_path / "missing.json"))["host"] == ""


def test_example_config_carries_the_mqtt_block():
    with open(os.path.join(ROOT, "config.local.example.json")) as f:
        ex = json.load(f)
    assert set(ms.DEFAULTS) <= set(ex["mqtt"])
    assert ex["mqtt"]["host"] == "" and ex["mqtt"]["password"] == ""


def test_topics_tree():
    t = ms.Topics("hakake/leaf", "car")
    assert t.rx("421") == "hakake/leaf/car/rx/421"
    assert t.batch == "hakake/leaf/car/rx/_batch"
    assert t.uds == "hakake/leaf/car/tx/uds"
    assert t.ack("a1b2") == "hakake/leaf/car/tx/uds/a1b2"
    assert t.status == "hakake/leaf/car/status"
    assert t.state == "hakake/leaf/state"
    assert t.signal("soc") == "hakake/leaf/signal/soc"


# ── frames → ELM lines ───────────────────────────────────────────────────

def test_frame_payload_reproduces_the_elm_line_with_dlc_preserved():
    t, i, d, flags = ms.parse_frame({"t": 1.5, "id": "421", "d": "08 00 00"})
    assert (t, i, d, flags) == (1.5, "421", b"\x08\x00\x00", {})
    assert ms.frame_line(i, d) == "421 08 00 00"
    _, i, d, _ = ms.parse_frame({"t": 0, "id": "385", "d": "00 00 95 8F 92 92 F0"})
    assert ms.frame_line(i, d) == "385 00 00 95 8F 92 92 F0"


def test_id_normalisation_and_flags():
    assert ms.norm_id("21") == "021" and ms.norm_id("7bb") == "7BB"
    assert ms.norm_id("18DAF110") == "18DAF110"
    _, i, _, flags = ms.parse_frame({"t": 0, "id": "18DAF110", "d": "01"})
    assert i == "18DAF110" and flags == {"ext": True}
    _, _, _, flags = ms.parse_frame({"t": 0, "id": "001", "d": "", "err": True})
    assert flags == {"err": True}
    assert ms.parse_frame({"t": 0, "d": "AA"}, topic_id="1DB")[1] == "1DB"


def test_bad_payloads_raise():
    with pytest.raises(ValueError):
        ms.parse_frame({"t": 0, "id": "421", "d": "8 0"})
    with pytest.raises(ValueError):
        ms.parse_frame({"t": 0, "d": "08"})
    with pytest.raises(ValueError):
        ms.expand_batch({"t0": 0, "frames": [[0, "421"]]})
    with pytest.raises(ValueError):
        ms.parse_frame([1, 2, 3])


def test_batch_expands_to_absolute_times():
    out = ms.expand_batch({"t0": 100.0, "frames": [[0, "1DB", "07 E6 00 00"], [12, "421", "08 00 00"],
                                                   [25, "18DAF110", "01", {"ext": True}]]})
    assert [(t, i, ms.hex_bytes(d)) for t, i, d, _ in out] == [
        (100.0, "1DB", "07 E6 00 00"), (100.012, "421", "08 00 00"), (100.025, "18DAF110", "01")]
    assert out[2][3] == {"ext": True}


def test_frames_from_the_fixture_reassemble_through_parse_isotp():
    """A bridge mirrors the raw 7BB frames one message each; parse_isotp gets
    exactly the lines a SerialELM capture gave."""
    with open(os.path.join(FIXTURES, "session_leaf_ze0.json")) as f:
        lines = json.load(f)["frames"][0]["uds"]["79B"]["2101"]
    msgs = []
    for n, line in enumerate(lines):
        cid, _, d = line.partition(" ")
        msgs.append({"t": 1000 + n * 0.005, "id": cid, "d": d})
    rebuilt = [ms.frame_line(*ms.parse_frame(m)[1:3]) for m in msgs]
    assert rebuilt == lines
    assert bytes(parse_isotp(rebuilt)) == bytes(parse_isotp(lines))


# ── the source ───────────────────────────────────────────────────────────

def test_start_connects_and_subscribes_the_control_topics():
    src, client, sink = run(started())
    assert client.host == ("broker.example", 1883)
    assert src.connected and src.liveness()["connected"]
    assert (f"{ROOT_T}/status", 1) in client.subscriptions
    assert (f"{ROOT_T}/tx/uds/+", 1) in client.subscriptions
    assert any(c.isdigit() for c in src.name)


def test_start_times_out_without_a_broker():
    client = FakeClient(auto_connect=False)
    src = ms.MqttSource(CFG, client_factory=lambda cid: client)
    with pytest.raises(ConnectionError):
        run(src.start(lambda *a: None, connect_timeout=0.05))
    assert not client.loop_running


def test_unconfigured_source_refuses_to_start():
    src = ms.MqttSource({"host": ""}, client_factory=lambda cid: FakeClient())
    with pytest.raises(ConnectionError):
        run(src.start(lambda *a: None))


def test_credentials_and_tls_reach_the_client():
    src, client = make_source({"username": "u", "password": "p", "tls": True})
    run(src.start(lambda *a: None, connect_timeout=1.0))
    assert client.auth == ("u", "p") and client.tls is True


def test_frames_in_on_frame_out():
    src, client, sink = run(started(ids={"421", "358"}))
    client.deliver(f"{ROOT_T}/rx/421", {"t": 5.0, "id": "421", "d": "08 00 00"})
    client.deliver(f"{ROOT_T}/rx/358", {"t": 5.1, "id": "358", "d": "00 00 00 00 00 00 00 00"})
    assert sink.lines == ["421 08 00 00", "358 00 00 00 00 00 00 00 00"]
    assert sink.frames[0][0] == 5.0 and sink.frames[0][3] == {}
    live = src.liveness()
    assert live["frames"] == 2 and live["t_last"] == 5.1 and live["age_s"] < 1


def test_batch_expansion_reaches_on_frame():
    src, client, sink = run(started({"batch": True}, ids={"421"}))
    assert client.subscriptions[-1] == (f"{ROOT_T}/rx/_batch", 0)
    client.deliver(f"{ROOT_T}/rx/_batch", {"t0": 10.0, "frames": [[0, "1DB", "07 E6"], [7, "421", "08 00 00"]]})
    assert sink.lines == ["1DB 07 E6", "421 08 00 00"]
    assert sink.frames[1][0] == 10.007


def test_malformed_message_is_dropped_not_fatal():
    logs = []
    client = FakeClient()
    src = ms.MqttSource(CFG, client_factory=lambda cid: client, log=logs.append)
    sink = Sink()
    run(src.start(sink.on_frame, connect_timeout=1.0))
    client.deliver(f"{ROOT_T}/rx/421", b"not json")
    client.deliver(f"{ROOT_T}/rx/421", {"t": 0, "d": "zz"})
    client.deliver("other/prefix/rx/421", {"t": 0, "d": "01"})
    client.deliver(f"{ROOT_T}/rx/421", {"t": 0, "d": "01"})
    assert sink.lines == ["421 01"]
    assert sum("dropped" in l for l in logs) == 2


def test_subscribe_auto_follows_the_id_set():
    src, client, sink = run(started())
    assert (f"{ROOT_T}/rx/#", 0) in client.subscriptions          # nothing asked for yet: the bus
    run(src.subscribe({"421", "358"}))
    assert set(client.subscriptions[-2:]) == {(f"{ROOT_T}/rx/358", 0), (f"{ROOT_T}/rx/421", 0)}
    assert client.unsubscribed == [f"{ROOT_T}/rx/#"]
    n = len(client.subscriptions)
    run(src.subscribe({"421", "5C5"}))            # 358 goes, 5C5 comes
    assert client.subscriptions[n:] == [(f"{ROOT_T}/rx/5C5", 0)]
    assert client.unsubscribed == [f"{ROOT_T}/rx/#", f"{ROOT_T}/rx/358"]
    n = len(client.subscriptions)
    run(src.subscribe({"421", "5C5"}))            # unchanged: nothing happens
    assert len(client.subscriptions) == n and len(client.unsubscribed) == 2
    run(src.subscribe(None))                      # everything
    assert client.subscriptions[-1] == (f"{ROOT_T}/rx/#", 0)
    assert set(client.unsubscribed) == {f"{ROOT_T}/rx/#", f"{ROOT_T}/rx/358", f"{ROOT_T}/rx/421", f"{ROOT_T}/rx/5C5"}


def test_subscribe_before_start_is_applied_on_connect():
    src, client = make_source()
    run(src.subscribe({"421"}))
    assert client.subscriptions == []
    run(src.start(lambda *a: None, connect_timeout=1.0))
    assert (f"{ROOT_T}/rx/421", 0) in client.subscriptions


def test_explicit_topic_list_overrides_auto():
    src, client, sink = run(started({"subscribe": ["421", "hakake/test/car/rx/1DB"]}, ids={"358"}))
    assert (f"{ROOT_T}/rx/421", 0) in client.subscriptions
    assert (f"{ROOT_T}/rx/1DB", 0) in client.subscriptions
    assert all(t != f"{ROOT_T}/rx/358" for t, _ in client.subscriptions)


def test_reconnect_resubscribes_everything():
    src, client, sink = run(started(ids={"421"}))
    client.disconnect()
    assert not src.connected
    client.subscriptions.clear()
    client.simulate_connect()
    assert (f"{ROOT_T}/rx/421", 0) in client.subscriptions
    assert (f"{ROOT_T}/status", 1) in client.subscriptions


# ── UDS requests ─────────────────────────────────────────────────────────

def _published_request(client):
    reqs = [json.loads(p) for t, p, q, r in client.published if t == f"{ROOT_T}/tx/uds"]
    return reqs[-1] if reqs else None


def test_request_publishes_the_envelope_and_resolves_on_the_ack():
    async def go():
        src, client, sink = await started(ids={"7BB"})

        async def bridge():
            await asyncio.sleep(0.02)
            req = _published_request(client)
            client.deliver(f"{ROOT_T}/rx/7BB", {"t": 1, "id": "7BB", "d": "10 29 61 01 FF FF F9 E9"})
            client.deliver(f"{ROOT_T}/tx/uds/{req['req']}", {"req": req["req"], "ok": True, "frames": 1})

        asyncio.ensure_future(bridge())
        ack = await src.send_uds("79b", "7bb", bytes.fromhex("2101"), bs=0, stmin=0x20, timeout=0.5)
        return src, client, sink, ack

    src, client, sink, ack = run(go())
    req = _published_request(client)
    assert req["tx"] == "79B" and req["rx"] == "7BB" and req["data"] == "21 01"
    assert req["bs"] == 0 and req["stmin"] == 32 and req["timeout"] == 0.5
    assert len(req["req"]) == 4 and int(req["req"], 16) >= 0
    pub = [p for p in client.published if p[0] == f"{ROOT_T}/tx/uds"][-1]
    assert pub[2] == 1 and pub[3] is False                   # QoS 1, not retained
    assert ack == {"req": req["req"], "ok": True, "frames": 1}
    assert sink.lines == ["7BB 10 29 61 01 FF FF F9 E9"]
    assert src._pending == {}


def test_request_waits_briefly_for_the_acked_frame_count():
    """The ack (QoS 1) can overtake the last response frame (QoS 0)."""
    async def go():
        src, client, sink = await started(ids={"7BB"})

        async def bridge():
            await asyncio.sleep(0.01)
            req = _published_request(client)["req"]
            client.deliver(f"{ROOT_T}/rx/7BB", {"t": 1, "id": "7BB", "d": "10 29 61 01 FF FF F9 E9"})
            client.deliver(f"{ROOT_T}/tx/uds/{req}", {"req": req, "ok": True, "frames": 2})
            await asyncio.sleep(0.05)
            client.deliver(f"{ROOT_T}/rx/7BB", {"t": 1.1, "id": "7BB", "d": "21 02 87 FF FF FC 44 FF"})

        asyncio.ensure_future(bridge())
        ack = await src.send_uds("79B", "7BB", b"\x21\x01", timeout=0.5)
        return sink, ack

    sink, ack = run(go())
    assert ack["ok"] and len(sink.lines) == 2


def test_request_times_out_without_an_ack():
    async def go():
        src, client, sink = await started()
        ms_saved = ms.ACK_GRACE
        ms.ACK_GRACE = 0.0
        try:
            return await src.send_uds("79B", "7BB", b"\x21\x01", timeout=0.05), client, src
        finally:
            ms.ACK_GRACE = ms_saved

    ack, client, src = run(go())
    assert ack["ok"] is False and ack["error"] == "timeout"
    assert _published_request(client) is not None
    assert src._pending == {}


def test_request_reports_the_bridges_error():
    async def go():
        src, client, sink = await started()

        async def bridge():
            await asyncio.sleep(0.01)
            req = _published_request(client)["req"]
            client.deliver(f"{ROOT_T}/tx/uds/{req}", {"req": req, "ok": False, "error": "bus-off"})

        asyncio.ensure_future(bridge())
        return await src.send_uds("7E0", "7E8", b"\x01\x0c", timeout=0.5)

    ack = run(go())
    assert ack == {"req": ack["req"], "ok": False, "error": "bus-off"}


@pytest.mark.parametrize("data", [b"\x04", b"\x2e\x12\x34", b"\x27\x01", b"\x10\x03", b"\x31\x01", b""])
def test_non_read_services_are_refused_before_publishing(data):
    logs = []
    client = FakeClient()
    src = ms.MqttSource(CFG, client_factory=lambda cid: client, log=logs.append)
    run(src.start(lambda *a: None, connect_timeout=1.0))
    ack = run(src.send_uds("79B", "7BB", data, timeout=0.5))
    assert ack["ok"] is False and ack["error"] == "refused"
    assert _published_request(client) is None
    assert client.published == []


@pytest.mark.parametrize("first", sorted(ms.READ_SERVICES))
def test_every_read_service_is_allowed(first):
    assert ms.is_read_request(bytes([first, 0x01]))


def test_the_whitelist_is_exactly_the_security_md_set():
    assert ms.READ_SERVICES == {0x21, 0x01, 0x03, 0x07}
    assert 0x04 not in ms.READ_SERVICES


def test_listen_only_bridge_refuses_locally():
    src, client, sink = run(started())
    client.deliver(f"{ROOT_T}/status", {"v": 1, "online": True, "bus": "ev", "listen_only": True,
                                        "bridge": "hakake-bridge 0.1"}, retain=True)
    assert src.listen_only is True
    ack = run(src.send_uds("79B", "7BB", b"\x21\x01", timeout=0.5))
    assert ack["error"] == "refused" and _published_request(client) is None


def test_request_while_disconnected_is_offline():
    src, client, sink = run(started())
    client.disconnect()
    ack = run(src.send_uds("79B", "7BB", b"\x21\x01", timeout=0.5))
    assert ack == {"req": ack["req"], "ok": False, "error": "offline"}


# ── liveness ─────────────────────────────────────────────────────────────

def test_status_and_lwt_drive_liveness():
    src, client, sink = run(started())
    assert src.liveness()["online"] is True           # connected, no status yet: assume up
    client.deliver(f"{ROOT_T}/status", {"v": 1, "online": True, "bus": "car", "bitrate": 500000,
                                        "listen_only": False, "fps": 1688, "errors": 0,
                                        "adapter": "socketcan can0", "bridge": "hakake-bridge 0.1",
                                        "t": 1757440000.0}, retain=True)
    live = src.liveness()
    assert live["online"] and live["fps"] == 1688 and live["bridge_online"] is True
    assert src.name.startswith("hakake-bridge 0.1 @ ")
    assert sink.status[-1]["bridge"] == "hakake-bridge 0.1"
    client.deliver(f"{ROOT_T}/status", {"v": 1, "online": False, "bus": "car"}, retain=True)   # the LWT
    live = src.liveness()
    assert live["online"] is False and live["connected"] is True and live["bridge_online"] is False
    client.disconnect()
    assert src.liveness()["online"] is False


def test_stop_cancels_pending_requests_and_stops_the_client():
    async def go():
        src, client, sink = await started()
        task = asyncio.ensure_future(src.send_uds("79B", "7BB", b"\x21\x01", timeout=5.0))
        await asyncio.sleep(0.01)
        await src.stop()
        return await task, client, src

    ack, client, src = run(go())
    assert ack["ok"] is False and ack["error"] == "offline"
    assert not client.loop_running and src.client is None


# ── open_mqtt: the adapter the reader gets ───────────────────────────────

class StubFacade:
    """What plan §4 says a façade does with a source (the shape of
    cantransport.CanFacade); enough to prove the wrapping."""

    adapter_type = "can"

    def __init__(self, source, **kw):
        self.source = source
        self.bus = source.bus
        self.listen_only = source.listen_only
        self.adapter_name = source.name
        self.adapter_port = getattr(source, "port", "")
        self.lines = []
        self.cmds = []

    async def connect(self, log=print):
        await self.source.start(self._on_frame, None, connect_timeout=1.0)

    def _on_frame(self, t, id_hex, data, flags):
        self.lines.append(ms.frame_line(id_hex, data))

    async def send(self, cmd, wait=0.0, timeout=8.0):
        self.cmds.append(cmd)
        return [self.adapter_name] if cmd in ("ATI", "ATZ") else []

    def marker(self):
        return {"can_bus": self.bus, "listen_only": self.listen_only}


@pytest.fixture
def stub_cantransport(monkeypatch):
    mod = types.ModuleType("cantransport")
    mod.CanFacade = StubFacade
    monkeypatch.setitem(sys.modules, "cantransport", mod)
    return mod


def test_open_mqtt_wraps_the_source_in_the_facade(stub_cantransport):
    clients = []

    def factory(cid):
        c = FakeClient()
        clients.append(c)
        return c

    elm = run(ms.open_mqtt(log=lambda *a: None, cfg=CFG, client_factory=factory))
    assert elm.adapter_type == "mqtt"
    assert any(ch.isdigit() for ch in elm.adapter_name)
    assert elm.adapter_port == "mqtt://broker.example:1883/hakake/test/car"
    assert elm.marker() == {"can_bus": "car", "listen_only": False,
                            "remote": {"host": "broker.example", "bus": "car"}}
    assert run(elm.send("ATI")) == [elm.adapter_name]
    client = clients[0]
    # nobody narrowed it yet: the whole bus
    assert (f"{ROOT_T}/rx/#", 0) in client.subscriptions
    # the profile's ATCRA narrows the subscription to the ids in use, and it grows
    run(elm.send("ATCRA 421"))
    assert (f"{ROOT_T}/rx/421", 0) in client.subscriptions and f"{ROOT_T}/rx/#" in client.unsubscribed
    run(elm.send("ATCRA 7BB"))
    run(elm.send("ATCRA 421"))                                   # again: no second SUBSCRIBE
    assert [t for t, _ in client.subscriptions].count(f"{ROOT_T}/rx/421") == 1
    assert (f"{ROOT_T}/rx/7BB", 0) in client.subscriptions
    assert elm.cmds == ["ATI", "ATCRA 421", "ATCRA 7BB", "ATCRA 421"]  # every command still reaches the façade
    client.deliver(f"{ROOT_T}/rx/421", {"t": 0, "id": "421", "d": "08 00 00"})
    assert elm.lines == ["421 08 00 00"]


def test_open_mqtt_with_the_real_facade_serves_atma_and_uds():
    """The real cantransport.CanFacade over an MqttSource: a passive capture
    and a UDS read, end to end, with the fake client standing in for the broker."""
    pytest.importorskip("cantransport")
    from elm327 import passive_capture, set_uds_target
    clients = []

    def factory(cid):
        c = FakeClient()
        clients.append(c)
        return c

    async def go():
        elm = await ms.open_mqtt(log=lambda *a: None, cfg=CFG, client_factory=factory)
        client = clients[0]
        assert elm.adapter_type == "mqtt" and any(ch.isdigit() for ch in elm.adapter_name)
        await elm.send("ATCAF0")
        await elm.send("ATCRA 421")
        client.deliver(f"{ROOT_T}/rx/421", {"t": 1.0, "id": "421", "d": "08 00 00"})
        lines = await passive_capture(elm, "421", 0.05, set_caf=False)
        assert lines == ["421 08 00 00"]
        await set_uds_target(elm, "79B", "7BB")
        assert (f"{ROOT_T}/rx/7BB", 0) in client.subscriptions
        with open(os.path.join(FIXTURES, "session_leaf_ze0.json")) as f:
            fixture_lines = json.load(f)["frames"][0]["uds"]["79B"]["2101"]

        async def bridge():
            await asyncio.sleep(0.02)
            req = _published_request(client)
            assert req["tx"] == "79B" and req["rx"] == "7BB" and req["data"] == "21 01"
            for n, line in enumerate(fixture_lines):
                cid, _, d = line.partition(" ")
                client.deliver(f"{ROOT_T}/rx/7BB", {"t": 2 + n * 0.005, "id": cid, "d": d})
            client.deliver(f"{ROOT_T}/tx/uds/{req['req']}", {"req": req["req"], "ok": True,
                                                              "frames": len(fixture_lines)})

        asyncio.ensure_future(bridge())
        got = await elm.send("2101", wait=0.05, timeout=1.0)
        assert got == fixture_lines
        refused = await elm.send("2E01", wait=0.05, timeout=1.0)
        assert refused == ["NO DATA"] and _published_request(client)["data"] == "21 01"
        assert elm.marker()["remote"] == {"host": "broker.example", "bus": "car"}
        await elm.close()

    run(go())


def test_open_mqtt_refuses_when_unconfigured(stub_cantransport, monkeypatch):
    monkeypatch.delenv("HAKAKE_MQTT_HOST", raising=False)
    with pytest.raises(ConnectionError):
        run(ms.open_mqtt(log=lambda *a: None, cfg={"host": ""}))


def test_open_mqtt_never_runs_without_being_asked():
    """The adapter is selected by name only: nothing in elm327's auto path
    imports this module."""
    import elm327
    src = open(elm327.__file__).read()
    assert "open_mqtt" not in src.split("def detect_adapter")[0]


# ── StatePublisher: what a gauge sees ────────────────────────────────────

RECORD = {"soc": 87.5, "pack_v": 393.2, "hv_current_a": -1.25, "gear": "P", "alive": True,
          "temps_c": [21.0, 22.0], "temps_f": [69.8, 71.6], "tpms_psi": [36, 35, 36, 35],
          "cells": [3.95, 3.96], "timing": {"lbc01": 0.1}, "item_age": {"lbc01": 0.5},
          "items": ["lbc01"], "message": None, "readings": 3, "status": "ok"}


def test_flatten_signals_keeps_scalars_and_scalar_lists_only():
    got = dict(ms.flatten_signals(RECORD))
    assert got["soc"] == 87.5 and got["gear"] == "P" and got["alive"] is True
    assert got["temps_f/1"] == 71.6 and got["tpms_psi/3"] == 35 and got["cells/0"] == 3.95
    assert "timing" not in got and "item_age" not in got and "message" not in got
    assert "items" not in got and "items/0" not in got


def test_state_publisher_publishes_state_and_signals_retained_qos0():
    client = FakeClient()
    sp = ms.StatePublisher(CFG, client_factory=lambda cid: client).connect()
    n = sp.publish_state(RECORD)
    topics = {t: (p, q, r) for t, p, q, r in client.published}
    state = json.loads(topics["hakake/test/state"][0])
    assert state["soc"] == 87.5 and state["v"] == 1 and state["timing"] == {"lbc01": 0.1}
    assert topics["hakake/test/state"][1:] == (0, True)
    assert topics["hakake/test/signal/soc"] == ("87.5", 0, True)
    assert topics["hakake/test/signal/gear"] == ('"P"', 0, True)
    assert topics["hakake/test/signal/alive"] == ("true", 0, True)
    assert topics["hakake/test/signal/temps_f/1"] == ("71.6", 0, True)
    assert topics["hakake/test/signal/cells/1"] == ("3.96", 0, True)
    assert "hakake/test/signal/timing" not in topics
    assert "hakake/test/signal/items/0" not in topics
    assert n == 1 + len(ms.flatten_signals(RECORD))


def test_state_publisher_sends_signals_on_change_and_state_every_cycle():
    client = FakeClient()
    sp = ms.StatePublisher(CFG, client_factory=lambda cid: client).connect()
    sp.publish_state(RECORD)
    client.published.clear()
    n = sp.publish_state(RECORD)
    assert n == 1 and client.published[0][0] == "hakake/test/state"
    client.published.clear()
    sp.publish_state(dict(RECORD, soc=88.0))
    assert [t for t, *_ in client.published] == ["hakake/test/state", "hakake/test/signal/soc"]


def test_state_publisher_publishes_temperatures_as_they_are():
    """Both units, untouched — the record already carries _c and _f."""
    client = FakeClient()
    sp = ms.StatePublisher(CFG, client_factory=lambda cid: client).connect()
    sp.publish_state({"ambient_c": 20.0, "ambient_f": 68.0})
    got = {t: p for t, p, *_ in client.published}
    assert got["hakake/test/signal/ambient_c"] == "20.0" and got["hakake/test/signal/ambient_f"] == "68.0"


def test_module_level_publish_state_is_a_noop_without_config(monkeypatch):
    ms.reset_state_publisher()
    monkeypatch.setattr(ms, "mqtt_config", lambda raw=None, path=None: dict(ms.DEFAULTS))
    assert ms.publish_state({"soc": 1}) == 0
    assert ms._state_disabled is True
    ms.reset_state_publisher()


def test_module_level_publish_state_uses_the_config(monkeypatch):
    ms.reset_state_publisher()
    clients = []

    def factory(cid):
        c = FakeClient()
        clients.append(c)
        return c

    monkeypatch.setattr(ms, "mqtt_config", lambda raw=None, path=None: dict(ms.DEFAULTS, host="h"))
    monkeypatch.setattr(ms, "paho_client", factory)
    try:
        assert ms.publish_state({"soc": 1}, log=lambda *a: None) == 2
        assert clients[0].published[0][0] == "hakake/leaf/state"
        assert ms.publish_state({"soc": 1}, log=lambda *a: None) == 1
    finally:
        ms.reset_state_publisher()


def test_module_level_publish_state_never_raises(monkeypatch):
    ms.reset_state_publisher()

    def boom(cid):
        raise RuntimeError("no paho")

    monkeypatch.setattr(ms, "mqtt_config", lambda raw=None, path=None: dict(ms.DEFAULTS, host="h"))
    monkeypatch.setattr(ms, "paho_client", boom)
    try:
        assert ms.publish_state({"soc": 1}, log=lambda *a: None) == 0
        assert ms._state_disabled is True
    finally:
        ms.reset_state_publisher()


# ── record_session --from-mqtt ───────────────────────────────────────────

def _jsonl(lines):
    return "\n".join(json.dumps(l) for l in lines) + "\n"


def test_from_mqtt_builds_a_replay_fixture_with_relative_times(tmp_path, leaf_profile):
    t0 = 1757440000.0
    fx = json.load(open(os.path.join(FIXTURES, "session_leaf_ze0.json")))
    lbc_lines = fx["frames"][0]["uds"]["79B"]["2101"]
    stream = [
        {"topic": f"{ROOT_T}/status", "payload": {"v": 1, "online": True, "bus": "car"}},
        {"topic": f"{ROOT_T}/rx/421", "payload": {"t": t0 + 0.10, "id": "421", "d": "08 00 00"}},
        {"topic": f"{ROOT_T}/rx/358", "payload": {"t": t0 + 0.20, "id": "358", "d": "00 00 00 00 00 00 00 00"}},
        {"topic": f"{ROOT_T}/tx/uds", "payload": {"req": "a1b2", "tx": "79B", "rx": "7BB", "data": "21 01",
                                                  "bs": 0, "stmin": 0, "timeout": 2.0}},
    ]
    for n, line in enumerate(lbc_lines):
        cid, _, d = line.partition(" ")
        stream.append({"topic": f"{ROOT_T}/rx/7BB", "payload": {"t": t0 + 0.3 + n * 0.005, "id": cid, "d": d}})
    stream += [
        {"topic": f"{ROOT_T}/tx/uds/a1b2", "payload": {"req": "a1b2", "ok": True, "frames": len(lbc_lines)}},
        # mosquitto_sub -F json style: payload as a string
        {"topic": f"{ROOT_T}/rx/421", "payload": json.dumps({"t": t0 + 1.5, "id": "421", "d": "10 00 00"})},
        {"topic": f"{ROOT_T}/rx/_batch", "payload": {"t0": t0 + 2.2, "frames": [[0, "358", "01 00 00 00 00 00 00 00"],
                                                                                [30, "421", "18 00 00"]]}},
    ]
    src = tmp_path / "drive.jsonl"
    src.write_text(_jsonl(stream))
    out = tmp_path / "session.json"
    path = rec.from_mqtt(str(src), str(out), vehicle="leaf_ze0", period=1.0)
    doc = json.load(open(path))
    assert doc["hakake_replay"] == 1 and doc["vehicle"] == "leaf_ze0" and doc["synthetic"] is False
    frames = doc["frames"]
    assert [f["t"] for f in frames] == [0.0, 1.0, 2.0]                    # offsets, never epoch
    assert all(f["t"] < 1e6 for f in frames)
    assert frames[0]["passive"]["421"] == ["421 08 00 00"]
    assert frames[0]["passive"]["358"] == ["358 00 00 00 00 00 00 00 00"]
    assert frames[0]["uds"]["79B"]["2101"] == lbc_lines                    # request → its rx frames
    assert "7BB" not in frames[0]["passive"]
    assert frames[1]["passive"]["421"] == ["421 10 00 00"]
    assert frames[2]["passive"]["421"] == ["421 18 00 00"] and frames[2]["passive"]["358"][0].startswith("358 01")
    assert "mqtt" in " ".join(doc["source"]).lower()
    assert "broker" not in json.dumps(doc) and "example" not in json.dumps(doc)
    from elm327 import ReplayELM, set_uds_target, passive_capture
    elm = ReplayELM(path)
    run(set_uds_target(elm, "79B", "7BB"))
    assert run(elm.send("2101")) == lbc_lines
    run(elm.send("ATCAF0"))
    assert run(passive_capture(elm, "421", 0.0, set_caf=False)) == ["421 08 00 00"]


def test_from_mqtt_refuses_an_empty_stream(tmp_path, leaf_profile):
    src = tmp_path / "empty.jsonl"
    src.write_text(_jsonl([{"topic": f"{ROOT_T}/status", "payload": {"online": True}}]))
    with pytest.raises(ValueError):
        rec.from_mqtt(str(src), str(tmp_path / "o.json"), vehicle="leaf_ze0")


def test_from_mqtt_cli(tmp_path, leaf_profile, capsys):
    src = tmp_path / "s.jsonl"
    src.write_text(_jsonl([{"topic": f"{ROOT_T}/rx/421", "payload": {"t": 5.0, "id": "421", "d": "08 00 00"}}]))
    out = tmp_path / "o.json"
    assert rec.main(["--from-mqtt", str(src), "--out", str(out), "--vehicle", "leaf_ze0"]) == 0
    assert json.load(open(out))["frames"][0]["t"] == 0.0
