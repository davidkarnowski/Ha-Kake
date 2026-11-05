# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""replyproof — which bytes are the answer, and what becomes of the rest.

The rules under test: a reply is the answer only when its echo (or the
bridge's ack) says so; ISO-TP is reassembled strictly; nothing in the window
is dropped, every line ends up in exactly one message; and what the decoders
make of a proven reply is exactly what they make of the car's own frames.
"""

import json
import os

import pytest

from conftest import ROOT, FIXTURES  # noqa: F401  (sys.path is set up there)

import replyproof as rp  # noqa: E402
from leaf_decoders import parse_isotp, decode_reading, decode_hvac  # noqa: E402

with open(os.path.join(FIXTURES, "session_leaf_ze0.json")) as _f:
    UDS = json.load(_f)["frames"][0]["uds"]
LBC, HVAC = UDS["79B"], UDS["744"]


def req(cmd):
    return bytes.fromhex(cmd)


def payload_of(lines, rx):
    (m,) = rp.messages(lines, rx)
    return m.payload


# ── echoes ───────────────────────────────────────────────────────────────

def test_a_positive_reply_starts_with_the_service_plus_0x40_and_what_the_service_repeats():
    assert rp.expected_echo(req("2101")) == b"\x61\x01"
    assert rp.expected_echo(req("22F190")) == b"\x62\xF1\x90"
    assert rp.expected_echo(req("010C0D")) == b"\x41\x0C"          # mode 01: the first PID asked for
    assert rp.expected_echo(req("03")) == b"\x43" and rp.expected_echo(req("07")) == b"\x47"
    assert rp.expected_echo(b"") == b""


def test_answers_is_the_echo_and_nothing_else():
    assert rp.answers(b"\x61\x01\xFF", req("2101"))
    assert not rp.answers(b"\x61\x05\xFF", req("2101"))            # another group's reply
    assert not rp.answers(b"\x7F\x21\x12", req("2101"))
    assert not rp.answers(b"", req("2101")) and not rp.answers(b"\x61\x01", b"")


def test_a_negative_reply_names_the_service_it_refuses():
    assert rp.negative_code(b"\x7F\x21\x12", req("2101")) == 0x12
    assert rp.negative_code(b"\x7F\x22\x12", req("2101")) is None  # somebody else's refusal
    assert rp.negative_code(b"\x7F\x21", req("2101")) is None
    assert rp.negative_code(b"\x61\x01", req("2101")) is None


def test_the_request_a_reply_answers_can_be_read_back_from_it():
    assert rp.request_of(b"\x61\x05\x02\xD0") == req("2105")
    assert rp.request_of(b"\x62\xF1\x90\x41") == req("22F190")
    assert rp.request_of(b"\x43\x01\x33") == req("03")
    assert rp.request_of(b"\x7F\x21\x12") is None and rp.request_of(b"") is None and rp.request_of(b"\x10") is None


def test_hex_parsing_takes_spaced_or_packed_and_refuses_the_rest():
    assert rp.parse_hex("61 01 FF") == rp.parse_hex("6101ff") == b"\x61\x01\xFF"
    assert rp.parse_hex("61 0") is None and rp.parse_hex("zz") is None and rp.parse_hex(None) is None
    assert rp.hex_bytes(b"\x61\x01\xff") == "61 01 FF"


# ── strict reassembly ────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", sorted(LBC))
def test_each_captured_group_is_one_complete_message_cut_to_its_declared_length(cmd):
    lines = LBC[cmd]
    (m,) = rp.messages(lines, "7BB")
    declared = ((int(lines[0].split()[1], 16) & 0x0F) << 8) | int(lines[0].split()[2], 16)
    assert m.complete and m.declared == declared == len(m.payload) and m.frames == len(lines) and m.lines == lines
    assert m.payload[:2] == bytes([0x61, int(cmd[2:], 16)])
    assert m.payload[2:] == bytes(parse_isotp(lines))[:declared - 2]            # the same bytes, minus the padding


def test_the_declared_lengths_of_the_battery_groups():
    got = {cmd: len(payload_of(LBC[cmd], "7BB")) - 2 for cmd in sorted(LBC)}
    assert got == {"2101": 39, "2102": 196, "2103": 26, "2104": 14, "2105": 69, "2106": 24}


def test_two_replies_in_one_window_are_two_messages():
    seen = rp.messages(LBC["2105"] + LBC["2101"], "7BB")
    assert [(m.complete, m.payload[:2]) for m in seen] == [(True, b"\x61\x05"), (True, b"\x61\x01")]
    assert seen[0].lines == LBC["2105"] and seen[1].lines == LBC["2101"]


def test_frames_of_other_ids_and_unreadable_lines_are_not_ours():
    noisy = ["764 10 2B 61 10 49 3D 29 02", LBC["2101"][0], "garbage", "7BB zz", ""] + LBC["2101"][1:]
    (m,) = rp.messages(noisy, "7bb")
    assert m.complete and m.lines == LBC["2101"]


def test_a_reply_cut_short_is_incomplete_and_kept():
    (m,) = rp.messages(LBC["2101"][:3], "7BB")
    assert not m.complete and m.declared == 41 and m.lines == LBC["2101"][:3]


def test_a_frame_out_of_sequence_ends_the_message_and_is_kept_as_a_fragment():
    lines = LBC["2101"]
    broken = [lines[0], lines[1], lines[3], lines[4]]                   # consecutive frame 2 never arrived
    seen = rp.messages(broken, "7BB")
    assert [m.complete for m in seen] == [False, False, False]
    assert [m.lines for m in seen] == [[lines[0], lines[1]], [lines[3]], [lines[4]]]
    assert sum(m.frames for m in seen) == len(broken)                   # every line is in exactly one message


def test_a_new_first_frame_closes_an_unfinished_message():
    seen = rp.messages(LBC["2105"][:4] + LBC["2101"], "7BB")
    assert [(m.complete, m.payload[:2]) for m in seen] == [(False, b"\x61\x05"), (True, b"\x61\x01")]


def test_single_frames_and_flow_control():
    seen = rp.messages(["7BB 03 7F 21 12 FF FF FF FF", "7BB 30 00 00", "7BB 02 61 05"], "7BB")
    assert [(m.complete, m.payload) for m in seen] == [(True, b"\x7F\x21\x12"), (True, b"\x61\x05")]
    (empty,) = rp.messages(["7BB 00"], "7BB")
    assert not empty.complete and empty.payload == b""
    (short,) = rp.messages(["7BB 05 61 01"], "7BB")                     # says five bytes, carries two
    assert not short.complete


@pytest.mark.parametrize("n", [1, 6, 7, 8, 13, 14, 41, 198, 300, 4095])
def test_payload_to_lines_and_back(n):
    payload = bytes((i * 7 + 3) & 0xFF for i in range(n))
    lines = rp.lines_from_payload("7bb", payload)
    (m,) = rp.messages(lines, "7BB")
    assert m.complete and m.payload == payload and all(line.startswith("7BB ") for line in lines)
    assert all(len(line.split()) <= 9 for line in lines)                # an id and at most eight bytes


def test_a_payload_too_long_for_classic_iso_tp_is_refused():
    with pytest.raises(ValueError):
        rp.lines_from_payload("7BB", bytes(4096))


# ── a proven reply decodes exactly as the car's own frames do ────────────

def tail_of(lines, rx):
    """What the ECU filled its last frame with: the bytes past the declared length."""
    (m,) = rp.messages(lines, rx)
    data = [bytes(int(b, 16) for b in line.split()[1:]) for line in lines]
    carried = (data[0][2:] if len(lines) > 1 else data[0][1:]) + b"".join(d[1:] for d in data[1:])
    return carried[len(m.payload):]


EVERY = [("7BB", cmd, LBC[cmd]) for cmd in sorted(LBC)] + [("764", cmd, HVAC[cmd]) for cmd in sorted(HVAC)]


@pytest.mark.parametrize("rx, cmd, lines", EVERY, ids=[f"{rx}-{cmd}" for rx, cmd, _ in EVERY])
def test_a_reply_and_its_padding_rebuild_the_cars_frames_exactly(rx, cmd, lines):
    """What a bridge puts in its ack (`reply`, `pad`) is enough to get back the
    very lines an adapter on the bus would print, so nothing downstream can
    tell the difference: not the decoders, not the raw fields they keep."""
    payload = payload_of(lines, rx)
    assert rp.lines_from_payload(rx, payload, tail_of(lines, rx)) == lines


@pytest.mark.parametrize("rx, cmd, lines", EVERY, ids=[f"{rx}-{cmd}" for rx, cmd, _ in EVERY])
def test_without_the_padding_every_decoded_value_is_still_the_same(rx, cmd, lines):
    """Only the fields that keep raw bytes (padding included) can differ."""
    bare = rp.lines_from_payload(rx, payload_of(lines, rx))
    decode = (lambda ls: decode_reading({cmd: ls})) if rx == "7BB" else (lambda ls: decode_hvac({cmd: ls}))
    want, got = decode(lines), decode(bare)
    assert want and set(got) == set(want)
    assert {k for k in want if got[k] != want[k]} <= {k for k in want if k.endswith("_raw")}


def test_padding_never_makes_a_frame_longer_than_eight_bytes():
    lines = rp.lines_from_payload("7BB", b"\x61\x01\x02", b"\xFF" * 20)
    assert lines == ["7BB 03 61 01 02 FF FF FF FF"]


# ── the decision: with the bridge's word ─────────────────────────────────

def ack_for(cmd, lines, **over):
    rx = lines[0].split()[0]
    payload = payload_of(lines, rx)
    return dict({"req": "r1", "ok": True, "frames": len(lines), "bytes": len(payload), "reply": rp.hex_bytes(payload),
                 "request": rp.hex_bytes(req(cmd)), "tx": "79B", "rx": rx}, **over)


def test_the_acks_reply_is_the_answer_whatever_else_is_in_the_window():
    g01, g05 = LBC["2101"], LBC["2105"]
    ex = rp.resolve(req("2101"), "7BB", ack_for("2101", g01), g05 + g01, tx_id="79B")
    assert ex.ok and ex.via == "ack" and ex.problem is None and ex.negative is None
    assert ex.payload == payload_of(g01, "7BB") and ex.mirror is True
    assert decode_reading({"2101": ex.lines}) == decode_reading({"2101": g01})
    padded = rp.resolve(req("2101"), "7BB", ack_for("2101", g01, pad=rp.hex_bytes(tail_of(g01, "7BB"))), [])
    assert padded.lines == g01 or tail_of(g01, "7BB") == b""          # with the bridge's `pad`: the frames themselves
    assert [m.payload[:2] for m in ex.strays] == [b"\x61\x05"]          # seen, kept, not the answer


def test_the_ack_answers_even_when_no_frame_arrived():
    ex = rp.resolve(req("2101"), "7BB", ack_for("2101", LBC["2101"]), [])
    assert ex.ok and ex.via == "ack" and ex.mirror is None and ex.strays == []


def test_a_mirror_that_disagrees_with_the_ack_is_reported_and_the_ack_still_wins():
    ex = rp.resolve(req("2101"), "7BB", ack_for("2101", LBC["2101"]), LBC["2105"])
    assert ex.ok and ex.via == "ack" and ex.mirror is False and len(ex.strays) == 1


def test_an_ack_about_another_request_is_not_our_answer():
    for over in ({"request": "21 05"}, {"rx": "764"}, {"tx": "744"}):
        ex = rp.resolve(req("2101"), "7BB", ack_for("2101", LBC["2101"], **over), [], tx_id="79B")
        assert not ex.ok and ex.problem == "ack-mismatch" and ex.payload == payload_of(LBC["2101"], "7BB")


def test_a_reply_in_the_ack_with_the_wrong_echo_is_not_our_answer():
    ack = ack_for("2101", LBC["2101"], reply=rp.hex_bytes(payload_of(LBC["2105"], "7BB")))
    ex = rp.resolve(req("2101"), "7BB", ack, [])
    assert not ex.ok and ex.problem == "echo-mismatch" and ex.payload[:2] == b"\x61\x05"


def test_an_ack_that_predates_the_echo_keys_is_judged_by_its_reply_alone():
    ack = {"req": "r1", "ok": True, "reply": rp.hex_bytes(payload_of(LBC["2101"], "7BB"))}
    assert rp.resolve(req("2101"), "7BB", ack, []).via == "ack"


def test_the_car_refusing_is_the_cars_answer_and_is_handed_back_as_an_adapter_would_print_it():
    ex = rp.resolve(req("2101"), "7BB", {"ok": True, "reply": "7F 21 12", "request": "21 01", "pad": "FF FF FF FF"}, [])
    assert ex.ok and ex.negative == 0x12 and ex.via == "ack" and ex.problem is None
    assert ex.lines == ["7BB 03 7F 21 12 FF FF FF FF"]
    ex = rp.resolve(req("2101"), "7BB", {"ok": True, "frames": 1}, ["7BB 03 7F 21 12 FF FF FF FF"])
    assert ex.ok and ex.negative == 0x12 and ex.via == "frames" and ex.lines == ["7BB 03 7F 21 12 FF FF FF FF"]
    assert decode_reading({"2101": ex.lines}) == {}                    # and it decodes to nothing, as it always did


def test_a_wait_answer_followed_by_the_real_one_resolves_to_the_real_one():
    """7F <service> 78 means "still working": the reply proper follows in the same window."""
    window = ["7BB 03 7F 21 78"] + LBC["2101"]
    ex = rp.resolve(req("2101"), "7BB", {"ok": True}, window)
    assert ex.lines == LBC["2101"] and ex.negative is None and [m.payload for m in ex.strays] == [b"\x7F\x21\x78"]


def test_a_failed_ack_is_no_answer_and_the_window_is_still_handed_back():
    for error in ("timeout", "superseded", "expired", "refused"):
        ex = rp.resolve(req("2101"), "7BB", {"req": "r1", "ok": False, "error": error}, LBC["2105"])
        assert not ex.ok and ex.problem == error and [m.payload[:2] for m in ex.strays] == [b"\x61\x05"]
    assert rp.resolve(req("2101"), "7BB", None, []).problem == "no-ack"


# ── the decision: from the frames alone ──────────────────────────────────

def test_without_a_reply_in_the_ack_the_answer_is_picked_by_its_echo():
    """2026-11-05: a late group-05 reply arrives ahead of the group-01 answer."""
    g01, g05 = LBC["2101"], LBC["2105"]
    ex = rp.resolve(req("2101"), "7BB", {"req": "r1", "ok": True, "frames": len(g01)}, g05 + g01)
    assert ex.ok and ex.via == "frames" and ex.lines == g01            # the car's own frames, untouched
    (stray,) = ex.strays
    assert stray.complete and rp.request_of(stray.payload) == req("2105") and stray.lines == g05


def test_another_groups_reply_alone_in_the_window_is_not_an_answer():
    ex = rp.resolve(req("2101"), "7BB", {"ok": True, "frames": 11}, LBC["2105"])
    assert not ex.ok and ex.problem == "echo-mismatch" and len(ex.strays) == 1


def test_an_empty_or_unfinished_window_says_which():
    assert rp.resolve(req("2101"), "7BB", {"ok": True}, []).problem == "no-reply"
    assert rp.resolve(req("2101"), "7BB", {"ok": True}, LBC["2101"][:2]).problem == "incomplete"


def test_of_two_replies_with_the_right_echo_the_last_is_the_answer():
    """The bridge runs requests one at a time: what is ahead of ours in the
    window answered an earlier request."""
    old = rp.lines_from_payload("7BB", b"\x61\x01" + bytes(39))
    new = LBC["2101"]
    ex = rp.resolve(req("2101"), "7BB", {"ok": True, "frames": len(new)}, old + new)
    assert ex.lines == new and [m.lines for m in ex.strays] == [old]


def test_the_acked_frame_count_breaks_a_tie_when_it_can():
    short = ["7BB 03 61 01 55"]                                         # a one-frame reply with our echo
    ex = rp.resolve(req("2101"), "7BB", {"ok": True, "frames": len(LBC["2101"])}, LBC["2101"] + short)
    assert ex.lines == LBC["2101"] and [m.lines for m in ex.strays] == [short]
    ex = rp.resolve(req("2101"), "7BB", {"ok": True, "frames": 99}, LBC["2101"] + short)
    assert ex.lines == short                                            # no message of that size: the last one stands


def test_every_line_in_the_window_is_accounted_for():
    window = LBC["2105"][:5] + LBC["2102"] + LBC["2101"] + [LBC["2104"][1]]
    ex = rp.resolve(req("2101"), "7BB", {"ok": True}, window)
    assert ex.lines == LBC["2101"]
    assert sum(m.frames for m in ex.strays) + len(ex.lines) == len(window)
    assert [m.complete for m in ex.strays] == [False, True, False]
