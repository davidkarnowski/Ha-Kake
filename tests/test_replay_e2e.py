# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end: the whole reader stack against a recorded session, no hardware.

This is the test the public invitation rests on. It boots the real Reader —
real scheduler, real transport interface, the profile's own configure() and
decode(), the real store — with ReplayELM standing in for the adapter, and
checks that decoded values actually arrive in the state file and the database.
For both shipped profiles, because "write a profile for your car" has to mean
you can see it work.

Nothing here touches web/leaf_battery.db or web/battery_state.json: every path
is under tmp_path.
"""
import asyncio
import json
import os

import pytest

from conftest import FIXTURES  # noqa: E402  (sys.path is set up there)

import reader as rd  # noqa: E402
from elm327 import ReplayELM  # noqa: E402
from store import Store  # noqa: E402


def session(name):
    return os.path.join(FIXTURES, f"session_{name}.json")


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    """Reader wired to a replay adapter, with every file it writes in tmp_path."""
    monkeypatch.setattr(rd, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(rd, "PAUSE_FILE", str(tmp_path / "reader.pause"))
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    monkeypatch.setattr(rd, "CALIB_FILE", str(tmp_path / "calibration.json"))
    monkeypatch.setattr(rd, "STORE_PERIOD", 0.0)
    monkeypatch.setattr(rd, "BACKOFF_MIN", 0.01)
    monkeypatch.setattr(rd, "BACKOFF_MAX", 0.02)

    def boot(vehicle, seconds=0.35, speed=1.0, fixture=None):
        rd.set_vehicle(vehicle)
        elm = ReplayELM(fixture or session(vehicle), speed=speed)

        async def fake_detect(prefer=None, log=None):
            await elm.connect(log=log or (lambda *a: None))
            return elm

        monkeypatch.setattr(rd, "detect_adapter", fake_detect)
        store = Store(str(tmp_path / f"{vehicle}.db"))
        reader = rd.Reader(interval=0.02, adapter_pref="replay", store=store)

        async def go():
            task = asyncio.ensure_future(reader.run())
            await asyncio.sleep(seconds)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(go())
        with open(tmp_path / "state.json") as f:
            state = json.load(f)
        return reader, store, state, elm

    yield boot
    rd.set_vehicle("leaf_ze0")


# ── the Leaf ─────────────────────────────────────────────────────────────

def test_leaf_replay_reaches_the_state_file(replay_env):
    reader, store, state, elm = replay_env("leaf_ze0")
    assert state["status"] == "ok"
    assert reader.readings >= 3

    # values decoded from tests/fixtures/lbc_raw_20260824.json, through the
    # real profile decode() — the same numbers test_decoders pins.
    assert state["soc"] == pytest.approx(76.87, abs=0.01)
    assert state["pack_v"] == pytest.approx(383.87, abs=0.01)
    assert state["temps_f"] == [93.2, 95.0, 93.2, 96.8]
    assert state["cell_count"] == 96
    assert state["cell_spread"] == 30
    assert state["soh"] == pytest.approx(35.1, abs=0.1)

    # Car-CAN passive decode ran too (probe_20260824_185139.json: Park, A/C on)
    assert state["gear"] == "P"
    assert len(state["tpms_psi"]) == 4
    assert state["hvac_ac_on"] is True


def test_leaf_replay_is_labelled_as_replay_everywhere(replay_env):
    reader, store, state, elm = replay_env("leaf_ze0")
    assert state["replay"] is True
    assert state["replay_synthetic"] is False
    assert state["replay_fixture"] == "session_leaf_ze0.json"
    assert state["adapter_type"] == "replay"
    assert "replay" in state["adapter_name"]


def test_leaf_replay_writes_rows_and_cells_to_the_store(replay_env):
    reader, store, state, elm = replay_env("leaf_ze0")
    assert store.count() == reader.readings
    hist = store.history()
    assert hist and hist[-1]["soc"] == pytest.approx(76.87, abs=0.01)
    assert hist[-1]["temp_avg_f"] == pytest.approx(94.6, abs=0.2)
    cells = store.cell_history(limit=1)
    assert cells["cells"] and len(cells["cells"][-1]) == 96


def test_leaf_replay_exercises_the_real_scheduler(replay_env):
    """Slow-lane items get polled too, not just the fast lane."""
    reader, store, state, elm = replay_env("leaf_ze0", seconds=0.5)
    assert "lbc01" in reader.item_last                     # fast lane
    assert set(reader.item_last) - {"lbc01"}, "no slow-lane item ever ran"
    assert state["item_age"]["lbc01"] >= 0


def test_missing_group_shows_as_absent_not_invented(replay_env):
    """HVAC group 00 is not in the capture. The dashboard must show nothing
    for it — a replay that filled the hole would be lying about a car."""
    reader, store, state, elm = replay_env("leaf_ze0", seconds=0.5)
    assert any(m.endswith(":2100") for m in elm.misses)
    assert "hvac_g00_raw" not in state


# ── the Lancer ───────────────────────────────────────────────────────────

def test_lancer_replay_reaches_the_state_file(replay_env):
    reader, store, state, elm = replay_env("lancer_2009")
    assert state["status"] == "ok"
    assert reader.readings >= 3
    assert state["rpm"] == pytest.approx(710.8, abs=0.5)
    assert state["coolant_temp_c"] == 99
    assert state["coolant_temp_f"] == pytest.approx(210.2, abs=0.1)
    assert state["module_v"] == pytest.approx(13.916, abs=0.01)
    assert state["speed_mph"] == 0.0
    assert state["replay"] is True


def test_lancer_replay_decodes_dtcs_from_both_ecus(replay_env):
    reader, store, state, elm = replay_env("lancer_2009", seconds=0.5)
    assert state["mil_on"] is True
    assert state["dtc_count"] == 12
    assert "P0171" in state["dtc_stored"]


def test_lancer_replay_writes_rows_to_the_store(replay_env):
    reader, store, state, elm = replay_env("lancer_2009")
    assert store.count() == reader.readings
    hist = store.history()
    assert hist[-1]["rpm"] == pytest.approx(711, abs=1)


def test_lancer_replay_never_shows_leaf_signals(replay_env):
    reader, store, state, elm = replay_env("lancer_2009")
    for leafism in ("soc", "pack_v", "cell_spread", "temps_f"):
        assert leafism not in state


# ── the knobs ────────────────────────────────────────────────────────────

def test_speed_makes_a_short_capture_drive_a_long_session(replay_env):
    """A 10-second capture at 20x walks its whole timeline in half a second."""
    slow, _, _, slow_elm = replay_env("leaf_ze0", seconds=0.3, speed=1.0)
    fast, _, _, fast_elm = replay_env("leaf_ze0", seconds=0.3, speed=20.0)
    assert fast_elm.max_frame > slow_elm.max_frame


def test_replay_survives_a_fixture_with_one_frame(replay_env):
    reader, store, state, elm = replay_env("lancer_2009", seconds=0.3)
    assert len(elm.frames) == 1 and reader.readings >= 2


# ── replay stays out of the real data ────────────────────────────────────

def test_replay_files_are_per_profile_and_never_the_real_ones():
    """Two profiles have different `readings` columns, and neither may land in
    web/leaf_battery.db or overwrite the last real reading."""
    rd.set_vehicle("leaf_ze0")
    leaf_db, leaf_state = rd.replay_db(), rd.replay_state()
    rd.set_vehicle("lancer_2009")
    lancer_db, lancer_state = rd.replay_db(), rd.replay_state()
    rd.set_vehicle("leaf_ze0")

    assert leaf_db != lancer_db and leaf_state != lancer_state
    assert os.path.basename(leaf_db) == "replay_leaf_ze0.db"
    assert os.path.basename(lancer_state) == "replay_lancer_2009_state.json"
    for p in (leaf_db, lancer_db, leaf_state, lancer_state):
        assert "leaf_battery.db" not in p
        assert os.path.basename(p) != "battery_state.json"


def test_replay_paths_are_gitignored():
    """A replay run must not show up as a change to commit."""
    with open(os.path.join(os.path.dirname(FIXTURES), "..", ".gitignore")) as f:
        ignored = f.read()
    assert "web/replay_*.db" in ignored
    assert "web/replay_*_state.json" in ignored
