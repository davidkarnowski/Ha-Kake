# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The simulated pull: the scenario, `hakake_sim.py --pull`, and the fixture.

Pinned: the peak current of the pull sits in the band the owner observed
(−250..−270 A, reported −271 A / −89 kW at 54 mph on 2026-09-08 — OWNER
REPORT, one reading); the three artefacts carry their provenance (the
database's meta table, every row's stamp, the fixture's `synthetic` flag and
a note saying every EV-CAN byte is ASSERTED); Playback finds the pull; the
shipped fixture replays. Nothing here is evidence about the car.
"""

import json
import os
import sqlite3

import pytest

from conftest import ROOT, FIXTURES  # noqa: E402

import elm327  # noqa: E402
from elm327 import ReplayELM, set_uds_target, passive_capture, load_replay_fixture  # noqa: E402
from simulator import make_sim, scenario_names  # noqa: E402
from simulator import pull as pl  # noqa: E402
from store import Store  # noqa: E402

import asyncio


def run(coro):
    return asyncio.run(coro)


SHIPPED = os.path.join(FIXTURES, "session_leaf_ze0_pull_sim.json")


def test_the_pull_scenario_ships_and_reaches_the_observed_peak():
    assert "pull" in scenario_names()
    sim = make_sim(vehicle="leaf_ze0", scenario="pull", seed=1)
    peak = (0.0, 0.0, 0.0)
    regen = 0.0
    for _ in range(340):
        sim.step(0.1)
        st = sim.state()
        if st["current_a"] < peak[1]:
            peak = (sim.model.t, st["current_a"], st["speed_mph"], st["power_kw"], st["cell_min"])
        regen = max(regen, st["current_a"])
    t, amps, mph, kw, cell_min = peak
    assert -290 <= amps <= -240, amps                   # the owner's −271 A, with a margin
    assert 45 <= mph <= 55 and kw < -80
    assert 4.0 <= t <= 14.0                             # reached during the eight-second launch
    assert cell_min < 3700                              # the cells sag under it
    assert regen > 20                                   # and the brake puts something back
    assert sim.state()["speed_mph"] == 0 and sim.get_knobs()["gear"] == "P"


@pytest.fixture(scope="module")
def pulled(tmp_path_factory):
    d = tmp_path_factory.mktemp("pull")
    sm = pl.run_pull(out_db=str(d / "pull.db"), jsonl=str(d / "pull.jsonl"),
                     fixture=str(d / "fixture.json"), seed=1, log=lambda *a: None)
    return sm


def test_the_database_side_is_stamped_and_playback_finds_the_pull(pulled):
    assert pulled["simulated"] is True and pulled["rows"] > 50
    assert pulled["peak"]["current_a"] < -240
    ro = sqlite3.connect(f"file:{pulled['db']}?mode=ro", uri=True)
    meta = dict(ro.execute("SELECT key, value FROM meta").fetchall())
    ro.close()
    assert meta["synthetic"] == "true" and "hakake_sim --pull" in meta["warning"]
    st = Store(pulled["db"], vehicle="leaf_ze0")
    try:
        t0, t1 = st.conn.execute("SELECT MIN(ts_epoch), MAX(ts_epoch) FROM readings").fetchone()
        pulls = st.pulls(t0, t1)
        assert len(pulls) == 1 and pulls[0]["peak_a"] < -240 and pulls[0]["n"] > 10
        assert pulls[0]["label"].startswith("pull -")
        fr = st.frames(t0, t1, cells=True)
        assert len(fr["cells_at"]) == len(fr["t"]) == pulled["rows"]     # the cell log: every row has cells
        recs = fr["records"]
        assert all(r["simulated"] for r in recs)
        assert min(min(r["cells"]) for r in recs) < 3700
        assert st.conn.execute("SELECT COUNT(*) FROM events WHERE name='gear'").fetchone()[0] >= 2
        assert st.conn.execute("SELECT adapter FROM sessions").fetchone()[0] == "sim"
    finally:
        st.close()
    assert os.path.exists(pulled["state"])
    with open(pulled["state"]) as f:
        state = json.load(f)
    assert state["simulated"] is True and state["adapter_type"] == "sim"


def test_the_stream_is_the_mqtt_message_format_with_offset_times(pulled):
    with open(pulled["jsonl"]) as f:
        first = json.loads(f.readline())
        rest = [json.loads(l) for l in f]
    assert set(first) == {"topic", "payload"}
    assert first["topic"].startswith("hakake/sim/") and "/rx/" in first["topic"]
    p = first["payload"]
    assert p["t"] == 0.0 and p["id"] == first["topic"].rsplit("/", 1)[-1]
    assert all(len(b) == 2 for b in p["d"].split())
    topics = {m["topic"] for m in rest}
    assert any("/car/rx/421" in t for t in topics) and any("/ev/rx/1DB" in t for t in topics)
    assert any(t.endswith("/car/tx/uds") for t in topics)
    acks = [m for m in rest if "/tx/uds/" in m["topic"]]
    assert acks and all(m["payload"]["ok"] and m["payload"]["frames"] > 0 for m in acks)
    assert pulled["frames_car"] > pulled["frames_ev"] > 0
    assert max(m["payload"].get("t", 0) for m in rest) <= pulled["duration_s"] + 0.01


def test_the_fixture_is_synthetic_thinned_and_says_ev_can_is_asserted(pulled):
    doc = load_replay_fixture(pulled["fixture"])
    assert doc["synthetic"] is True and doc["vehicle"] == "leaf_ze0"
    assert "ASSERTED" in doc["notes"] and "EV-CAN" in doc["notes"] and "SYNTHETIC" in doc["notes"]
    assert "compare_sessions" in doc["notes"]
    frames = doc["frames"]
    assert 40 < len(frames) < 200
    assert frames[0]["t"] == 0.0
    with_uds = [fr for fr in frames if fr["uds"].get("79B", {}).get("2101")]
    assert len(with_uds) > len(frames) // 2
    assert any(fr["uds"].get("79B", {}).get("2102") for fr in frames)          # the cell log
    assert any(fr["uds"].get("744", {}).get("2110") for fr in frames)          # the HVAC amp
    for fr in frames:
        for cid, lines in fr["passive"].items():
            # a stride of k leaves ceil(n/k) per bucket, plus one where a bucket edge falls between strides
            assert len(lines) <= pl.FIXTURE_LINES_PER_BUCKET + 1, (cid, len(lines))
    ids = {cid for fr in frames for cid in fr["passive"]}
    assert "1DB" in ids and "421" in ids and "284" in ids
    assert "002" not in ids and "1DA" not in ids                                  # thinned to what is decoded + 1DB
    assert os.path.getsize(pulled["fixture"]) < 400_000


def test_the_pull_is_deterministic(tmp_path):
    a = pl.run_pull(out_db=str(tmp_path / "a.db"), jsonl=str(tmp_path / "a.jsonl"),
                    fixture=str(tmp_path / "a.json"), seed=1, log=lambda *a: None)
    b = pl.run_pull(out_db=str(tmp_path / "b.db"), jsonl=str(tmp_path / "b.jsonl"),
                    fixture=str(tmp_path / "b.json"), seed=1, log=lambda *a: None)
    with open(a["jsonl"], "rb") as fa, open(b["jsonl"], "rb") as fb:
        assert fa.read() == fb.read()
    with open(a["fixture"]) as fa, open(b["fixture"]) as fb:
        da, db = json.load(fa), json.load(fb)
    da.pop("captured"), db.pop("captured")
    assert da == db


def test_the_pull_refuses_the_real_database(tmp_path):
    import store as store_mod
    with pytest.raises(ValueError, match="real database"):
        pl.run_pull(out_db=store_mod.DEFAULT_DB, jsonl=str(tmp_path / "x.jsonl"), write_fixture=False)
    with pytest.raises(ValueError, match="real database"):
        pl.run_pull(out_db=str(tmp_path / os.path.basename(store_mod.DEFAULT_DB)),
                    jsonl=str(tmp_path / "x.jsonl"), write_fixture=False)


def test_the_shipped_fixture_is_the_generated_one_and_replays():
    assert os.path.exists(SHIPPED), "python hakake_sim.py --pull writes it"
    doc = load_replay_fixture(SHIPPED)
    assert doc["synthetic"] is True and "ASSERTED" in doc["notes"]
    e = ReplayELM(path=SHIPPED, loop=False)
    run(e.connect(log=lambda *a: None))
    try:
        assert e.synthetic is True
        run(set_uds_target(e, "79B", "7BB"))
        lines = run(e.send("2101"))
        assert lines and lines[0].startswith("7BB 10 ")
        gear = run(passive_capture(e, "421", 0.2))
        assert gear and gear[0].startswith("421 ")
        ev = run(passive_capture(e, "1DB", 0.2, set_caf=False))
        assert ev and ev[0].startswith("1DB ")
    finally:
        run(e.close())


def test_hakake_sim_pull_cli(tmp_path):
    import subprocess, sys
    p = subprocess.run([sys.executable, os.path.join(ROOT, "hakake_sim.py"), "--pull",
                        "--out", str(tmp_path / "c.db"), "--jsonl", str(tmp_path / "c.jsonl"),
                        "--fixture", str(tmp_path / "c.json"), "--json"],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert p.returncode == 0, p.stderr
    sm = json.loads(p.stdout.strip().splitlines()[-1])
    assert sm["simulated"] is True and sm["peak"]["current_a"] < -240
    assert os.path.exists(sm["fixture"]) and os.path.exists(sm["db"]) and os.path.exists(sm["jsonl"])
    assert "ASSERTED" in sm["warning"]
