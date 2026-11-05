# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""linkaccount — the books: what every request came to, what became of every reply.

No reader, no transport: request records as the façade keeps them, a fake
clock, a ledger in tmp_path. The promises under test: nothing that arrives is
dropped silently; a late reply is credited to the item that asked for it and
to no other; a reply nobody asked for is recorded as foreign; every ledger
line carries the bytes; and the ledger cannot take its writer down.
"""

import json
import os

import pytest

from conftest import ROOT, FIXTURES  # noqa: F401  (sys.path is set up there)

import linkaccount as la  # noqa: E402
import replyproof as rp  # noqa: E402

with open(os.path.join(FIXTURES, "session_leaf_ze0.json")) as _f:
    LBC = json.load(_f)["frames"][0]["uds"]["79B"]
ITEM_OF = {"2101": "lbc01", "2102": "lbc02", "2105": "lbc05"}


def find_item(bus, rx, request):
    return ITEM_OF.get(bytes(request).hex().upper()) if (bus, rx) == ("car", "7BB") else None


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def books(tmp_path):
    clock = Clock()
    ledger = la.Ledger(str(tmp_path / "reply_ledger.jsonl"), clock=Clock(1_793_906_572.41))
    return la.LinkAccount(find_item, ledger, mono=clock), clock, ledger


def record(cmd, ack, lines=(), rtt_ms=250.0):
    """A request record, as CanFacade._request keeps it."""
    request = bytes.fromhex(cmd)
    ex = rp.resolve(request, "7BB", ack, list(lines), tx_id="79B")
    return {"tx": "79B", "rx": "7BB", "cmd": cmd, "request": request, "t_asked": 1.0, "t": 1.25, "rtt_ms": rtt_ms,
            "ack": ack, "ok": ex.ok, "via": ex.via, "payload": ex.payload, "negative": ex.negative,
            "problem": ex.problem, "strays": ex.strays, "mirror": ex.mirror}


def payload(cmd):
    (m,) = rp.messages(LBC[cmd], "7BB")
    return m.payload


def ack_with_reply(cmd, **over):
    p = payload(cmd)
    return dict({"req": "mac-1f2e-000007", "ok": True, "frames": len(LBC[cmd]), "bytes": len(p),
                 "reply": rp.hex_bytes(p), "request": rp.hex_bytes(bytes.fromhex(cmd)), "tx": "79B", "rx": "7BB",
                 "src": "mac-1f2e", "seq": 7, "t_in": 500.0, "t_done": 500.21, "queue_ms": 3, "bus_ms": 201}, **over)


# ── the ledger ───────────────────────────────────────────────────────────

def test_a_ledger_line_says_when_what_and_carries_what_it_was_given(tmp_path):
    ledger = la.Ledger(str(tmp_path / "l.jsonl"), clock=Clock(1_793_906_572.41))
    entry = ledger.add("late", item="lbc05", reply="61 05 02", late_s=11.2, nothing=None)
    (line,) = open(ledger.path).read().splitlines()
    assert json.loads(line) == entry == {"t": 1_793_906_572.41, "ts": "2026-11-05T19:22:52.410Z", "class": "late",
                                         "item": "lbc05", "reply": "61 05 02", "late_s": 11.2}
    assert ledger.written == 1 and ledger.failed == 0


def test_the_ledger_rolls_over_and_keeps_one_old_file(tmp_path):
    ledger = la.Ledger(str(tmp_path / "l.jsonl"), max_bytes=300)
    for n in range(12):
        ledger.add("foreign", n=n, reply="61 02 " + "0F " * 20)
    assert os.path.exists(ledger.path + ".1")
    assert os.path.getsize(ledger.path) < 600 and sorted(os.listdir(tmp_path)) == ["l.jsonl", "l.jsonl.1"]
    assert ledger.tail(1)[0]["n"] == 11


def test_a_ledger_that_cannot_be_written_counts_the_failure_and_carries_on(tmp_path):
    ledger = la.Ledger(str(tmp_path / "no-such-folder" / "l.jsonl"))
    assert ledger.add("late", item="lbc05")["class"] == "late"
    assert ledger.failed == 1 and ledger.written == 0 and ledger.tail() == []
    assert la.Ledger(None).add("late")["class"] == "late"              # no file at all: still an entry, still no error


def test_tail_is_the_newest_entries_oldest_first_and_skips_what_is_not_an_entry(tmp_path):
    path = str(tmp_path / "l.jsonl")
    ledger = la.Ledger(path)
    for n in range(5):
        ledger.add("foreign", n=n)
    with open(path, "a") as f:
        f.write("not json\n[1, 2]\n")
    ledger.add("late", n=5)
    assert [e["n"] for e in la.tail(path, 3)] == [5]                   # two of the last three lines are not entries
    assert [e["n"] for e in la.tail(path, 4)] == [4, 5]
    assert [e["n"] for e in la.tail(path, 100)] == [0, 1, 2, 3, 4, 5]
    assert la.tail(path, 0) == [] and la.tail(str(tmp_path / "absent"), 5) == [] and la.tail(None, 5) == []


# ── where a round trip went ──────────────────────────────────────────────

def test_a_round_trip_splits_into_queue_bus_and_the_rest_from_durations_alone():
    assert la.split_rtt(253.0, {"queue_ms": 3, "bus_ms": 201}) == {"queue_ms": 3, "bus_ms": 201, "net_ms": 49.0}
    assert la.split_rtt(100.0, {"queue_ms": 80, "bus_ms": 60})["net_ms"] == 0.0      # never negative
    assert la.split_rtt(253.0, {"bus_ms": 201}) == {"bus_ms": 201}                   # an older bridge: no guess
    assert la.split_rtt(253.0, {"queue_ms": True, "bus_ms": "9"}) == {}
    assert la.split_rtt(253.0, {}) == {}


# ── one request ──────────────────────────────────────────────────────────

def test_an_answer_proven_by_the_ack_is_entered_with_where_its_time_went(tmp_path):
    link, clock, ledger = books(tmp_path)
    link.begin_cycle()
    entry = link.exchange("car", "lbc01", record("2101", ack_with_reply("2101"), LBC["2101"], rtt_ms=253.0),
                          caps=["ack-reply", "supersede"])
    assert entry == {"rtt_ms": 253.0, "req": "mac-1f2e-000007", "queue_ms": 3, "bus_ms": 201, "net_ms": 49.0,
                     "t_bus": 500.21, "via": "ack", "state": "ok"}
    block = link.block()
    assert block["via"] == "ack" and block["caps"] == ["ack-reply", "supersede"]
    assert block["items"] == {"lbc01": entry} and block["cycle"] == {"ok": 1, "sent": 1} == block["totals"]
    assert ledger.tail() == []                                          # a plain answer in time is not news


def test_counts_are_per_cycle_and_totals_run_on(tmp_path):
    link, clock, ledger = books(tmp_path)
    for _ in range(3):
        link.begin_cycle()
        link.exchange("car", "lbc01", record("2101", ack_with_reply("2101")))
    assert link.block()["cycle"] == {"ok": 1, "sent": 1} and link.block()["totals"] == {"ok": 3, "sent": 3}


def test_a_late_reply_found_in_the_window_is_credited_to_the_item_that_asked_for_it(tmp_path):
    """2026-11-05, with a bridge that does not put replies in its acks."""
    link, clock, ledger = books(tmp_path)
    link.begin_cycle()
    gave_up = link.exchange("car", "lbc05", record("2105", {"req": "a", "ok": False, "error": "timeout"}))
    assert gave_up["state"] == "timeout" and link.cycle["timeouts"] == 1 and ledger.tail() == []
    clock.t += 1.5
    link.begin_cycle()
    entry = link.exchange("car", "lbc01", record("2101", {"req": "b", "ok": True, "frames": 6}, LBC["2105"] + LBC["2101"]))
    assert entry["state"] == "ok" and entry["via"] == "frames"
    (credit,) = link.take_credits()
    assert credit["item"] == "lbc05" and credit["lines"] == LBC["2105"] and credit["via"] == "frames"
    assert credit["late_s"] == 1.75 and credit["given_up"] == 1000.0 and credit["request"] == "21 05"   # since it was asked
    assert link.take_credits() == [] and link.cycle["late"] == 1
    link.credited(credit, applied=True, wall=1_793_906_573.0)
    block = link.block()
    assert block["items"]["lbc05"] == {"late_s": 1.75, "state": "late"}
    assert block["cycle"] == {"late": 1, "late_credited": 1, "ok": 1, "sent": 1}
    assert block["last_stall"] == {"t": 1_793_906_573.0, "item": "lbc05", "late_s": 1.75}
    (line,) = ledger.tail()
    assert line["class"] == "late" and line["item"] == "lbc05" and line["reply"].startswith("61 05 02 D0")
    assert line["action"] == "credited to lbc05, 1.8 s late, and marked late" and line["via"] == "frames"


def test_a_late_reply_that_a_newer_reading_overtook_is_kept_in_the_ledger_only(tmp_path):
    link, clock, ledger = books(tmp_path)
    link.exchange("car", "lbc05", record("2105", {"ok": False, "error": "timeout"}))
    clock.t += 5
    link.exchange("car", "lbc05", record("2105", {"ok": True, "frames": 11}, LBC["2105"]))     # read again, in time
    clock.t += 5
    link.exchange("car", "lbc01", record("2101", {"ok": True, "frames": 6}, LBC["2105"] + LBC["2101"]))
    (credit,) = link.take_credits()                                     # still ours, still late, not foreign
    assert credit["item"] == "lbc05" and credit["late_s"] == 10.25
    link.credited(credit, applied=False, wall=0.0)
    assert link.totals["late"] == 1 and link.totals["late_credited"] == 0 and link.totals["foreign"] == 0
    assert ledger.tail()[-1]["action"] == "kept here only: a newer reading of lbc05 was already in"


def test_a_reply_nobody_here_asked_for_is_foreign_recorded_once_and_counted_every_time(tmp_path):
    link, clock, ledger = books(tmp_path)
    for _ in range(3):
        link.begin_cycle()
        link.exchange("car", "lbc01", record("2101", {"ok": True, "frames": 6}, LBC["2102"] + LBC["2101"]))
    assert link.take_credits() == [] and link.totals["foreign"] == 3 and link.cycle["foreign"] == 1
    (line,) = ledger.tail()                                             # the same thing is not written thrice
    assert line["class"] == "foreign" and line["item"] == "lbc02" and line["request"] == "21 02"
    assert line["reply"].startswith("61 02") and line["frames"] == len(LBC["2102"])
    clock.t += la.REPEAT_EVERY + 1
    link.exchange("car", "lbc01", record("2101", {"ok": True, "frames": 6}, LBC["2102"] + LBC["2101"]))
    assert len(ledger.tail()) == 2                                      # but it is written again later


def test_a_request_given_up_on_long_ago_no_longer_claims_a_reply(tmp_path):
    link, clock, ledger = books(tmp_path)
    link.exchange("car", "lbc05", record("2105", {"ok": False, "error": "timeout"}))
    clock.t += la.ABANDONED_FOR + 1
    link.exchange("car", "lbc01", record("2101", {"ok": True, "frames": 6}, LBC["2105"] + LBC["2101"]))
    assert link.take_credits() == [] and link.totals["foreign"] == 1


def test_part_of_a_reply_is_recorded_as_a_fragment(tmp_path):
    link, clock, ledger = books(tmp_path)
    link.exchange("car", "lbc01", record("2101", {"ok": True, "frames": 6}, LBC["2105"][:4] + LBC["2101"]))
    assert link.totals["fragments"] == 1 and link.take_credits() == []
    (line,) = ledger.tail()
    assert line["class"] == "fragment" and line["frames"] == 4 and line["declared"] == 71


def test_with_replies_in_the_acks_the_mirror_is_counted_not_judged(tmp_path):
    """The frames are then only a mirror of other traffic; our own late replies come back through late acks."""
    link, clock, ledger = books(tmp_path)
    link.exchange("car", "lbc05", record("2105", {"ok": False, "error": "timeout"}))
    link.exchange("car", "lbc01", record("2101", ack_with_reply("2101"), LBC["2105"] + LBC["2101"]), caps=["ack-reply"])
    assert link.take_credits() == [] and link.totals["mirrored_other"] == 1 and link.totals["foreign"] == 0
    assert ledger.tail() == []


def test_a_mirror_that_disagrees_with_the_ack_is_ledgered(tmp_path):
    link, clock, ledger = books(tmp_path)
    entry = link.exchange("car", "lbc01", record("2101", ack_with_reply("2101"), LBC["2105"]))
    assert entry["state"] == "ok" and link.totals["mirror_mismatch"] == 1
    assert ledger.tail()[0]["class"] == "mirror"


# ── acks that come late ──────────────────────────────────────────────────

def late(cmd, ack, waited=11.4):
    p = rp.parse_hex(ack.get("reply")) if ack.get("ok") and "reply" in ack else None
    return {"req": "mac-1f2e-000007", "tx": "79B", "rx": "7BB", "request": bytes.fromhex(cmd), "ack": ack,
            "t_asked": 1.0, "t": 12.4, "waited_s": waited, "payload": p,
            "lines": rp.lines_from_payload("7BB", p) if p else None}


def test_a_late_ack_with_its_reply_is_credited_to_its_own_item(tmp_path):
    link, clock, ledger = books(tmp_path)
    link.exchange("car", "lbc05", record("2105", {"ok": False, "error": "timeout"}))
    clock.t += 11.4
    link.late_ack("car", late("2105", ack_with_reply("2105")))
    (credit,) = link.take_credits()
    assert credit["item"] == "lbc05" and credit["via"] == "ack" and credit["late_s"] == 11.4
    assert credit["t_src"] == 500.21 and credit["given_up"] == 1000.0 and credit["payload"] == payload("2105")
    link.credited(credit, applied=True, wall=5.0)
    line = ledger.tail()[-1]
    assert line["via"] == "ack" and line["t_src"] == 500.21 and line["ack"]["req"] == "mac-1f2e-000007"


def test_a_late_ack_without_a_reply_or_that_failed_is_only_counted(tmp_path):
    link, clock, ledger = books(tmp_path)
    link.late_ack("car", late("2105", {"req": "x", "ok": True, "frames": 11}))             # an older bridge
    link.late_ack("car", late("2105", {"req": "x", "ok": False, "error": "superseded"}))
    link.late_ack("car", late("2105", {"req": "x", "ok": False, "error": "bus-off"}))
    assert link.take_credits() == []
    assert (link.totals["late_acks"], link.totals["late_superseded"], link.totals["late_failed"]) == (1, 1, 1)


# ── requests that got no proven answer ───────────────────────────────────

@pytest.mark.parametrize("error", ["superseded", "duplicate", "expired", "busy", "refused", "offline"])
def test_a_request_the_bridge_did_not_run_is_counted_under_its_reason(tmp_path, error):
    link, clock, ledger = books(tmp_path)
    entry = link.exchange("car", "lbc01", record("2101", {"req": "r", "ok": False, "error": error}))
    assert entry["state"] == error and link.totals[error] == 1 and ledger.tail() == []


def test_a_reply_that_is_not_provably_ours_is_ledgered_with_what_was_in_hand(tmp_path):
    link, clock, ledger = books(tmp_path)
    entry = link.exchange("car", "lbc01", record("2101", {"ok": True, "frames": 11}, LBC["2105"]))
    assert entry["state"] == "echo-mismatch" and link.totals["unproven"] == 1
    kinds = [e["class"] for e in ledger.tail()]
    assert "echo-mismatch" in kinds and "foreign" in kinds              # no answer, and the reply that was there
    line = next(e for e in ledger.tail() if e["class"] == "echo-mismatch")
    assert line["request"] == "21 01" and line["window"][0].startswith("61 05")


def test_the_ecu_refusing_is_entered_as_its_answer(tmp_path):
    link, clock, ledger = books(tmp_path)
    entry = link.exchange("car", "lbc01", record("2101", {"ok": True, "reply": "7F 21 12", "request": "21 01"}))
    assert entry["state"] == "negative" and entry["nrc"] == 0x12 and entry["via"] == "ack"
    (line,) = ledger.tail()
    assert line["class"] == "negative" and line["nrc"] == "0x12" and line["reply"] == "7F 21 12"


def test_a_value_outside_its_limits_is_ledgered_with_the_frames_that_carried_it(tmp_path):
    link, clock, ledger = books(tmp_path)
    link.flagged("car", "soc", 419.44, (0.0, 100.0), item="lbc01", lines=LBC["2101"])
    link.flagged("car", "soc", 419.44, (0.0, 100.0), item="lbc01", lines=LBC["2101"])
    assert link.totals["flagged"] == 2
    (line,) = ledger.tail()
    assert line["class"] == "out-of-limits" and line["key"] == "soc" and line["value"] == 419.44
    assert line["limits"] == [0.0, 100.0] and line["frames"] == LBC["2101"] and "not dropped" in line["action"]
