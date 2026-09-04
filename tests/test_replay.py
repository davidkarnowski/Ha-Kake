# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ReplayELM — the transport that serves a recorded session instead of a car.

These tests are the guarantee behind the project's invitation to write a
profile for your own car: the whole stack has to run with no adapter, and it
has to run *honestly* — replayed lines are verbatim recorded lines, and
anything the capture does not hold answers NO DATA rather than something
plausible.
"""
import asyncio
import json
import os

import pytest

from conftest import ROOT, FIXTURES  # noqa: E402  (sys.path is set up there)

import elm327  # noqa: E402
import record_session as rec  # noqa: E402
from elm327 import (ReplayELM, ReplayFixtureError, load_replay_fixture,  # noqa: E402
                    passive_capture, replay_fixture_path, set_uds_target)

SESSIONS = {"leaf_ze0": os.path.join(FIXTURES, "session_leaf_ze0.json"),
            "lancer_2009": os.path.join(FIXTURES, "session_lancer_2009.json")}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def leaf():
    return ReplayELM(SESSIONS["leaf_ze0"])


# ── fixture format ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(SESSIONS))
def test_shipped_session_fixtures_are_valid(name):
    doc = load_replay_fixture(SESSIONS[name])
    assert doc["vehicle"] == name
    assert doc["frames"]
    assert doc["source"], "a session fixture must say where its data came from"


@pytest.mark.parametrize("name", sorted(SESSIONS))
def test_shipped_fixtures_are_not_labelled_synthetic(name):
    """Both shipped fixtures are derived from real captures. If one ever stops
    being real it must say so, loudly, in the file and in the startup log."""
    doc = load_replay_fixture(SESSIONS[name])
    assert doc["synthetic"] is False


def test_leaf_fixture_lines_all_come_from_the_committed_captures():
    """No invented telemetry: every replayed line must exist verbatim in a raw
    capture that is already in the repo."""
    doc = load_replay_fixture(SESSIONS["leaf_ze0"])
    lbc = json.load(open(os.path.join(FIXTURES, "lbc_raw_20260824.json")))
    probe = json.load(open(os.path.join(FIXTURES, "probe_20260824_185139.json")))
    known = set()
    for group in list(lbc["groups"].values()) + list(probe["hvac"].values()) + list(probe["passive"].values()):
        known.update(group)
    for fr in doc["frames"]:
        for cmds in fr["uds"].values():
            for lines in cmds.values():
                assert set(lines) <= known
        for lines in fr["passive"].values():
            assert set(lines) <= known


def test_lancer_fixture_lines_all_come_from_the_committed_captures():
    doc = load_replay_fixture(SESSIONS["lancer_2009"])
    known = set()
    for name in ("lancer_idle_raw_20260828.json", "lancer_dtc_raw_20260828.json"):
        for lines in json.load(open(os.path.join(FIXTURES, name)))["responses"].values():
            known.update(lines)
    for fr in doc["frames"]:
        for cmds in fr["uds"].values():
            for lines in cmds.values():
                assert set(lines) <= known


def test_rejects_a_file_that_is_not_a_session_fixture(tmp_path):
    p = tmp_path / "raw.json"
    p.write_text(json.dumps({"captured": "now", "groups": {}}))
    with pytest.raises(ReplayFixtureError):
        load_replay_fixture(str(p))


def test_rejects_empty_and_malformed_frames(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"hakake_replay": 1, "frames": []}))
    with pytest.raises(ReplayFixtureError):
        load_replay_fixture(str(p))
    p.write_text(json.dumps({"hakake_replay": 1, "frames": [{"uds": []}]}))
    with pytest.raises(ReplayFixtureError):
        load_replay_fixture(str(p))


def test_default_fixture_path_follows_the_profile(use_vehicle):
    use_vehicle("lancer_2009")
    assert replay_fixture_path().endswith("session_lancer_2009.json")
    use_vehicle("leaf_ze0")
    assert replay_fixture_path().endswith("session_leaf_ze0.json")
    assert replay_fixture_path("lancer_2009").endswith("session_lancer_2009.json")


# ── the ELM327 command surface ───────────────────────────────────────────

def test_at_commands_answer_like_an_adapter(leaf):
    assert run(leaf.send("ATI")) == [leaf.adapter_id]
    assert run(leaf.send("ATZ")) == [leaf.adapter_id]
    for cmd in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6", "ATFCSH 79B",
                "ATFCSD 30 00 20", "ATFCSM1"):
        assert run(leaf.send(cmd)) == []            # a real adapter says OK, which send() strips
    assert run(leaf.send("")) == []                 # the poke that interrupts ATMA


def test_ati_satisfies_the_readers_liveness_probe(leaf):
    """reader.probe_alive() wants a non-empty answer containing a digit."""
    lines = run(leaf.send("ATI"))
    assert lines and any(c.isdigit() for c in " ".join(lines))


def test_atsh_selects_which_ecu_answers(leaf):
    run(leaf.send("ATSH 79B"))
    run(leaf.send("ATCRA 7BB"))
    assert run(leaf.send("2101"))[0].startswith("7BB")
    # the HVAC amp does not answer LBC group 01
    run(set_uds_target(leaf, "744", "764"))
    assert run(leaf.send("2101")) == ["NO DATA"]
    assert run(leaf.send("2110"))[0].startswith("764")


def test_atcra_filters_response_lines(leaf):
    run(leaf.send("ATSH 7E0"))                       # nothing under that header here
    assert run(leaf.send("0105")) == ["NO DATA"]
    lancer = ReplayELM(SESSIONS["lancer_2009"])
    run(set_uds_target(lancer, "7E0", "7E8"))
    assert run(lancer.send("0105")) == ["7E8 03 41 05 8B"]   # the 7E9 twin is filtered out
    run(set_uds_target(lancer, "7E1", "7E9"))
    assert run(lancer.send("03")) == ["7E9 04 43 01 08 68"]


def test_missing_command_answers_no_data_and_is_recorded(leaf):
    run(set_uds_target(leaf, "744", "764"))
    assert run(leaf.send("2100")) == ["NO DATA"]     # not in the capture — the car's silence, kept
    assert "744:2100" in leaf.misses


def test_passive_capture_returns_recorded_frames(leaf):
    lines = run(passive_capture(leaf, "421", 0.2))
    assert lines and all(l.startswith("421 ") for l in lines)
    assert leaf.caf is False                          # passive_capture left ATCAF0 set, as on hardware


def test_monitor_with_no_filter_returns_everything(leaf):
    run(leaf.send("ATCAF0"))
    run(leaf.send("ATAR"))
    ids = {l.split()[0] for l in leaf.monitor()}
    assert len(ids) > 3


# ── the timeline ─────────────────────────────────────────────────────────

def test_frames_advance_with_time_and_loop(leaf):
    assert leaf.frame_index(now=0.0) == 0
    assert leaf.frame_index(now=3.5) == 3
    assert leaf.frame_index(now=len(leaf.frames) + 3) == 3   # wrapped round the loop


def test_loop_can_be_switched_off():
    elm = ReplayELM(SESSIONS["leaf_ze0"], loop=False)
    assert elm.frame_index(now=10_000) == len(elm.frames) - 1


def test_speed_scales_the_clock(monkeypatch):
    elm = ReplayELM(SESSIONS["leaf_ze0"], speed=10.0)
    t = [1000.0]
    monkeypatch.setattr(elm327.time, "monotonic", lambda: t[0])
    run(elm.connect(log=lambda *a: None))
    t[0] += 0.3
    assert elm.elapsed() == pytest.approx(3.0)
    assert elm.frame_index() == 3


def test_frames_are_cumulative(leaf):
    """A later frame only carries what changed; the UDS answers recorded in
    frame 0 stay available for the rest of the session."""
    uds, passive = leaf.view(len(leaf.frames) - 1)
    assert "2101" in uds["79B"]
    assert passive["284"] != leaf.view(0)[1]["284"]     # the speed counter did move


def test_passive_values_change_over_the_session(leaf):
    seen = set()
    for i in range(len(leaf.frames)):
        _, passive = leaf.view(i)
        seen.add(tuple(passive["284"]))
    assert len(seen) > 1, "replay should show motion, not one frozen frame"


# ── detect_adapter wiring ────────────────────────────────────────────────

def test_detect_adapter_replay_uses_the_env_fixture(monkeypatch):
    monkeypatch.setenv("HAKAKE_REPLAY_FIXTURE", SESSIONS["lancer_2009"])
    elm = run(elm327.detect_adapter(prefer="replay", log=lambda *a: None))
    assert isinstance(elm, ReplayELM)
    assert elm.adapter_type == "replay" and elm.replay is True
    assert elm.vehicle == "lancer_2009"
    run(elm.close())


def test_detect_adapter_replay_honours_speed_and_loop_env(monkeypatch):
    monkeypatch.setenv("HAKAKE_REPLAY_FIXTURE", SESSIONS["leaf_ze0"])
    monkeypatch.setenv("HAKAKE_REPLAY_SPEED", "4")
    monkeypatch.setenv("HAKAKE_REPLAY_LOOP", "0")
    elm = run(elm327.detect_adapter(prefer="replay", log=lambda *a: None))
    assert elm.speed == 4.0 and elm.loop is False


def test_auto_detect_never_picks_replay(monkeypatch):
    """Recorded data must be asked for. If auto-detect could fall back to it,
    a dashboard with a flat battery would quietly show yesterday's drive."""
    monkeypatch.setattr(elm327, "_find_serial_port", lambda: None)

    async def no_ble(self, log=print):
        raise ConnectionError("no BLE here")

    monkeypatch.setattr(elm327.BleELM, "connect", no_ble)
    with pytest.raises(ConnectionError):
        run(elm327.detect_adapter(prefer=None, log=lambda *a: None))


def test_connect_log_says_it_is_a_replay(leaf):
    lines = []
    run(leaf.connect(log=lines.append))
    blob = " ".join(lines)
    assert "REPLAY" in blob and leaf.path in blob
    assert "source" in blob


def test_synthetic_fixture_is_announced(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"hakake_replay": 1, "vehicle": "leaf_ze0", "synthetic": True,
                             "frames": [{"t": 0, "uds": {}, "passive": {}}]}))
    elm = ReplayELM(str(p))
    lines = []
    run(elm.connect(log=lines.append))
    assert any("SYNTHETIC" in l for l in lines)


# ── the deriver that produced the shipped fixtures ───────────────────────

def test_derive_reproduces_the_committed_fixtures(tmp_path):
    """The fixtures in the repo are exactly what `record_session.py --derive`
    makes from the committed captures — nothing was hand-edited into them."""
    rec.derive(out_dir=str(tmp_path), log=lambda *a: None)
    for name, path in SESSIONS.items():
        made = json.load(open(tmp_path / f"session_{name}.json"))
        have = json.load(open(path))
        assert made["frames"] == have["frames"]
        assert made["source"] == have["source"]
