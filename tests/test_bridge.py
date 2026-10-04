# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""hakake-bridge — the Raspberry Pi side of docs/MQTT.md, without a Pi.

python-can's ``virtual`` interface is the car (a fake ECU answers ISO-TP on
it), a fake paho client is the broker. What is proven: a frame on the bus
becomes exactly the documented JSON, the kernel-style filter keeps unlisted
ids out, batches bundle, a read request goes round through can-isotp and
comes back as raw frames plus an ack, and nothing but a read ever reaches
the bus — not even with ``listen_only`` off.
"""

import json
import os
import sys
import threading
import time

import pytest

from conftest import ROOT, FIXTURES  # noqa: F401

can = pytest.importorskip("can")
isotp = pytest.importorskip("isotp")

sys.path.insert(0, os.path.join(ROOT, "bridge"))
import hakake_bridge as hb  # noqa: E402
from leaf_decoders import parse_isotp  # noqa: E402

_chan = [0]


class FakeClient:
    def __init__(self):
        self.published = []            # (topic, payload, qos, retain)
        self.subscriptions = []
        self.will = None
        self.on_connect = self.on_disconnect = self.on_message = None

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def topics(self, prefix):
        return [(t, json.loads(p)) for t, p, *_ in self.published if t.startswith(prefix)]

    def wait_for(self, topic, n=1, timeout=2.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            got = [json.loads(p) for t, p, *_ in self.published if t == topic]
            if len(got) >= n:
                return got
            time.sleep(0.005)
        return [json.loads(p) for t, p, *_ in self.published if t == topic]


def make_bridge(**over):
    _chan[0] += 1
    ch = f"hakake-test-{_chan[0]}-{os.getpid()}"
    cfg = hb.load_config(None, dict({"prefix": "hakake/t", "bus": "car", "interface": "virtual",
                                     "channel": ch, "stats_s": 0, "status_s": 100}, **over))
    car = can.Bus(interface="virtual", channel=ch)
    pi = can.Bus(interface="virtual", channel=ch)
    bridge = hb.Bridge(cfg, bus=pi, client=FakeClient())
    bridge.apply_filters()
    bridge.attach_client(bridge.client)
    return bridge, car


@pytest.fixture
def live():
    """A started bridge (notifier + publisher threads) and the car's end of the bus."""
    bridge, car = make_bridge()
    bridge.start()
    yield bridge, car
    bridge.stop()
    car.shutdown()
    bridge.bus.shutdown()


ROOT_T = "hakake/t/car"


# ── serialisation ────────────────────────────────────────────────────────

def test_frame_payload_is_the_documented_json():
    p = hb.frame_payload(1757440000.123456, "421", "08 00 00")
    assert json.loads(p) == {"t": 1757440000.123456, "id": "421", "d": "08 00 00"}
    p = hb.frame_payload(1.0, "18DAF110", "01", err=False, ext=True)
    assert json.loads(p) == {"t": 1.0, "id": "18DAF110", "d": "01", "ext": True}
    p = hb.frame_payload(1.0, "001", "", err=True)
    assert json.loads(p) == {"t": 1.0, "id": "001", "d": "", "err": True}


def test_batch_payload_is_the_documented_json():
    p = hb.batch_payload(100.0, [(0, "1DB", "07 E6", ""), (12, "421", "08 00 00", ',{"ext":true}')])
    assert json.loads(p) == {"t0": 100.0, "frames": [[0, "1DB", "07 E6"], [12, "421", "08 00 00", {"ext": True}]]}


def test_id_and_data_formatting_preserve_dlc():
    assert hb.id_hex(0x21) == "021" and hb.id_hex(0x7BB) == "7BB" and hb.id_hex(0x18DAF110, True) == "18DAF110"
    assert hb.fmt_data(b"\x08\x00\x00") == "08 00 00" and hb.fmt_data(b"") == ""


def test_load_config_ids_and_overrides(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"ids": ["421", "358", "7bb"], "bus": "ev", "batch_ms": 50, "secret": 1}))
    cfg = hb.load_config(str(p), {"listen_only": True, "host": None})
    assert cfg["ids"] == [0x358, 0x421, 0x7BB] and cfg["bus"] == "ev" and cfg["batch_ms"] == 50
    assert cfg["listen_only"] is True and cfg["host"] == "127.0.0.1" and "secret" not in cfg
    assert hb.load_config(None, {"ids": "all"})["ids"] == "all"
    assert hb.load_config(None, {"ids": []})["ids"] == "all"


# ── mirroring ────────────────────────────────────────────────────────────

def test_a_frame_on_the_bus_becomes_one_rx_message(live):
    bridge, car = live
    car.send(can.Message(arbitration_id=0x421, data=[0x08, 0x00, 0x00], is_extended_id=False))
    got = bridge.client.wait_for(f"{ROOT_T}/rx/421")
    assert len(got) == 1
    assert got[0]["id"] == "421" and got[0]["d"] == "08 00 00"
    assert isinstance(got[0]["t"], float) and got[0]["t"] > 1e9
    assert set(got[0]) == {"t", "id", "d"}
    topic, payload, qos, retain = [p for p in bridge.client.published if p[0].endswith("/rx/421")][0]
    assert qos == 0 and retain is False
    assert bridge.stats.frames_in == 1 and bridge.stats.frames_out == 1


def test_eight_byte_frame_and_extended_id(live):
    bridge, car = live
    car.send(can.Message(arbitration_id=0x385, data=bytes.fromhex("000095 8F 92 92 F0 00".replace(" ", "")),
                         is_extended_id=False))
    car.send(can.Message(arbitration_id=0x18DAF110, data=[1], is_extended_id=True))
    assert bridge.client.wait_for(f"{ROOT_T}/rx/385")[0]["d"] == "00 00 95 8F 92 92 F0 00"
    e = bridge.client.wait_for(f"{ROOT_T}/rx/18DAF110")[0]
    assert e["id"] == "18DAF110" and e["ext"] is True


def test_id_filter_keeps_unlisted_frames_out():
    bridge, car = make_bridge(ids=["421"])
    bridge.start()
    try:
        car.send(can.Message(arbitration_id=0x358, data=[0] * 8, is_extended_id=False))
        car.send(can.Message(arbitration_id=0x421, data=[8, 0, 0], is_extended_id=False))
        assert bridge.client.wait_for(f"{ROOT_T}/rx/421")
        time.sleep(0.05)
        assert bridge.client.topics(f"{ROOT_T}/rx/358") == []
        assert bridge.bus.filters == [{"can_id": 0x421, "can_mask": 0x7FF, "extended": False}]
    finally:
        bridge.stop()
        car.shutdown()
        bridge.bus.shutdown()


def test_batch_mode_bundles_frames_with_ms_offsets():
    bridge, car = make_bridge(batch_ms=50)
    bridge.start()
    try:
        t0 = time.time()
        for n in range(3):
            car.send(can.Message(arbitration_id=0x1DB, data=[n], is_extended_id=False))
        time.sleep(0.02)
        bridge.flush_batch(force=True)
        got = bridge.client.wait_for(f"{ROOT_T}/rx/_batch")
        assert len(got) == 1
        b = got[0]
        assert abs(b["t0"] - t0) < 1.0
        assert [f[1] for f in b["frames"]] == ["1DB"] * 3
        assert [f[2] for f in b["frames"]] == ["00", "01", "02"]
        assert all(0 <= f[0] < 1000 for f in b["frames"])
        assert bridge.client.topics(f"{ROOT_T}/rx/1DB") == []      # never both
        assert bridge.stats.frames_out == 3 and bridge.stats.msgs_out == 1
    finally:
        bridge.stop()
        car.shutdown()
        bridge.bus.shutdown()


def test_bounded_queue_drops_the_oldest_and_counts():
    bridge, car = make_bridge(queue=10)
    try:
        for n in range(30):
            bridge.on_can(can.Message(arbitration_id=0x421, data=[n], timestamp=1.0 + n, is_extended_id=False))
        assert bridge.q.qsize() == 10 and bridge.stats.dropped == 20
        bridge.drain()
        got = bridge.client.topics(f"{ROOT_T}/rx/421")
        assert [json.loads(json.dumps(p))["d"] for _, p in got] == [f"{n:02X}" for n in range(20, 30)]
    finally:
        car.shutdown()
        bridge.bus.shutdown()


# ── status ───────────────────────────────────────────────────────────────

def test_status_and_lwt_shapes(live):
    bridge, car = live
    s = json.loads(bridge.status_payload())
    for k in ("v", "online", "bus", "bitrate", "listen_only", "fps", "errors", "adapter", "bridge", "t",
              "dropped", "queue", "fps_out"):
        assert k in s
    assert s["v"] == 1 and s["online"] is True and s["bus"] == "car" and s["listen_only"] is False
    assert s["bridge"].startswith("hakake-bridge ") and any(c.isdigit() for c in s["bridge"])
    topic, payload, qos, retain = bridge.client.will
    assert topic == f"{ROOT_T}/status" and qos == 1 and retain is True
    assert json.loads(payload) == {"v": 1, "online": False, "bus": "car", "bridge": s["bridge"]}


def test_connect_subscribes_tx_uds_and_publishes_status(live):
    bridge, car = live
    bridge.client.on_connect(bridge.client, None, {}, 0, None)
    assert (f"{ROOT_T}/tx/uds", 1) in bridge.client.subscriptions
    topic, payload, qos, retain = [p for p in bridge.client.published if p[0] == f"{ROOT_T}/status"][-1]
    assert qos == 1 and retain is True and json.loads(payload)["online"] is True


def test_stop_publishes_offline():
    bridge, car = make_bridge()
    bridge.start()
    bridge.stop()
    topic, payload, qos, retain = bridge.client.published[-1]
    assert topic == f"{ROOT_T}/status" and json.loads(payload)["online"] is False and retain
    car.shutdown()
    bridge.bus.shutdown()


# ── UDS through can-isotp ────────────────────────────────────────────────

class FakeEcu(threading.Thread):
    """Answers one ISO-TP request id with a canned payload, on the car's end of the bus."""

    def __init__(self, bus, txid, rxid, answers):
        super().__init__(daemon=True)
        self.bus, self.answers = bus, answers          # request bytes → response bytes
        addr = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=txid, rxid=rxid)
        self.layer = isotp.TransportLayer(rxfn=self._rx, txfn=self._tx, address=addr,
                                          params={"tx_padding": None, "stmin": 0, "blocksize": 0,
                                                  "rx_flowcontrol_timeout": 1000,
                                                  "rx_consecutive_frame_timeout": 1000},
                                          read_timeout=0.005)
        self.seen = []
        self._halt = threading.Event()

    def _rx(self, t):
        m = self.bus.recv(timeout=t)
        if m is None:
            return None
        return isotp.CanMessage(arbitration_id=m.arbitration_id, dlc=m.dlc, data=bytes(m.data),
                                extended_id=bool(m.is_extended_id))

    def _tx(self, m):
        self.bus.send(can.Message(arbitration_id=m.arbitration_id, data=bytes(m.data), is_extended_id=False))

    def run(self):
        while not self._halt.is_set():
            self.layer.process()
            req = self.layer.recv()
            if req is not None:
                self.seen.append(bytes(req))
                ans = self.answers.get(bytes(req))
                if ans is not None:
                    self.layer.send(ans)
            time.sleep(min(self.layer.sleep_time(), 0.002))

    def stop(self):
        self._halt.set()
        self.join(timeout=1.0)


def _group01_payload():
    with open(os.path.join(FIXTURES, "session_leaf_ze0.json")) as f:
        lines = json.load(f)["frames"][0]["uds"]["79B"]["2101"]
    return lines, b"\x61\x01" + bytes(parse_isotp(lines))


def test_read_request_round_trip_mirrors_raw_frames_and_acks(live):
    bridge, car = live
    lines, payload = _group01_payload()
    ecu = FakeEcu(car, txid=0x7BB, rxid=0x79B, answers={b"\x21\x01": payload})
    ecu.start()
    try:
        ack = bridge.handle_uds({"req": "a1b2", "tx": "79B", "rx": "7BB", "data": "21 01",
                                 "bs": 0, "stmin": 0, "timeout": 2.0})
    finally:
        ecu.stop()
    assert ack["ok"] is True and ack["req"] == "a1b2"
    assert ecu.seen == [b"\x21\x01"]
    frames = [p for _, p in bridge.client.topics(f"{ROOT_T}/rx/7BB")]
    assert len(frames) == ack["frames"] >= 6                      # FF + consecutive frames
    rebuilt = [f"{f['id']} {f['d']}" for f in frames]
    assert rebuilt[0].startswith("7BB 10 29 61 01")
    assert bytes(parse_isotp(rebuilt)) == payload[2:]
    assert bytes(parse_isotp(rebuilt)) == bytes(parse_isotp(lines))
    # our own request and flow-control frames are not mirrored (a SocketCAN
    # socket does not receive its own frames; the ELM never echoed them either)
    assert bridge.client.topics(f"{ROOT_T}/rx/79B") == []
    assert bridge.stats.tx >= 2                                   # request + flow control
    # the ack came after the frames, on its own topic, QoS 1
    order = [t for t, *_ in bridge.client.published]
    assert order.index(f"{ROOT_T}/tx/uds/a1b2") > max(i for i, t in enumerate(order) if t == f"{ROOT_T}/rx/7BB")
    assert [q for t, _, q, _ in bridge.client.published if t == f"{ROOT_T}/tx/uds/a1b2"] == [1]


def test_request_via_on_message_uses_the_worker(live):
    bridge, car = live
    lines, payload = _group01_payload()
    ecu = FakeEcu(car, txid=0x7BB, rxid=0x79B, answers={b"\x21\x01": payload})
    ecu.start()

    class Msg:
        topic = f"{ROOT_T}/tx/uds"
        payload = json.dumps({"req": "c3d4", "tx": "79B", "rx": "7BB", "data": "21 01", "timeout": 2.0}).encode()

    try:
        bridge.client.on_message(bridge.client, None, Msg())
        got = bridge.client.wait_for(f"{ROOT_T}/tx/uds/c3d4", timeout=3.0)
    finally:
        ecu.stop()
    assert got and got[0]["ok"] is True


def test_silent_ecu_times_out(live):
    bridge, car = live
    t0 = time.time()
    ack = bridge.handle_uds({"req": "e5f6", "tx": "79B", "rx": "7BB", "data": "21 01", "timeout": 0.3})
    assert ack["ok"] is False and ack["error"] == "timeout" and ack["frames"] == 0
    assert 0.25 < time.time() - t0 < 2.0
    assert car.recv(timeout=0.2) is not None                      # the request did go out


@pytest.mark.parametrize("data", ["04", "2E 12 34", "27 01", "10 03", "31 01 FF 00", "3D", "22 F1 90", "1A 81", ""])
def test_non_read_services_never_reach_the_bus(live, data):
    bridge, car = live
    ack = bridge.handle_uds({"req": "r1", "tx": "79B", "rx": "7BB", "data": data, "timeout": 1.0})
    assert ack == {"req": "r1", "ok": False, "error": "refused", "reason": ack["reason"]}
    assert car.recv(timeout=0.1) is None
    assert bridge.stats.refused == 1 and bridge.stats.tx == 0
    topic, payload, qos, retain = bridge.client.published[-1]
    assert topic == f"{ROOT_T}/tx/uds/r1" and json.loads(payload)["error"] == "refused"


@pytest.mark.parametrize("first", sorted(hb.READ_SERVICES))
def test_the_read_set_is_security_md_exactly(first):
    assert hb.is_read_request(bytes([first]))
    assert hb.READ_SERVICES == {0x21, 0x01, 0x03, 0x07} and 0x04 not in hb.READ_SERVICES


def test_listen_only_refuses_even_a_read():
    bridge, car = make_bridge(listen_only=True)
    bridge.start()
    try:
        ack = bridge.handle_uds({"req": "lo", "tx": "79B", "rx": "7BB", "data": "21 01", "timeout": 1.0})
        assert ack["error"] == "refused" and "listen-only" in ack["reason"]
        assert car.recv(timeout=0.1) is None
        assert json.loads(bridge.status_payload())["listen_only"] is True
        # mirroring still works
        car.send(can.Message(arbitration_id=0x1DB, data=[7], is_extended_id=False))
        assert bridge.client.wait_for(f"{ROOT_T}/rx/1DB")
    finally:
        bridge.stop()
        car.shutdown()
        bridge.bus.shutdown()


def test_malformed_requests_are_answered_or_ignored_never_sent(live):
    bridge, car = live
    assert bridge.handle_uds({"tx": "79B"}) is None                         # no req → nothing
    assert bridge.handle_uds({"req": "a/b", "tx": "79B"}) is None            # unusable req id
    ack = bridge.handle_uds({"req": "x1", "tx": "79B", "rx": "7BB", "data": "2 1"})
    assert ack["ok"] is False and ack["error"] == "error"
    ack = bridge.handle_uds({"req": "x2", "rx": "7BB", "data": "21 01"})
    assert ack["ok"] is False and ack["error"] == "error"
    assert car.recv(timeout=0.1) is None


def test_response_id_is_added_to_the_filter():
    bridge, car = make_bridge(ids=["421"])
    bridge.start()
    lines, payload = _group01_payload()
    ecu = FakeEcu(car, txid=0x7BB, rxid=0x79B, answers={b"\x21\x01": payload})
    ecu.start()
    try:
        ack = bridge.handle_uds({"req": "f1", "tx": "79B", "rx": "7BB", "data": "21 01", "timeout": 2.0})
        assert ack["ok"] is True
        assert {f["can_id"] for f in bridge.bus.filters} == {0x421, 0x7BB}
    finally:
        ecu.stop()
        bridge.stop()
        car.shutdown()
        bridge.bus.shutdown()


def test_batch_is_flushed_before_the_ack():
    bridge, car = make_bridge(batch_ms=500)
    bridge.start()
    lines, payload = _group01_payload()
    ecu = FakeEcu(car, txid=0x7BB, rxid=0x79B, answers={b"\x21\x01": payload})
    ecu.start()
    try:
        ack = bridge.handle_uds({"req": "b1", "tx": "79B", "rx": "7BB", "data": "21 01", "timeout": 2.0})
        assert ack["ok"] is True
        order = [t for t, *_ in bridge.client.published]
        assert f"{ROOT_T}/rx/_batch" in order
        assert order.index(f"{ROOT_T}/tx/uds/b1") > order.index(f"{ROOT_T}/rx/_batch")
        frames = [fr for _, b in bridge.client.topics(f"{ROOT_T}/rx/_batch") for fr in b["frames"] if fr[1] == "7BB"]
        assert len(frames) == ack["frames"]
    finally:
        ecu.stop()
        bridge.stop()
        car.shutdown()
        bridge.bus.shutdown()


def test_no_vehicle_and_no_secrets_in_the_bridge_tree():
    tree = os.path.join(ROOT, "bridge")
    for name in os.listdir(tree):
        if not os.path.isfile(os.path.join(tree, name)):
            continue
        with open(os.path.join(tree, name), errors="replace") as f:
            text = f.read()
        home_mac, home_linux = "/" + "Users/", "/" + "home/"          # spelled so the sweep does not trip on this line
        assert home_mac not in text and home_linux not in text.replace(home_linux + "pi", "")
    with open(os.path.join(tree, "config.example.json")) as f:
        ex = json.load(f)
    assert ex["host"] in ("", "127.0.0.1") and not {"username", "password", "tls"} & set(ex)
    assert set(ex) <= set(hb.DEFAULTS)


# ── what the bridge will put on the bus, and how it holds up ─────────────

def _msg(payload, topic=f"{ROOT_T}/tx/uds"):
    class M:
        pass
    m = M()
    m.topic, m.payload = topic, payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return m


def _nothing_on(car, wait=0.3):
    return car.recv(timeout=wait) is None


def test_requests_go_only_to_configured_targets(live):
    bridge, car = live
    for tx, rx in (("1DB", "7FF"), ("7DF", "7E8"), ("7BB", "79B")):
        ack = bridge.handle_uds({"req": "r1", "tx": tx, "rx": rx, "data": "21 01", "timeout": 0.5})
        assert ack["ok"] is False and ack["error"] == "refused" and "uds_targets" in ack["reason"], tx
    assert _nothing_on(car)
    assert bridge.stats.refused >= 3


def test_targets_come_from_config_and_the_command_line():
    cfg = hb.load_config(None, {"uds_targets": ["7E0:7E8", "18DA10F1:18DAF110"]})
    assert cfg["uds_targets"] == {(0x7E0, 0x7E8), (0x18DA10F1, 0x18DAF110)}
    assert hb.load_config(None)["uds_targets"] == {(0x79B, 0x7BB), (0x744, 0x764)}
    for bad in (["79B"], ["79B:79B"], ["ZZZ:7BB"], ["79B:200000000"]):
        with pytest.raises(ValueError):
            hb.load_config(None, {"uds_targets": bad})


def test_a_29_bit_request_goes_out_as_a_29_bit_frame():
    bridge, car = make_bridge(uds_targets=["18DA10F1:18DAF110"])
    bridge.start()
    try:
        bridge.handle_uds({"req": "x29", "tx": "18DA10F1", "rx": "18DAF110", "data": "21 01", "timeout": 0.3})
        m = car.recv(timeout=1.0)
        assert m is not None and m.arbitration_id == 0x18DA10F1 and m.is_extended_id is True
    finally:
        bridge.stop()
        car.shutdown()
        bridge.bus.shutdown()


def test_an_ev_bridge_is_listen_only_whatever_the_config_says():
    bridge, car = make_bridge(bus="ev", listen_only=False)
    try:
        assert bridge.listen_only is True and bridge.cfg["listen_only"] is True
        ack = bridge.handle_uds({"req": "e1", "tx": "79B", "rx": "7BB", "data": "21 01", "timeout": 0.3})
        assert ack["error"] == "refused" and _nothing_on(car)
    finally:
        car.shutdown()
        bridge.bus.shutdown()


def test_a_bad_request_is_answered_and_the_next_one_still_runs(live):
    bridge, car = live
    lines, payload = _group01_payload()
    ecu = FakeEcu(car, txid=0x7BB, rxid=0x79B, answers={b"\x21\x01": payload})
    ecu.start()
    try:
        bridge.client.on_message(bridge.client, None, _msg({"req": "bad", "tx": "79B", "rx": "7BB",
                                                            "data": "21 01", "stmin": 300}))
        assert bridge.client.wait_for(f"{ROOT_T}/tx/uds/bad", timeout=3.0)[0]["ok"] is False
        bridge.client.on_message(bridge.client, None, _msg({"req": "good", "tx": "79B", "rx": "7BB",
                                                            "data": "21 01", "timeout": 2.0}))
        assert bridge.client.wait_for(f"{ROOT_T}/tx/uds/good", timeout=3.0)[0]["ok"] is True
        assert bridge._uds_thread.is_alive()
    finally:
        ecu.stop()


def test_unreadable_messages_never_raise_on_the_broker_thread(live):
    bridge, _ = live
    for raw in (b"[" * 200000, b"\xff\xfe not json", b"{" + b" " * (70 * 1024) + b"}", b"null", b"[]"):
        bridge._on_message(bridge.client, None, _msg(raw))       # must not raise
    assert bridge._uds_thread.is_alive()


def test_a_full_request_queue_answers_busy():
    bridge, car = make_bridge()                                    # not started: nothing drains the queue
    try:
        for i in range(hb.UDS_QUEUE + 5):
            bridge._on_message(bridge.client, None, _msg({"req": f"q{i}", "tx": "79B", "rx": "7BB", "data": "21 01"}))
        busy = [p for t, p in bridge.client.topics(f"{ROOT_T}/tx/uds/") if p.get("error") == "busy"]
        assert len(busy) == 5
    finally:
        car.shutdown()
        bridge.bus.shutdown()


def test_a_request_that_waited_too_long_is_not_sent(live):
    bridge, car = live
    bridge._uds_requests.put_nowait(({"req": "old", "tx": "79B", "rx": "7BB", "data": "21 01", "timeout": 0.5},
                                     time.monotonic() - 10))
    got = bridge.client.wait_for(f"{ROOT_T}/tx/uds/old", timeout=3.0)
    assert got and got[0]["error"] == "expired"
    assert _nothing_on(car)


def test_a_receive_error_marks_the_bridge_offline():
    bridge, car = make_bridge()
    try:
        hb._Listener(bridge).on_error(OSError("device gone"))
        status = json.loads(bridge.status_payload())
        assert status["online"] is False and "device gone" in status["error"]
        assert bridge.client.topics(f"{ROOT_T}/status")[-1][1]["online"] is False
    finally:
        car.shutdown()
        bridge.bus.shutdown()
