# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The raw output console (docs/CONSOLE.md): the framework tile, the ring, the
decimation, the taps, the endpoint and the tile's markup.

No hardware and no network — the transports are exercised through their own
fakes, the way tests/test_cantransport.py and tests/test_mqtt_source.py do.
"""
import json
import os
import time

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import cantransport as ct  # noqa: E402
import console as con  # noqa: E402  (web/console.py)
import vehicles  # noqa: E402
from vehicles import get_vehicle, validate_profile  # noqa: E402


# ── the vehicle-independent built-in tile ──

def test_the_console_is_a_framework_tile_every_profile_gets():
    assert [t["id"] for t in vehicles.FRAMEWORK_TILES] == ["console"]
    tile = vehicles.FRAMEWORK_TILES[0]
    assert tile["name"] == "Raw output"
    assert tile["items"] == [], "the console polls nothing — it taps what the reader already does"
    for name in ("leaf_ze0", "lancer_2009"):
        v = get_vehicle(name)
        assert "console" not in {t["id"] for t in v.TILES}, "the profile must not declare it"
        merged = {t["id"]: t for t in vehicles.tiles(v)}
        assert merged["console"]["name"] == "Raw output"
        assert vehicles.default_span(v)["console"] == 12          # full width
        default = next(t for t in vehicles.default_tiles(v) if t["id"] == "console")
        assert default["enabled"] is False, "off by default: the tap is never armed unasked"


def test_the_reader_binds_the_merged_tile_views(use_vehicle):
    import reader as rd
    for name in ("leaf_ze0", "lancer_2009"):
        use_vehicle(name)
        assert "console" in {t["id"] for t in rd.TILES}
        assert rd.DEFAULT_SPAN["console"] == 12
        assert rd.DEFAULT_TILES["tiles"][-1]["id"] == "console"


def test_enabling_the_console_polls_nothing_extra(use_vehicle):
    """The point of an empty `items`: the bandwidth of every other tile is unchanged."""
    import reader as rd
    rd_ = use_vehicle("leaf_ze0")  # noqa: F841
    base = {"tiles": [{"id": "soc", "enabled": True}]}
    with_console = {"tiles": [{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}]}
    assert rd.enabled_items(with_console) == rd.enabled_items(base)


def test_a_profile_may_not_declare_a_framework_tile(leaf_profile):
    import types
    m = types.ModuleType("vehicles.collide")
    for k in dir(leaf_profile):
        if not k.startswith("__"):
            setattr(m, k, getattr(leaf_profile, k))
    m.NAME = "collide"
    m.TILES = list(leaf_profile.TILES) + [{"id": "console", "name": "Mine", "items": []}]
    m.DEFAULT_SPAN = dict(leaf_profile.DEFAULT_SPAN, console=4)
    problems = validate_profile(m)
    assert any("framework tile" in p for p in problems), problems
    assert any("DEFAULT_SPAN may not set a span" in p for p in problems), problems


# ── the ring ──

def test_the_ring_is_bounded_and_evicts_the_oldest():
    r = con.Ring(capacity=10, ids=["421"], rate_cap=10_000)
    for i in range(50):
        r.add("frame", f"421 {i:02X}", "421")
    s = r.stats()
    assert s["held"] == 10 and s["kept"] == 50 and s["evicted"] == 40
    rows = r.entries(limit=100)
    assert len(rows) == 10
    assert [e["seq"] for e in rows] == list(range(41, 51)), "oldest first, newest kept"


def test_the_cursor_is_a_sequence_number_not_a_time():
    """Two frames in the same millisecond must still be distinguishable."""
    r = con.Ring(ids=["421"], rate_cap=10_000)
    t = time.monotonic()
    a = r.add("frame", "421 01", "421", t=t)
    b = r.add("frame", "421 02", "421", t=t)
    assert a["wall"] == pytest.approx(b["wall"], abs=0.05)
    assert b["seq"] == a["seq"] + 1
    assert [e["text"] for e in r.entries(since=a["seq"])] == ["421 02"]
    assert r.entries(since=b["seq"]) == []


def test_unknown_ids_are_dropped_and_counted_unless_everything():
    r = con.Ring(ids=["421"], rate_cap=10_000)
    assert r.add("frame", "421 01", "421") is not None
    assert r.add("frame", "5B3 01", "5B3") is None
    s = r.stats()
    assert s["dropped"] == 1 and s["dropped_by_id"] == {"5B3": 1} and s["kept"] == 1
    assert r.wants("frame", "421") and not r.wants("frame", "5B3")

    lossy = con.Ring(ids=["421"], rate_cap=10_000, everything=True)
    assert lossy.add("frame", "5B3 01", "5B3") is not None
    assert lossy.stats()["everything"] is True


def test_the_per_id_rate_cap_thins_a_firehose_and_says_how_much():
    r = con.Ring(ids=["421", "5B3"], rate_cap=3)
    t = time.monotonic()
    for i in range(100):                       # 100 frames of each id inside one second
        r.add("frame", f"421 {i:02X}", "421", t=t)
        r.add("frame", f"5B3 {i:02X}", "5B3", t=t)
    s = r.stats()
    assert s["kept"] == 6, "three of each id in that second"
    assert s["dropped"] == 194
    assert s["dropped_by_id"] == {"421": 97, "5B3": 97}
    r.add("frame", "421 FF", "421", t=t + 1.0)  # the next second starts fresh
    assert r.stats()["kept"] == 7


def test_only_frames_are_decimated():
    """An answer or an event is not a stream: dropping one loses the thing the
    person was watching for."""
    r = con.Ring(ids=[], rate_cap=1)
    t = time.monotonic()
    for kind in ("uds", "adapter", "text", "event"):
        for _ in range(5):
            assert r.add(kind, f"{kind} line", "7BB", t=t) is not None
    assert r.stats()["dropped"] == 0 and r.stats()["kept"] == 20


def test_entries_filter_by_kind_and_id_and_return_the_newest_within_a_limit():
    r = con.Ring(ids=["421", "5B3"], rate_cap=10_000)
    for i in range(5):
        r.add("frame", f"421 {i:02X}", "421")
        r.add("frame", f"5B3 {i:02X}", "5B3")
        r.add("event", f"event {i}")
    assert [e["text"] for e in r.entries(ids=["421"])] == [f"421 {i:02X}" for i in range(5)]
    assert len(r.entries(kind=["event"])) == 5
    assert [e["text"] for e in r.entries(kind=["frame"], ids=["5B3"], limit=2)] == ["5B3 03", "5B3 04"]
    assert r.entries(kind=["uds"]) == []


def test_an_entry_carries_both_clocks_the_bus_the_kind_and_the_id():
    r = con.Ring(ids=["421"])
    e = r.add("frame", "421 08 00 00", "421", bus="car")
    assert set(e) == {"seq", "t", "wall", "bus", "kind", "id", "text"}
    assert e["bus"] == "car" and e["kind"] == "frame" and e["id"] == "421"
    assert e["text"] == "421 08 00 00"
    assert e["wall"] > 1_700_000_000 and e["t"] != e["wall"], "monotonic and wall are two clocks"


def test_text_is_clipped_and_normalised():
    r = con.Ring(ids=["421"])
    e = r.add("event", "  a\n  very   long  " + "x" * 500)
    assert "\n" not in e["text"] and len(e["text"]) == con.TEXT_MAX


def test_an_unknown_kind_becomes_an_event_rather_than_a_new_kind():
    r = con.Ring()
    assert r.add("whatever", "hello")["kind"] == "event"


# ── the rolling file ──

def test_the_log_round_trips_entries_and_filters_like_the_ring(tmp_path):
    log = con.ConsoleLog(str(tmp_path / "console.jsonl"))
    r = con.Ring(ids=["421"], rate_cap=10_000)
    for i in range(4):
        r.add("frame", f"421 {i:02X}", "421")
    r.add("event", "reader: connected")
    log.append(r.drain())
    rows = log.read()
    assert [e["text"] for e in rows][-1] == "reader: connected"
    assert len(log.read(since=rows[0]["seq"])) == 4
    assert [e["kind"] for e in log.read(kind=["event"])] == ["event"]
    assert [e["text"] for e in log.read(ids=["421"], limit=2)] == ["421 02", "421 03"]


def test_the_log_is_size_capped_and_keeps_the_newest_half(tmp_path):
    path = str(tmp_path / "console.jsonl")
    log = con.ConsoleLog(path, max_bytes=4000)
    r = con.Ring(capacity=100_000, ids=["421"], rate_cap=10_000)
    for i in range(400):
        r.add("frame", f"421 {i:04X}", "421")
        if i % 50 == 0:
            log.append(r.drain())
    log.append(r.drain())
    assert os.path.getsize(path) <= 4000
    rows = log.read(limit=10_000)
    assert rows, "truncation keeps a tail, it does not empty the file"
    assert rows[-1]["text"] == "421 018F"
    assert [e["seq"] for e in rows] == sorted(e["seq"] for e in rows)


def test_a_torn_line_is_skipped_not_fatal(tmp_path):
    path = str(tmp_path / "console.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"seq": 1, "kind": "event", "id": "", "text": "ok"}) + "\n")
        f.write('{"seq": 2, "kind": "eve')          # the writer, mid-append
    assert [e["text"] for e in con.ConsoleLog(path).read()] == ["ok"]


def test_a_missing_file_reads_as_empty(tmp_path):
    assert con.ConsoleLog(str(tmp_path / "nope.jsonl")).read() == []


# ── the Console object ──

def test_arming_starts_a_new_window(tmp_path):
    path = str(tmp_path / "console.jsonl")
    first = con.Console(path, ids=["421"])
    first.add("frame", "421 01", "421")
    first.flush()
    assert con.ConsoleLog(path).read()
    second = con.Console(path, ids=["421"])       # the tile was turned off and on again
    assert con.ConsoleLog(path).read() == [], "a new arming does not show the last one's frames"
    assert second.stats()["on"] is True


def test_the_tap_never_raises_at_the_transport(tmp_path):
    c = con.Console(str(tmp_path / "console.jsonl"), ids=["421"])
    tap = c.tap(bus="car")
    tap("frame", "421 08 00 00", "421")
    tap("frame", None, object())                  # nonsense from a broken caller
    assert [e["text"] for e in c.ring.entries()] == ["421 08 00 00"]


def test_flush_writes_each_entry_once(tmp_path):
    path = str(tmp_path / "console.jsonl")
    c = con.Console(path, ids=["421"], rate_cap=10_000)
    c.add("frame", "421 01", "421")
    assert c.flush() == 1
    assert c.flush() == 0
    c.add("frame", "421 02", "421")
    assert c.flush() == 1
    assert [e["text"] for e in c.log.read()] == ["421 01", "421 02"]


def test_the_module_cannot_send_anything():
    """The read-only rule (SECURITY.md) applied to the debug pane: there is no
    transport handle in web/console.py at all."""
    src = open(os.path.join(ROOT, "web", "console.py")).read()
    for forbidden in ("send", "write_uds", "socket", "requests", "serial"):
        assert f"def {forbidden}" not in src
    assert "import" in src and "elm327" not in src and "cantransport" not in src


# ── the taps: what each transport can honestly give ──

class StubSource(ct.FrameSource):
    """Just enough of the FrameSource contract for the façade to exist."""
    name = "stub source 1"
    bus = "car"
    port = "mem"


def facade():
    return ct.CanFacade(StubSource(), settle=0)


def test_the_can_facade_taps_every_broadcast_frame(tmp_path):
    """Native CAN, MQTT and the simulated bus all reach the reader through this
    façade, so one tap covers all three."""
    c = con.Console(str(tmp_path / "c.jsonl"), ids=["421", "5B3"], rate_cap=1000)
    f = facade()
    f.tap = c.tap(bus="car")
    f._on_frame(time.time(), "421", b"\x08\x00\x00", {})
    f._on_frame(time.time(), "5b3", b"\x01\x02", {})
    rows = c.ring.entries()
    assert [(e["kind"], e["id"], e["text"], e["bus"]) for e in rows] == [
        ("frame", "421", "421 08 00 00", "car"),
        ("frame", "5B3", "5B3 01 02", "car")]


def test_frames_captured_for_a_uds_request_are_not_tapped_twice(tmp_path):
    """The reader emits one `uds` entry per request, grouped with the command
    that asked; printing the same 7BB bytes again as loose frames would be noise."""
    c = con.Console(str(tmp_path / "c.jsonl"), ids=["7BB"], rate_cap=1000)
    f = facade()
    f.tap = c.tap(bus="car")
    f._captures["7BB"] = []
    f._on_frame(time.time(), "7BB", b"\x10\x29\x61\x01", {})
    assert c.ring.entries() == []
    assert f._captures["7BB"] == ["7BB 10 29 61 01"], "the request still gets its frame"


def test_a_refusal_reaches_the_console_as_an_adapter_line(tmp_path):
    c = con.Console(str(tmp_path / "c.jsonl"), ids=[])
    f = facade()
    f.listen_only = True
    f.tap = c.tap(bus="ev")
    f.commands = 0
    import asyncio
    assert asyncio.run(f.send("2101")) == ["NO DATA"]
    rows = [e for e in c.ring.entries() if e["kind"] == "adapter"]
    assert rows and "listen-only" in rows[0]["text"]
    assert rows[0]["bus"] == "ev"


def test_the_source_going_offline_and_back_is_an_event(tmp_path):
    c = con.Console(str(tmp_path / "c.jsonl"), ids=[])
    f = facade()
    f.tap = c.tap(bus="car")
    f._on_status({"online": False, "error": "bridge gone"})
    f._on_status({"online": True})
    texts = [e["text"] for e in c.ring.entries() if e["kind"] == "event"]
    assert "bridge gone" in texts[0] and "back online" in texts[1]


def test_a_broken_tap_never_takes_the_transport_down():
    f = facade()

    def explode(*a, **kw):
        raise RuntimeError("console bug")

    f.tap = explode
    f._on_frame(time.time(), "421", b"\x08", {})      # must not raise
    assert f.tap is None, "the tap is dropped, the bus keeps running"
    assert f.frames == 1


def test_the_mqtt_source_taps_what_the_facade_never_sees(tmp_path):
    """A bridge message this source had to throw away is real information and
    reaches nothing else. Frames are not tapped here — they arrive through the
    façade, and tapping both would print every frame twice."""
    import mqttsource
    c = con.Console(str(tmp_path / "c.jsonl"), ids=["421"], rate_cap=1000)
    src = mqttsource.MqttSource(cfg={"host": "broker.invalid", "prefix": "hakake", "bus": "car"},
                                log=lambda *a: None)
    src.event_tap = c.tap(bus="car")
    seen = []
    src._on_frame = lambda *a: seen.append(a)

    class Msg:
        topic = "hakake/car/rx/421"
        payload = b"not json at all"

    src._on_message(None, None, Msg())
    rows = c.ring.entries()
    assert [e["kind"] for e in rows] == ["event"]
    assert "dropped message" in rows[0]["text"] and "hakake/car/rx/421" in rows[0]["text"]
    assert seen == [], "a message that did not parse is not a frame"


# ── the reader: armed by the tile, and only by the tile ──

@pytest.fixture
def armed(tmp_path, monkeypatch, leaf_profile):
    """A Reader with its files in tmp_path and a tiles.json the test writes."""
    import reader as rd
    from store import Store
    for attr, name in (("STATE_FILE", "state.json"), ("PAUSE_FILE", "reader.pause"),
                       ("TILES_FILE", "tiles.json"), ("CALIB_FILE", "calibration.json"),
                       ("CONSOLE_FILE", "console.jsonl")):
        monkeypatch.setattr(rd, attr, str(tmp_path / name))
    store = Store(str(tmp_path / "t.db"))

    def make(tiles):
        """`tiles` names the tiles to enable; every other default is written out
        disabled, because load_tiles() appends any default the file omits."""
        want = {t["id"]: t for t in tiles}
        out = []
        for d in rd.DEFAULT_TILES["tiles"]:
            out.append(dict(d, **want.pop(d["id"], {"enabled": False})))
        out.extend(want.values())
        with open(rd.TILES_FILE, "w") as f:
            json.dump({"tiles": out}, f)
        r = rd.Reader(0.1, "fake", store=store)
        r.refresh_items()
        return r

    yield make
    store.close()


class ElmLike:
    """An ELM327-shaped transport: no `tap` attribute, which is precisely what
    makes its view of the bus partial."""
    adapter_type = "ble"
    adapter_name = "ELM327 v1.5"
    adapter_port = "mem"

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sent = []

    async def send(self, cmd, wait=0, timeout=0):
        self.sent.append(cmd)
        return self.answers.get(cmd.strip().upper(), [])

    async def close(self):
        pass


def test_the_tap_stays_off_while_the_tile_is_disabled(armed):
    import reader as rd
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": False}])
    assert r.console is None
    f = facade()
    r.attach_console({"car": f})
    assert f.tap is None, "no tile, no tap"
    f._on_frame(time.time(), "421", b"\x08", {})
    assert not os.path.exists(rd.CONSOLE_FILE), "and no file either"


def test_enabling_the_tile_arms_the_ring_with_the_polled_ids(armed):
    r = armed([{"id": "vehicle", "enabled": True}, {"id": "console", "enabled": True}])
    assert r.console is not None
    ids = set(r.console.ring.ids)
    assert {"421", "358", "284", "60D", "5C5", "5B3", "5A9", "355"} == ids, \
        "the ids the enabled tiles poll, and no others"
    assert r.console.ring.everything is False
    f = facade()
    r.attach_console({"car": f})
    assert callable(f.tap)


def test_a_uds_tile_arms_the_ring_with_the_response_headers(armed):
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    assert set(r.console.ring.ids) == {"7BB"}, "a UDS item's id on the wire is its rx header"


def test_the_tile_options_choose_the_rules(armed):
    r = armed([{"id": "soc", "enabled": True},
               {"id": "console", "enabled": True, "opts": {"everything": True, "rate": 2}}])
    assert r.console.ring.everything is True and r.console.ring.rate_cap == 2
    stats = r.console.stats()
    assert stats["everything"] is True and stats["rate_cap"] == 2


def test_an_absurd_rate_is_clamped_not_obeyed(armed):
    r = armed([{"id": "soc", "enabled": True},
               {"id": "console", "enabled": True, "opts": {"rate": 10 ** 9}}])
    assert r.console.ring.rate_cap == rd_console_max()
    r2 = armed([{"id": "soc", "enabled": True},
                {"id": "console", "enabled": True, "opts": {"rate": "nonsense"}}])
    assert r2.console.ring.rate_cap == con.RATE_CAP


def rd_console_max():
    import reader as rd
    return rd.CONSOLE_RATE_MAX


def test_turning_the_tile_off_drops_the_object_and_the_taps(armed, tmp_path, monkeypatch):
    import reader as rd
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    f = facade()
    r.transports = {"car": f}
    r.attach_console(r.transports)
    assert callable(f.tap)
    with open(rd.TILES_FILE, "w") as fh:
        json.dump({"tiles": [{"id": "soc", "enabled": True}, {"id": "console", "enabled": False}]}, fh)
    r._tiles_mtime = None                      # the mtime path, forced for the test
    r.refresh_items()
    assert r.console is None and f.tap is None


# ── what each transport puts in it, through the reader ──

def test_an_elm327_contributes_its_polled_frames_and_says_the_view_is_partial(armed):
    import reader as rd
    r = armed([{"id": "vehicle", "enabled": True}, {"id": "console", "enabled": True}])
    elm = ElmLike()
    r.attach_console({"car": elm})
    assert r.console.partial and "ATMA dwell" in r.console.partial
    r.console_item("car", elm, "p421", rd.ITEMS["p421"], ["421 08 00 00", "421 09 00 00"])
    rows = [e for e in r.console.ring.entries() if e["kind"] == "frame"]
    assert [(e["kind"], e["id"], e["text"]) for e in rows] == [
        ("frame", "421", "421 08 00 00"), ("frame", "421", "421 09 00 00")]
    assert r.console.stats()["partial"] == rd.CONSOLE_PARTIAL_ELM


def test_a_tapped_transport_does_not_repeat_its_frames_through_the_poller(armed):
    """Behind the façade every frame already went in as it arrived; replaying
    the ATMA answer would double each one. And there the view is not partial."""
    import reader as rd
    r = armed([{"id": "vehicle", "enabled": True}, {"id": "console", "enabled": True}])
    f = facade()
    r.attach_console({"car": f})
    assert r.console.partial == ""
    assert "partial" not in r.console.stats()
    r.console_item("car", f, "p421", rd.ITEMS["p421"], ["421 08 00 00"])
    assert [e for e in r.console.ring.entries() if e["kind"] == "frame"] == []


def test_a_silent_dwell_is_reported_rather_than_left_blank(armed):
    import reader as rd
    r = armed([{"id": "vehicle", "enabled": True}, {"id": "console", "enabled": True}])
    r.console_item("car", ElmLike(), "p385", rd.ITEMS["p385"], [])
    e = r.console.ring.entries()[-1]
    assert e["kind"] == "adapter" and e["id"] == "385" and "nothing in a" in e["text"]


def test_a_uds_answer_is_grouped_with_the_request_that_asked_for_it(armed):
    import reader as rd
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    r.console_item("car", ElmLike(), "lbc01", rd.ITEMS["lbc01"],
                   ["7BB 10 29 61 01", "7BB 21 00 00 00"])
    e = r.console.ring.entries()[-1]
    assert e["kind"] == "uds" and e["id"] == "7BB"
    assert e["text"] == "2101 -> 7BB 10 29 61 01 / 7BB 21 00 00 00"


def test_an_answer_that_is_not_an_answer_is_what_the_adapter_said(armed):
    import reader as rd
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    for lines, expect in ((["NO DATA"], "2101 -> NO DATA"), (["?"], "2101 -> ?"), ([], "2101 -> no answer")):
        r.console_item("car", ElmLike(), "lbc01", rd.ITEMS["lbc01"], lines)
        e = r.console.ring.entries()[-1]
        assert e["kind"] == "adapter" and e["text"] == expect


def test_a_text_signal_appears_when_it_changes_and_not_again(armed, use_vehicle):
    """The Lancer's stored codes are the case that makes the console worth
    having on a car that is not the Leaf."""
    import reader as rd
    use_vehicle("lancer_2009")
    r = armed([{"id": "console", "enabled": True}])
    r.cache["dtc_stored"] = "P0420"
    r.console_text()
    r.console_text()
    rows = [e for e in r.console.ring.entries() if e["kind"] == "text"]
    assert [e["text"] for e in rows] == ["Engine codes: P0420"]
    r.cache["dtc_stored"] = "P0420, P0171"
    r.console_text()
    assert [e["text"] for e in r.console.ring.entries() if e["kind"] == "text"][-1] == \
        "Engine codes: P0420, P0171"
    use_vehicle("leaf_ze0")


def test_a_reader_event_carries_the_bus(armed):
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    r.console_event("connected: FakeELM via fake")
    e = r.console.ring.entries()[-1]
    assert e["kind"] == "event" and e["bus"] == "car" and e["id"] == ""


def test_arming_itself_is_an_event_so_the_pane_is_never_blank(armed):
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    first = r.console.ring.entries()[0]
    assert first["kind"] == "event" and "console armed" in first["text"]


def test_the_record_carries_the_console_stats_or_says_it_is_off(armed):
    import asyncio
    r = armed([{"id": "console", "enabled": False}])
    rec, _ = asyncio.run(r.poll_once(ElmLike()))
    assert rec["console"] == {"on": False}

    r2 = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    rec2, _ = asyncio.run(r2.poll_once(ElmLike({"2101": ["NO DATA"]})))
    assert rec2["console"]["on"] is True
    assert rec2["console"]["cursor"] != "0" and "dropped" in rec2["console"]
    assert rec2["console"]["partial"]


def test_a_cycle_flushes_the_ring_to_the_file(armed):
    import asyncio
    import reader as rd
    r = armed([{"id": "soc", "enabled": True}, {"id": "console", "enabled": True}])
    asyncio.run(r.poll_once(ElmLike({"2101": ["7BB 10 29 61 01"]})))
    rows = con.ConsoleLog(rd.CONSOLE_FILE).read()
    assert any(e["kind"] == "uds" for e in rows), [e["text"] for e in rows]


# ── the endpoint ──

@pytest.fixture
def api(tmp_path, monkeypatch, leaf_profile):
    """The Flask test client with every file under tmp_path, plus a writer that
    fills the console file the way the reader would."""
    import app as webapp
    import reader as rd
    monkeypatch.setattr(webapp, "DEMO", None)
    monkeypatch.setattr(webapp, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(rd, "CONSOLE_FILE", str(tmp_path / "console.jsonl"))
    webapp.app.config["TESTING"] = True

    def write(entries=(), stats=None):
        c = con.Console(rd.CONSOLE_FILE, ids=["421", "5B3", "7BB"], rate_cap=10_000)
        for kind, cid, text in entries:
            c.add(kind, text, cid=cid, bus="car")
        c.flush()
        with open(webapp.STATE_FILE, "w") as f:
            json.dump({"status": "ok", "console": stats if stats is not None else c.stats()}, f)
        return c

    with webapp.app.test_client() as client:
        yield client, write


ROWS = [("frame", "421", "421 08 00 00"), ("frame", "5B3", "5B3 01 02"),
        ("uds", "7BB", "2101 -> 7BB 10 29 61 01"), ("event", "", "connected: ELM327 v1.5"),
        ("frame", "421", "421 09 00 00")]


def test_the_endpoint_serves_the_window_and_a_cursor(api):
    client, write = api
    write(ROWS)
    body = client.get("/api/console").get_json()
    assert [e["text"] for e in body["entries"]] == [r[2] for r in ROWS]
    assert body["cursor"] == str(body["entries"][-1]["seq"])
    assert body["kinds"] == list(con.KINDS)
    assert body["stats"]["on"] is True


def test_the_cursor_asks_only_for_what_is_new(api):
    """How the tile polls: take what is there, then keep asking from the cursor
    it was handed. A limit always takes the *newest* matches — a pane that fell
    behind wants the end of the stream, not the start of a backlog."""
    client, write = api
    c = write(ROWS)
    first = client.get("/api/console?limit=2").get_json()
    assert [e["text"] for e in first["entries"]] == ["connected: ELM327 v1.5", "421 09 00 00"]

    idle = client.get(f"/api/console?since={first['cursor']}").get_json()
    assert idle["entries"] == [] and idle["cursor"] == first["cursor"], \
        "nothing new keeps the cursor where it was"

    c.add("frame", "421 0A 00 00", cid="421", bus="car")
    c.flush()
    fresh = client.get(f"/api/console?since={first['cursor']}").get_json()
    assert [e["text"] for e in fresh["entries"]] == ["421 0A 00 00"]
    assert int(fresh["cursor"]) == int(first["cursor"]) + 1
    assert client.get("/api/console?since=0").get_json()["entries"][0]["text"] == "421 08 00 00"


def test_the_endpoint_filters_by_kind_and_id(api):
    client, write = api
    write(ROWS)
    assert [e["text"] for e in client.get("/api/console?kind=frame").get_json()["entries"]] == \
        ["421 08 00 00", "5B3 01 02", "421 09 00 00"]
    assert [e["text"] for e in client.get("/api/console?ids=421").get_json()["entries"]] == \
        ["421 08 00 00", "421 09 00 00"]
    assert [e["text"] for e in client.get("/api/console?kind=uds,event").get_json()["entries"]] == \
        ["2101 -> 7BB 10 29 61 01", "connected: ELM327 v1.5"]
    assert client.get("/api/console?ids=999").get_json()["entries"] == []
    # a kind nobody defined is ignored rather than obeyed
    assert len(client.get("/api/console?kind=nonsense").get_json()["entries"]) == len(ROWS)


def test_the_limit_takes_the_newest_and_is_capped(api):
    client, write = api
    write(ROWS)
    assert [e["text"] for e in client.get("/api/console?limit=2").get_json()["entries"]] == \
        ["connected: ELM327 v1.5", "421 09 00 00"]
    body = client.get(f"/api/console?limit={con.LIMIT_MAX * 100}").get_json()
    assert len(body["entries"]) == len(ROWS)        # capped, not refused


def test_the_stats_say_what_was_thrown_away(api):
    client, write = api
    write(ROWS, stats={"on": True, "kept": 5, "dropped": 1200, "dropped_by_id": {"1DB": 1200},
                       "partial": "ELM327: …", "everything": False, "rate_cap": 10})
    stats = client.get("/api/console").get_json()["stats"]
    assert stats["dropped"] == 1200 and stats["dropped_by_id"] == {"1DB": 1200}
    assert stats["partial"].startswith("ELM327")


def test_a_console_that_was_never_armed_is_an_empty_window_not_an_error(api):
    client, _ = api
    body = client.get("/api/console").get_json()
    assert body["entries"] == [] and body["stats"] == {"on": False}
    assert client.get("/api/console").status_code == 200


def test_demo_mode_never_opens_the_file(api, monkeypatch):
    import app as webapp
    client, write = api
    write(ROWS)
    monkeypatch.setattr(webapp, "DEMO", "docs/demo")
    body = client.get("/api/console").get_json()
    assert body["entries"] == [] and body["stats"] == {"on": False, "demo": True}


def test_the_endpoint_is_read_only(api):
    client, _ = api
    for method in ("post", "put", "delete"):
        assert getattr(client, method)("/api/console").status_code == 405


# ── the tile: markup, page wiring and the pure half of console.js ──

import shutil  # noqa: E402
import subprocess  # noqa: E402

TEMPLATES = os.path.join(ROOT, "web", "templates")
CONSOLE_JS = os.path.join(ROOT, "web", "static", "console.js")
CONSOLE_HTML = os.path.join(TEMPLATES, "tiles", "console.html")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_tile_is_a_partial_included_once_with_its_script():
    index = read(os.path.join(TEMPLATES, "index.html"))
    assert os.path.exists(CONSOLE_HTML)
    assert index.count('{% include "tiles/console.html" %}') == 1
    assert index.count('/static/console.js') == 1
    assert index.count("TileStudio.menuExtra('console', consoleMenu)") == 1


def test_the_tile_markup_carries_every_control_the_plan_asked_for():
    html = read(CONSOLE_HTML)
    assert 'data-tile="console"' in html and 'data-span="12"' in html
    assert "Raw output" in html
    for anchor in ("console-out", "console-pause", "console-kinds", "console-ids",
                   "console-known", "console-stats", "console-partial"):
        assert f'id="{anchor}"' in html, anchor
    assert "known ids only" in html
    assert "ID B0 B1" in html, "the copy format is stated where a person can see it"


def test_the_rendered_page_carries_the_tile_once(tmp_path, monkeypatch, leaf_profile):
    import app as webapp
    import reader as rd
    from store import Store
    monkeypatch.setattr(webapp, "DEMO", None)
    monkeypatch.setattr(webapp, "STATE_FILE", str(tmp_path / "state.json"))
    for attr, name in (("STATE_FILE", "state.json"), ("TILES_FILE", "tiles.json"),
                       ("CALIB_FILE", "calibration.json"), ("LAYOUTS_FILE", "layouts.json")):
        monkeypatch.setattr(rd, attr, str(tmp_path / name))
    store = Store(str(tmp_path / "p.db"))
    monkeypatch.setattr(webapp, "store", lambda: store)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        page = c.get("/").get_data(as_text=True)
    store.close()
    assert page.count('data-tile="console"') == 1
    assert read(CONSOLE_HTML).rstrip("\n") in page, "the include must not reshape the markup"


def test_the_tile_is_offered_by_the_layout_api_for_every_profile(tmp_path, monkeypatch, use_vehicle):
    import app as webapp
    import reader as rd
    monkeypatch.setattr(webapp, "DEMO", None)
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    webapp.app.config["TESTING"] = True
    for name in ("leaf_ze0", "lancer_2009"):
        use_vehicle(name)
        with webapp.app.test_client() as c:
            tiles = {t["id"]: t for t in c.get("/api/tiles").get_json()["tiles"]}
        assert tiles["console"]["name"] == "Raw output"
        assert tiles["console"]["enabled"] is False
        assert tiles["console"]["span"] == 12
        assert tiles["console"]["items"] == []


def test_the_signal_registry_carries_each_item_s_id_on_the_wire(tmp_path, monkeypatch, leaf_profile):
    """What the tile's "known ids only" toggle is built from — the profile's own
    list, so nothing in the browser knows what a Leaf is."""
    import app as webapp
    monkeypatch.setattr(webapp, "DEMO", None)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        items = c.get("/api/signals").get_json()["items"]
    assert items["p421"]["can_id"] == "421"           # a passive item: its own id
    assert items["lbc01"]["can_id"] == "7BB"          # a UDS item: its response header
    assert all("can_id" in v for v in items.values())


def test_console_js_keeps_the_pane_read_only():
    js = read(CONSOLE_JS)
    assert "method:" not in js and "PUT" not in js and "POST" not in js
    assert js.count("fetch(") == 2, "one poll and one registry read, both GET"


def test_a_multi_row_selection_copies_one_line_per_row():
    """A row is laid out with flexbox, and a browser's clipboard serialiser puts
    every flex child on its own line — which turned a three-row selection into
    nine lines of time / kind / bytes. The pane builds the text itself."""
    out = run_node(HARNESS + """
      const rows = [
        {wall: 1789141190.5, kind: 'frame', text: '284 00 00 00 00 00 00 9A 20'},
        {wall: 1789141190.5, kind: 'frame', text: '292 7E C8 28 80 20 00 00 00'},
      ];
      console.log(JSON.stringify({one: R.rowText(rows[0]), many: R.rowsText(rows),
                                  none: R.rowsText([])}));""")
    assert out["many"].count("\n") == 1, "one line per row, not one per column"
    assert out["many"].split("\n")[1].endswith("292 7E C8 28 80 20 00 00 00")
    assert "  frame  " in out["one"]
    assert out["none"] == ""


def test_the_pane_can_be_cleared_without_touching_the_reader():
    html = read(CONSOLE_HTML)
    js = read(CONSOLE_JS)
    assert 'id="console-clear"' in html and ">Clear<" in html
    assert "el.clear.addEventListener" in js
    assert "rows = [];" in js
    # the cursor is deliberately not reset: cleared lines must not come back
    assert "state.cursor = 0" not in js.split("el.clear.addEventListener")[1].split("});")[0]


def test_click_to_copy_never_eats_a_hand_made_selection():
    """A drag-select ends in a click; copying the whole line then would throw
    away the selection the person just made."""
    js = read(CONSOLE_JS)
    assert "isCollapsed" in js and "getSelection" in js


def test_console_js_says_what_the_pane_is_not():
    js = read(CONSOLE_JS)
    assert "record_session.py" in js and "not a capture tool" in js


def run_node(script):
    r = subprocess.run(["node", "-e", script, CONSOLE_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


HARNESS = "globalThis.window = globalThis; const R = require(process.argv[1]);"


@needs_node
def test_the_changed_byte_mask_is_what_a_person_hunts_a_signal_with():
    out = run_node(HARNESS + """
      console.log(JSON.stringify({
        changed: R.diffMask('421 08 00 00', '421 09 00 00'),
        same: R.diffMask('421 08 00 00', '421 08 00 00'),
        first: R.diffMask('', '421 08 00 00'),
        grew: R.diffMask('421 08', '421 08 00'),
        tokens: R.tokens('  421   08 00 '),
      }));""")
    assert out["changed"] == [True, False, False]
    assert out["same"] == [False, False, False]
    assert out["first"] == [False, False, False], "a first sighting highlights nothing"
    assert out["grew"] == [False, True]
    assert out["tokens"] == ["421", "08", "00"]


@needs_node
def test_the_tile_asks_with_a_cursor_and_its_kind_filter():
    out = run_node(HARNESS + """
      const all = {frame:true, uds:true, adapter:true, text:true, event:true};
      console.log(JSON.stringify({
        fresh: R.query({cursor: 0, limit: 300, kinds: all, idFilter: ''}),
        walk: R.query({cursor: '4120', limit: 50, kinds: {frame:true, uds:false}, idFilter: ''}),
        ids: R.query({cursor: 0, kinds: all, idFilter: ' 421, 5b3 '}),
        idList: R.idList('421, 5b3  7bb'),
      }));""")
    assert out["fresh"] == "/api/console?since=0&limit=300"
    assert out["walk"] == "/api/console?since=4120&limit=50&kind=frame"
    assert out["ids"].endswith("ids=421,5B3")
    assert out["idList"] == ["421", "5B3", "7BB"]


@needs_node
def test_the_stats_line_says_what_was_dropped_and_never_hides_a_lossy_mode():
    out = run_node(HARNESS + """
      console.log(JSON.stringify({
        off: R.statsLine({on: false}),
        quiet: R.statsLine({on: true, kept: 40, dropped: 0, rate_cap: 10}),
        busy: R.statsLine({on: true, kept: 120, dropped: 5201, dropped_by_id: {'1DB': 3200}, rate_cap: 10}),
        lossy: R.statsLine({on: true, kept: 9, dropped: 2, everything: true}),
        copy: R.copyText({text: '421 08 00 00'}),
      }));""")
    assert out["off"] == "off"
    assert out["quiet"] == "40 shown — max 10/s per id"
    assert "5,201 dropped (1DB 3,200)" in out["busy"]
    assert "every id — lossy" in out["lossy"]
    assert out["copy"] == "421 08 00 00"
