# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""record_session.py — the tool that turns a drive into a replayable fixture.

The live path cannot be exercised against a car here (there is no adapter in
CI and none on the bench for this branch), so it is driven by a fake
transport instead: recording *from* a ReplayELM and replaying the result must
decode to the same numbers. That covers everything in record() except the
adapter handshake itself. The `--derive` path needs no hardware at all and is
tested directly.
"""
import asyncio
import json
import os

import pytest

from conftest import FIXTURES  # noqa: E402  (sys.path is set up there)

import record_session as rec  # noqa: E402
from elm327 import ReplayELM, load_replay_fixture  # noqa: E402
from vehicles import get_vehicle  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class SilentELM:
    """A transport where nothing answers — the car asleep, or the wrong bus."""

    adapter_type = "fake"
    adapter_name = "SilentELM"
    adapter_port = "mem"

    async def send(self, cmd, wait=0, timeout=0):
        return [] if cmd.upper().startswith("AT") else ["NO DATA"]

    async def close(self):
        pass


# ── recording from a transport ───────────────────────────────────────────

def test_record_captures_every_item_the_profile_declares():
    v = get_vehicle("leaf_ze0")
    elm = ReplayELM(os.path.join(FIXTURES, "session_leaf_ze0.json"))
    run(elm.connect(log=lambda *a: None))
    frames = run(rec.record(v, elm, seconds=0.05, period=0.02, log=lambda *a: None))
    assert frames
    uds = frames[0]["uds"]
    assert "2101" in uds["79B"] and "2110" in uds["744"]
    assert "421" in frames[0]["passive"]
    assert all(l.startswith("7BB") for l in uds["79B"]["2101"])


def test_record_leaves_out_what_did_not_answer():
    """The Leaf profile polls HVAC group 00; the source capture has no answer
    for it. A recorder that wrote a placeholder would be inventing a car."""
    v = get_vehicle("leaf_ze0")
    elm = ReplayELM(os.path.join(FIXTURES, "session_leaf_ze0.json"))
    run(elm.connect(log=lambda *a: None))
    frames = run(rec.record(v, elm, seconds=0.05, period=0.02, log=lambda *a: None))
    assert "2100" not in frames[0]["uds"].get("744", {})


def test_record_of_a_silent_car_produces_no_frames():
    v = get_vehicle("lancer_2009")
    frames = run(rec.record(v, SilentELM(), seconds=0.05, period=0.02, log=lambda *a: None))
    assert frames == []


def test_record_timestamps_frames_in_order():
    v = get_vehicle("lancer_2009")
    elm = ReplayELM(os.path.join(FIXTURES, "session_lancer_2009.json"))
    run(elm.connect(log=lambda *a: None))
    frames = run(rec.record(v, elm, seconds=0.12, period=0.03, log=lambda *a: None))
    assert len(frames) >= 2
    assert [f["t"] for f in frames] == sorted(f["t"] for f in frames)


def test_recorded_fixture_replays_to_the_same_values(tmp_path):
    """Round trip: replay -> record -> replay. Recording must not lose or
    reshape a single line, or a contributor's capture would decode differently
    from their car."""
    v = get_vehicle("leaf_ze0")
    src = ReplayELM(os.path.join(FIXTURES, "session_leaf_ze0.json"))
    run(src.connect(log=lambda *a: None))
    frames = run(rec.record(v, src, seconds=0.05, period=0.02, log=lambda *a: None))

    doc = rec.new_doc(v, source=["recorded from a replay transport in a test"])
    doc["frames"] = frames
    path = rec.write_fixture(str(tmp_path / "round.json"), doc)
    load_replay_fixture(path)                       # it is a valid session fixture

    elm = ReplayELM(path)
    run(elm.connect(log=lambda *a: None))
    responses = {}
    for iid, it in v.ITEMS.items():
        tgt = v.TARGETS[it["kind"]]
        if tgt is None:
            from elm327 import passive_capture
            run(elm.send("ATCAF0"))
            responses[iid] = run(passive_capture(elm, it["id"], 0.1, set_caf=False))
        else:
            from elm327 import set_uds_target
            run(set_uds_target(elm, tgt[0], tgt[1]))
            responses[iid] = run(elm.send(it["cmd"]))
    record, alive = v.decode(responses)
    assert alive is True
    assert record["soc"] == pytest.approx(76.87, abs=0.01)
    assert record["temps_f"] == [93.2, 95.0, 93.2, 96.8]
    assert record["gear"] == "P"


# ── the document it writes ───────────────────────────────────────────────

def test_new_doc_carries_the_provenance_a_reader_needs():
    doc = rec.new_doc(get_vehicle("lancer_2009"), source=["a capture"], notes="hello")
    assert doc["hakake_replay"] == 1
    assert doc["vehicle"] == "lancer_2009" and doc["title"]
    assert doc["synthetic"] is False and doc["source"] == ["a capture"]
    assert doc["notes"] == "hello" and doc["captured"].endswith("Z")


def test_write_fixture_is_atomic(tmp_path):
    p = str(tmp_path / "s.json")
    doc = rec.new_doc(get_vehicle("leaf_ze0"))
    doc["frames"] = [{"t": 0.0, "uds": {}, "passive": {}}]
    rec.write_fixture(p, doc)
    assert json.load(open(p))["vehicle"] == "leaf_ze0"
    assert not os.path.exists(p + ".tmp")


# ── the derive path ──────────────────────────────────────────────────────

def test_derive_writes_a_valid_fixture_per_profile(tmp_path):
    paths = rec.derive(out_dir=str(tmp_path), log=lambda *a: None)
    assert len(paths) == len(rec.DERIVERS)
    for p in paths:
        doc = load_replay_fixture(p)
        assert doc["frames"] and doc["source"]


def test_derive_one_profile(tmp_path):
    paths = rec.derive(["lancer_2009"], out_dir=str(tmp_path), log=lambda *a: None)
    assert len(paths) == 1 and paths[0].endswith("session_lancer_2009.json")


def test_derive_says_so_when_a_profile_has_no_recipe(tmp_path):
    msgs = []
    assert rec.derive(["civic_2006"], out_dir=str(tmp_path), log=msgs.append) == []
    assert any("no deriver" in m for m in msgs)


def test_main_derive_writes_the_shipped_fixtures(monkeypatch, tmp_path):
    """`record_session.py --derive` regenerates what the repo ships, in place."""
    written = {}

    def capture(path, doc):
        written[path] = doc
        return path

    monkeypatch.setattr(rec, "write_fixture", capture)
    monkeypatch.setattr(rec, "load_replay_fixture", lambda p: None)
    assert rec.main(["--derive"]) == 0
    assert {os.path.basename(p) for p in written} == {
        f"session_{n}.json" for n in rec.DERIVERS}


def test_derived_fixtures_only_contain_real_recorded_lines(tmp_path):
    """The one rule the whole project runs on: no fabricated telemetry."""
    rec.derive(out_dir=str(tmp_path), log=lambda *a: None)
    known = set()
    for name in ("lbc_raw_20260824.json", "probe_20260824_185139.json",
                 "lancer_idle_raw_20260828.json", "lancer_dtc_raw_20260828.json"):
        blob = json.load(open(os.path.join(FIXTURES, name)))
        for section in ("groups", "responses", "hvac", "passive"):
            for lines in (blob.get(section) or {}).values():
                if isinstance(lines, list):
                    known.update(lines)
    for p in sorted(os.listdir(tmp_path)):
        for fr in json.load(open(os.path.join(tmp_path, p)))["frames"]:
            for cmds in fr["uds"].values():
                for lines in cmds.values():
                    assert set(lines) <= known, p
            for lines in fr["passive"].values():
                assert set(lines) <= known, p
