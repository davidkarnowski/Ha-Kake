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
