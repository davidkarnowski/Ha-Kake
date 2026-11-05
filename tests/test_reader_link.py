# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The reader accounting for its link: the whole stack below the page.

A real ``Reader`` polls through the real façade over a real ``MqttSource``;
only the broker is fake, and behind it a small bridge that can go quiet on a
request and answer it later, the way a stalled link does. Frames are the
car's own (tests/fixtures). What is held to:

  * a reply is only ever decoded for the item that asked for it;
  * a reply that comes late is credited to that item and marked `late`,
    unless the item has been read again since: an old value never replaces a
    newer one, and nothing is dropped either way (the ledger has it);
  * a reply nobody here asked for is recorded as foreign and decoded for no item;
  * a value outside its physical limits is stored, marked and ledgered;
  * a request that got no answer is not stamped as freshly acquired;
  * with a bridge that returns replies in its acks, every value carries the
    request that produced it and where its time went.
"""

import asyncio
import json
import os

import pytest

from conftest import ROOT, FIXTURES  # noqa: F401  (sys.path is set up there)

import linkaccount as la  # noqa: E402
import mqttsource as ms  # noqa: E402
import replyproof as rp  # noqa: E402
import vehicles  # noqa: E402
from test_mqtt_source import FakeClient, CFG, ROOT_T  # noqa: E402

pytest.importorskip("cantransport")

with open(os.path.join(FIXTURES, "session_leaf_ze0.json")) as _f:
    LBC = json.load(_f)["frames"][0]["uds"]["79B"]
SOC, INSULATION = 76.87, 885                    # what the car said in those captures


def payload(lines):
    (m,) = rp.messages(lines, "7BB")
    return m.payload


def padding(lines):
    data = [bytes(int(b, 16) for b in line.split()[1:]) for line in lines]
    carried = (data[0][2:] if len(lines) > 1 else data[0][1:]) + b"".join(d[1:] for d in data[1:])
    return carried[len(payload(lines)):]


def altered(lines, offset, new):
    """The same reply with some bytes changed (offsets count from after the echo), re-framed as the car frames it."""
    p = bytearray(payload(lines))
    p[2 + offset:2 + offset + len(new)] = new
    return rp.lines_from_payload("7BB", bytes(p), padding(lines))


class FakeBridge:
    """Answers each request from `replies` ({cmd: lines}). A cmd in `quiet` is
    held instead, as a stalled link would hold it; held requests are answered,
    in the order they came, just before the next request that is not held.
    `ack_reply` makes it a bridge that returns the reply in its ack."""

    def __init__(self, client, replies, ack_reply=False):
        self.client, self.replies, self.ack_reply = client, dict(replies), ack_reply
        self.quiet, self.held, self.before = set(), [], {}
        self.seen = 0
        self.t = 5000.0
        self.forget = False                 # True: a held request is never answered at all

    def requests(self):
        return [json.loads(p) for t, p, q, r in self.client.published if t == f"{ROOT_T}/tx/uds"]

    async def run(self):
        while True:
            reqs = self.requests()
            while self.seen < len(reqs):
                req = reqs[self.seen]
                self.seen += 1
                cmd = req["data"].replace(" ", "")
                if cmd in self.quiet:
                    self.held.append(req)
                    continue
                held, self.held = self.held, []
                for old in ([] if self.forget else held):
                    self.answer(old, queue_ms=11000)
                for lines in self.before.pop(cmd, []):                  # somebody else's reply, ahead of ours
                    self.frames(lines)
                self.answer(req)
            await asyncio.sleep(0.002)

    def frames(self, lines):
        for line in lines:
            self.t += 0.005
            cid, _, d = line.partition(" ")
            self.client.deliver(f"{ROOT_T}/rx/{cid}", {"t": self.t, "id": cid, "d": d})

    def answer(self, req, queue_ms=2):
        lines = self.replies[req["data"].replace(" ", "")]
        self.frames(lines)
        ack = {"req": req["req"], "ok": True, "frames": len(lines)}
        if self.ack_reply:
            p, pad = payload(lines), padding(lines)
            ack.update({"bytes": len(p), "reply": rp.hex_bytes(p), "request": req["data"], "tx": req["tx"],
                        "rx": req["rx"], "src": req["src"], "seq": req["seq"], "t_in": self.t - 0.21,
                        "t_done": round(self.t, 3), "queue_ms": queue_ms, "bus_ms": 201})
            if pad:
                ack["pad"] = rp.hex_bytes(pad)
        self.client.deliver(f"{ROOT_T}/tx/uds/{req['req']}", ack)


@pytest.fixture
def rig(isolated_reader, tmp_store, tmp_path, monkeypatch):
    """A Reader that polls the battery state every cycle and the extended
    state on its period, and gives up on a request after 50 ms."""
    rd = isolated_reader
    rd.set_vehicle("leaf_ze0")
    leaf = vehicles.get_vehicle("leaf_ze0")
    with open(tmp_path / "tiles.json", "w") as f:
        json.dump({"tiles": [dict(t, enabled=t["id"] == "power") for t in vehicles.default_tiles(leaf)]}, f)
    monkeypatch.setattr(ms, "ACK_GRACE", 0.0)
    for item in ("lbc01", "lbc05"):
        monkeypatch.setitem(rd.ITEMS[item], "timeout", 0.05)
    return rd, tmp_store


def ledger(rd):
    return la.tail(rd.LEDGER_FILE, 50)


def play(rig, script, replies=None, ack_reply=False):
    """Open the stack and hand `script(reader, bridge, cycle)` a coroutine that
    runs one poll cycle and returns its record."""
    rd, store = rig

    async def go():
        clients = []

        def factory(cid):
            clients.append(FakeClient())
            return clients[-1]

        elm = await ms.open_mqtt(log=lambda *a: None, cfg=CFG, client_factory=factory)
        bridge = FakeBridge(clients[0], replies or {"2101": LBC["2101"], "2105": LBC["2105"]}, ack_reply)
        if ack_reply:
            clients[0].deliver(f"{ROOT_T}/status", {"v": 1, "online": True, "bus": "car",
                                                    "caps": ["ack-reply", "supersede"]})
        runner = asyncio.ensure_future(bridge.run())
        reader = rd.Reader(interval=0, adapter_pref="mqtt", store=store)

        async def cycle(due=()):
            for item in due:
                reader.item_last.pop(item, None)
            rec, alive = await reader.poll_once(elm)
            return rec

        try:
            return await script(reader, bridge, cycle)
        finally:
            runner.cancel()
            await elm.close()

    return asyncio.run(go())


# ── a reply that comes late ──────────────────────────────────────────────

def test_a_late_reply_is_credited_to_the_item_that_asked_for_it_and_marked(rig):
    """2026-11-05, end to end, with a bridge that does not put replies in its acks."""
    rd, store = rig
    old_g05 = altered(LBC["2105"], 24, b"\x00\x6F")                      # the held reply says insulation 111

    async def script(reader, bridge, cycle):
        first = await cycle()
        bridge.quiet.add("2105")
        bridge.replies["2105"] = old_g05
        gave_up = await cycle(due=["lbc05"])
        bridge.quiet.clear()
        late = await cycle()                                            # only the battery state is asked
        bridge.replies["2105"] = LBC["2105"]
        again = await cycle(due=["lbc05"])
        return first, gave_up, late, again

    first, gave_up, late, again = play(rig, script)

    assert first["soc"] == SOC and first["g05_insulation_kohm"] == INSULATION
    assert first["link"]["via"] == "frames" and "quality" not in first
    assert first["link"]["items"]["lbc01"]["state"] == first["link"]["items"]["lbc05"]["state"] == "ok"

    # the request was given up on: nothing was acquired, and nothing says it was
    assert gave_up["link"]["items"]["lbc05"]["state"] == "timeout" and gave_up["link"]["cycle"]["timeouts"] == 1
    assert gave_up["item_ts_epoch"]["lbc05"] == first["item_ts_epoch"]["lbc05"]
    assert gave_up["item_ts_epoch"]["lbc01"] > first["item_ts_epoch"]["lbc01"]
    assert gave_up["g05_insulation_kohm"] == INSULATION and gave_up["soc"] == SOC

    # its reply arrives inside the next request's window: the battery state is
    # still the battery state, and the late reply is the extended state's
    assert late["soc"] == SOC and late["pack_v"] == first["pack_v"] and late["lv_volts"] == first["lv_volts"]
    assert late["g05_insulation_kohm"] == 111
    assert late["quality"]["g05_insulation_kohm"] == "late" and "soc" not in late["quality"]
    assert late["link"]["items"]["lbc01"]["state"] == "ok"
    assert late["link"]["items"]["lbc05"]["state"] == "late" and late["link"]["items"]["lbc05"]["late_s"] >= 0
    assert late["link"]["cycle"]["late"] == late["link"]["cycle"]["late_credited"] == 1
    assert late["link"]["last_stall"]["item"] == "lbc05"

    # read again in time: the mark goes
    assert again["g05_insulation_kohm"] == INSULATION and "quality" not in again
    assert again["link"]["totals"]["late_credited"] == 1 and again["link"]["cycle"].get("late") is None

    (line,) = [e for e in ledger(rd) if e["class"] == "late"]
    assert line["item"] == "lbc05" and line["request"] == "21 05" and line["reply"].startswith("61 05")
    assert "credited to lbc05" in line["action"] and line["via"] == "frames"


def test_a_late_reply_never_replaces_a_newer_reading(rig):
    rd, store = rig
    old_g05 = altered(LBC["2105"], 24, b"\x00\x6F")

    async def script(reader, bridge, cycle):
        await cycle()
        bridge.quiet.add("2105")
        bridge.replies["2105"] = old_g05
        await cycle(due=["lbc05"])                                      # given up on; the bridge still holds it
        held, bridge.held = bridge.held, []
        bridge.quiet.clear()
        bridge.replies["2105"] = LBC["2105"]
        fresh = await cycle(due=["lbc05"])                              # asked again, answered in time
        bridge.replies["2105"] = old_g05
        bridge.held = held                                              # and now the old reply turns up
        after = await cycle()
        return fresh, after

    fresh, after = play(rig, script)
    assert fresh["g05_insulation_kohm"] == INSULATION
    assert after["g05_insulation_kohm"] == INSULATION and "quality" not in after       # the newer reading stands
    assert after["link"]["cycle"]["late"] == 1 and "late_credited" not in after["link"]["cycle"]
    (line,) = [e for e in ledger(rd) if e["class"] == "late"]
    assert line["action"] == "kept here only: a newer reading of lbc05 was already in"
    assert line["reply"].startswith("61 05")                            # and its bytes are kept all the same


def test_a_request_that_is_never_answered_leaves_the_last_reading_and_its_time_alone(rig):
    async def script(reader, bridge, cycle):
        first = await cycle()
        bridge.quiet.add("2105")
        bridge.forget = True
        second = await cycle(due=["lbc05"])
        third = await cycle(due=["lbc05"])
        return first, second, third

    first, second, third = play(rig, script)
    assert third["item_ts_epoch"]["lbc05"] == first["item_ts_epoch"]["lbc05"]
    assert third["g05_insulation_kohm"] == INSULATION and "quality" not in third
    assert third["link"]["totals"]["timeouts"] == 2 and third["link"]["totals"].get("late") is None


# ── a reply nobody here asked for ────────────────────────────────────────

def test_another_requesters_reply_is_recorded_as_foreign_and_decoded_for_nothing(rig):
    rd, store = rig

    async def script(reader, bridge, cycle):
        first = await cycle()
        bridge.before["2101"] = [LBC["2102"]]                           # someone else read the cell voltages
        return first, await cycle()

    first, second = play(rig, script)
    assert second["soc"] == SOC and second["pack_v"] == first["pack_v"] and "quality" not in second
    assert "cells" not in second and "cell_min" not in second          # nobody here asked for cells
    assert second["link"]["cycle"]["foreign"] == 1 and second["link"]["items"]["lbc01"]["state"] == "ok"
    (line,) = [e for e in ledger(rd) if e["class"] == "foreign"]
    assert line["item"] == "lbc02" and line["reply"].startswith("61 02") and line["frames"] == len(LBC["2102"])


# ── a value no car can produce ───────────────────────────────────────────

def test_an_impossible_value_is_stored_marked_and_ledgered_and_the_mark_goes_with_the_next_good_read(rig):
    rd, store = rig
    wrong = altered(LBC["2101"], 29, b"\x40\x00\x40")                    # a well-formed group 01 that says 419.44 %

    async def script(reader, bridge, cycle):
        await cycle()
        bridge.replies["2101"] = wrong
        marked = await cycle()
        peaks = dict(reader._peaks)
        bridge.replies["2101"] = LBC["2101"]
        return marked, peaks, await cycle()

    marked, peaks, cleared = play(rig, script)
    assert marked["soc"] == 419.44 and marked["quality"] == {"soc": "out-of-limits"}     # kept, and marked
    assert marked["pack_v"] == cleared["pack_v"]                         # the rest of that reply is fine and unmarked
    assert marked["link"]["cycle"]["flagged"] == 1
    assert cleared["soc"] == SOC and "quality" not in cleared
    (line,) = [e for e in ledger(rd) if e["class"] == "out-of-limits"]
    assert line["key"] == "soc" and line["value"] == 419.44 and line["limits"] == [0.0, 100.0]
    assert line["item"] == "lbc01" and line["frames"] == wrong
    assert all(p["max"] < 1000 for k, p in peaks.items())               # the envelope between rows is not polluted


def test_the_profiles_limits_name_signals_it_has():
    leaf = vehicles.get_vehicle("leaf_ze0")
    assert set(leaf.LIMITS) <= set(leaf.SIGNALS) | {"cell_min", "cell_max", "pack_v", "current_a", "temp_avg_c"}
    assert all(lo < hi for lo, hi in leaf.LIMITS.values())
    assert leaf.LIMITS["soc"] == (0.0, 100.0)


# ── a bridge that returns the reply in its ack ───────────────────────────

def test_with_replies_in_the_acks_every_value_carries_its_request_and_where_its_time_went(rig):
    async def script(reader, bridge, cycle):
        return await cycle(), reader

    rec, reader = play(rig, script, ack_reply=True)
    assert rec["soc"] == SOC and rec["g05_insulation_kohm"] == INSULATION      # the same numbers as from the frames
    link = rec["link"]
    assert link["via"] == "ack" and link["caps"] == ["ack-reply", "supersede"]
    e = link["items"]["lbc01"]
    assert e["state"] == "ok" and e["via"] == "ack" and e["req"].endswith("-000001")
    assert (e["queue_ms"], e["bus_ms"]) == (2, 201) and e["net_ms"] == round(max(0.0, e["rtt_ms"] - 203), 1)
    assert e["t_bus"] == rec["frame_ts"]["lbc01"] > 5000              # the bridge's clock, kept beside ours
    assert rec["item_ts_epoch"]["lbc01"] > 1_000_000_000               # our own clock is still our own
    assert link["cycle"] == {"ok": 2, "sent": 2}


def test_raw_fields_are_the_same_whichever_way_the_reply_came(rig):
    """The ack's reply and padding rebuild the car's frames, so even the fields that keep raw bytes agree."""
    async def script(reader, bridge, cycle):
        return await cycle()

    replies = {"2101": LBC["2101"], "2105": LBC["2105"]}
    by_frames = play(rig, script, replies)
    by_ack = play(rig, script, replies, ack_reply=True)
    skip = {"link", "item_ts", "item_ts_epoch", "frame_ts", "timing", "item_age", "item_dur", "item_gap", "clock_offset_s"}
    assert {k: v for k, v in by_ack.items() if k not in skip} == {k: v for k, v in by_frames.items() if k not in skip}


def test_a_late_ack_brings_its_reply_and_it_is_credited_by_identity(rig):
    rd, store = rig

    async def script(reader, bridge, cycle):
        await cycle()
        bridge.quiet.add("2105")
        bridge.replies["2105"] = altered(LBC["2105"], 24, b"\x00\x6F")
        await cycle(due=["lbc05"])
        bridge.quiet.clear()
        return await cycle()

    late = play(rig, script, ack_reply=True)
    assert late["soc"] == SOC and late["g05_insulation_kohm"] == 111 and late["quality"]["g05_insulation_kohm"] == "late"
    assert late["link"]["items"]["lbc05"]["state"] == "late" and late["link"]["cycle"]["late_credited"] == 1
    assert late["link"]["cycle"].get("foreign") is None                # the mirrored frames are not judged twice
    (line,) = [e for e in ledger(rd) if e["class"] == "late"]
    assert line["via"] == "ack" and line["t_src"] > 5000 and line["ack"]["queue_ms"] == 11000
    assert line["ack"]["request"] == "21 05" and "credited to lbc05" in line["action"]


def test_an_adapter_that_pairs_requests_itself_has_no_link_account(isolated_reader, tmp_store):
    """An ELM327 prints what answered before its prompt: there is nothing to prove, so no `link` block."""
    rd = isolated_reader
    rd.set_vehicle("leaf_ze0")

    class Elm:
        adapter_type, adapter_name, adapter_port = "fake", "FakeELM 1", "mem"

        async def send(self, cmd, wait=0, timeout=0):
            return LBC.get(cmd.upper(), [])

    reader = rd.Reader(interval=0, adapter_pref=None, store=tmp_store)
    rec, alive = asyncio.run(reader.poll_once(Elm()))
    assert rec["soc"] == SOC and "link" not in rec and "quality" not in rec


# ── the ledger over HTTP ─────────────────────────────────────────────────

def test_the_ledger_is_served_newest_last_and_filters_by_class(isolated_reader):
    rd = isolated_reader
    import app as webapp
    client = webapp.app.test_client()
    assert client.get("/api/ledger").get_json() == {"entries": []}        # no file: an empty list, not an error
    book = la.Ledger(rd.LEDGER_FILE)
    for n in range(6):
        book.add("foreign" if n % 2 else "late", n=n, reply="61 05 02")
    got = client.get("/api/ledger").get_json()["entries"]
    assert [e["n"] for e in got] == [0, 1, 2, 3, 4, 5] and got[0]["reply"] == "61 05 02"
    assert [e["n"] for e in client.get("/api/ledger?n=2").get_json()["entries"]] == [4, 5]
    assert [e["n"] for e in client.get("/api/ledger?class=late").get_json()["entries"]] == [0, 2, 4]
    assert [e["n"] for e in client.get("/api/ledger?class=late&n=1").get_json()["entries"]] == [4]
    assert client.get("/api/ledger?n=-5").status_code == 200
