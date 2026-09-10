# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""tools/compare_sessions.py — the expected pull against the observed one.

Two synthetic inputs of every kind the tool accepts (a fixture, a
database, a stream), the metrics on a hand-made series with known answers,
and the report shape. The tool is what the owner runs when the CANable
arrives; here both sides are simulated, so the only claim is that the
arithmetic is right.
"""

import datetime as dt
import json
import os
import sys

import pytest

from conftest import ROOT  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))

import compare_sessions as cs  # noqa: E402
from simulator import make_sim, encode  # noqa: E402
from simulator import canbus as cb  # noqa: E402
from simulator.pull import StreamWriter  # noqa: E402
from store import Store  # noqa: E402


def series(samples, **kw):
    d = {"source": "test", "kind": "fixture", "synthetic": True, "vehicle": "leaf_ze0",
         "samples": samples, "periods": {}, "counts": {}}
    d.update(kw)
    return d


def test_metrics_on_a_known_series():
    s = []
    for t in range(0, 20):
        i = {4: -60.0, 5: -150.0, 6: -250.0, 7: -240.0, 8: -100.0, 9: -30.0}.get(t, -1.0)
        s.append({"t": 100.0 + t, "current_a": i, "pack_v": 380.0 + i * 0.15, "soc": 70.0 - t * 0.1,
                  "cell_min": 3950 + int(i * 1.5), "cell_min_no": 56 if i < -200 else 12,
                  "speed_mph": max(0, (t - 3) * 8) if 3 < t < 9 else 0})
    m = cs.metrics(series(s), amps=40)
    assert m["peak_a"] == -250.0 and m["t_peak_s"] == 6.0 and m["peak_speed_mph"] == 24
    assert m["pull_start_s"] == 4.0 and m["time_to_peak_s"] == 2.0 and m["pull_duration_s"] == 5.0
    assert m["rest_v"] == pytest.approx(380.0 - 0.15) and m["min_v"] == pytest.approx(380 - 37.5)
    assert m["sag_v"] == pytest.approx(37.35, abs=0.01)
    assert m["min_cell_mv"] == 3950 - 375 and m["min_cell_pair"] == 56 and m["t_min_cell_s"] == 6.0
    assert m["soc_drop"] == pytest.approx(1.9) and m["soc_drop_pull"] == pytest.approx(0.5)
    assert m["peak_kw"] == pytest.approx(-250 * (380 - 37.5) / 1000, abs=0.01)
    assert m["samples"] == 20 and m["span_s"] == 19.0


def test_metrics_without_a_pull_or_without_data():
    flat = [{"t": t, "current_a": -1.0, "pack_v": 380.0, "soc": 70.0} for t in range(5)]
    m = cs.metrics(series(flat))
    assert m["pull_start_s"] is None and m["time_to_peak_s"] is None and m["sag_v"] == 0
    assert cs.metrics(series([{"t": 0}]))["error"]


def test_compare_lists_the_deltas_and_the_periods():
    a = [{"t": t, "current_a": -250.0 if t == 5 else -10.0 if t > 3 else -1.0, "pack_v": 380.0, "soc": 70.0}
         for t in range(10)]
    b = [{"t": t, "current_a": -230.0 if t == 6 else -10.0 if t > 3 else -1.0, "pack_v": 378.0, "soc": 69.0}
         for t in range(10)]
    r = cs.compare(series(a, periods={"1DB": 0.01}, counts={"1DB": 500}),
                   series(b, periods={"1DB": 0.0102, "421": 0.061}, counts={"1DB": 490, "421": 80}), amps=40)
    rows = {x["metric"]: x for x in r["rows"]}
    assert rows["peak current (A)"]["expected"] == -250.0 and rows["peak current (A)"]["observed"] == -230.0
    assert rows["peak current (A)"]["delta"] == 20.0
    assert rows["time to peak (s)"]["delta"] == 1.0
    per = {p["id"]: p for p in r["periods"]}
    assert per["1DB"]["survey_s"] == 0.01 and per["1DB"]["observed_s"] == 0.0102 and per["1DB"]["expected_n"] == 500
    assert per["421"]["survey_s"] == 0.06 and per["421"]["expected_s"] is None


# ── the three input kinds ────────────────────────────────────────────────

def _lbc_lines(sim):
    st = sim.state()
    return {"2101": encode.lbc_response("2101", st), "2102": encode.lbc_response("2102", st)}


def _fixture_doc(states, synthetic=True):
    frames = []
    for t, sim in states:
        st = sim.state()
        frames.append({"t": t, "uds": {"79B": _lbc_lines(sim)},
                       "passive": {"1DB": [encode.line("1DB", cb.enc_1db(st))],
                                   "284": [encode.frame_line("284", st)] * 3}})
    return {"hakake_replay": 1, "vehicle": "leaf_ze0", "title": "t", "adapter": "test",
            "synthetic": synthetic, "captured": "2026-01-01T00:00:00Z", "source": [], "notes": "",
            "frames": frames}


@pytest.fixture
def two_fixtures(tmp_path):
    def states(peak_pedal):
        out = []
        sim = make_sim(vehicle="leaf_ze0", knobs={"noise": 0, "soc": 70, "gear": "D", "start_state": "ready"}, seed=1)
        for t in range(0, 12):
            pedal = peak_pedal if 4 <= t <= 7 else 0.0
            sim.set(speed_mph=min(50.0, max(0.0, (t - 3) * 10.0)) if t > 3 else 0.0, accel_pedal_pct=pedal)
            out.append((float(t), sim))
            sim.step(1.0)
        return out
    a, b = tmp_path / "expected.json", tmp_path / "observed.json"
    with open(a, "w") as f:
        json.dump(_fixture_doc(states(100.0)), f)
    with open(b, "w") as f:
        json.dump(_fixture_doc(states(70.0), synthetic=False), f)
    return str(a), str(b)


def test_load_fixture_decodes_through_the_profile_and_1db(two_fixtures, leaf_profile):
    exp = cs.load_series(two_fixtures[0])
    assert exp["kind"] == "fixture" and exp["synthetic"] is True and exp["vehicle"] == "leaf_ze0"
    s = exp["samples"]
    assert len(s) == 12 and s[0]["current_a"] is not None and s[0]["pack_v"] > 300
    assert s[0]["cell_min"] and s[0]["cell_min_no"] in range(1, 97)
    assert s[5]["ev_current_a"] == pytest.approx(s[5]["current_a"], abs=0.5)   # 0x1DB agrees with group 01
    assert s[5]["speed_mph"] == pytest.approx(20.0, abs=0.5)
    assert exp["counts"]["284"] == 36 and exp["frame_width_s"] == 1.0
    m = cs.metrics(exp)
    assert m["peak_a"] < -100 and m["pull_start_s"] == 4.0 and m["ev_peak_a"] < -100


def test_the_cli_compares_two_fixtures(two_fixtures, capsys, leaf_profile):
    assert cs.main([two_fixtures[0], two_fixtures[1]]) == 0
    out = capsys.readouterr().out
    assert "expected:" in out and "(SYNTHETIC)" in out and "peak current (A)" in out
    assert "0x1DB peak (A, ASSERTED bytes)" in out
    assert "consistency, not verification" in out
    assert cs.main([two_fixtures[0], two_fixtures[1], "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    rows = {r["metric"]: r for r in j["rows"]}
    assert rows["peak current (A)"]["delta"] > 0            # the softer pull drew less
    assert j["expected_synthetic"] is True and j["observed_synthetic"] is False


def test_load_db_reads_the_rows_playback_would(tmp_path, leaf_profile):
    st = Store(str(tmp_path / "obs.db"), vehicle="leaf_ze0")
    base = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.timezone.utc)
    sim = make_sim(vehicle="leaf_ze0", knobs={"noise": 0, "soc": 60}, seed=2)
    for t in range(8):
        sim.set(current_a=-200.0 if t == 4 else 0.0)
        rec = sim.record(cells=(t % 2 == 0))
        st.insert_reading(rec, ts=base + dt.timedelta(seconds=t), adapter="sim")
    st.close()
    obs = cs.load_series(str(tmp_path / "obs.db"))
    assert obs["kind"] == "db" and obs["vehicle"] == "leaf_ze0" and len(obs["samples"]) == 8
    m = cs.metrics(obs)
    assert m["peak_a"] < -190 and m["t_peak_s"] == 4.0 and m["min_cell_mv"] is not None
    assert m["pull_start_s"] == 4.0


def test_load_stream_measures_periods_from_the_timestamps(tmp_path, leaf_profile):
    path = str(tmp_path / "s.jsonl")
    sim = make_sim(vehicle="leaf_ze0", knobs={"noise": 0, "soc": 65, "gear": "D", "start_state": "ready"}, seed=1)
    sched = cb.FrameSchedule("car", bus_load=0.0, t0=0.0)
    ev = cb.FrameSchedule("ev", bus_load=0.0, t0=0.0)
    with StreamWriter(path) as sw:
        t = 0.0
        n = 0
        while t < 3.0:
            st = sim.state()
            for cid in sched.due(t):
                sw.frame("car", t, cid, cb.frame_bytes("car", cid, st))
            for cid in ev.due(t):
                sw.frame("ev", t, cid, cb.frame_bytes("ev", cid, st))
            if abs(t - round(t)) < 1e-9:
                n += 1
                sw.uds("car", t, f"{n:04x}", "79B", "7BB", "2101",
                       cb.frames_of_lines(encode.lbc_response("2101", st)))
            if 1.0 <= t < 2.0:
                sim.set(accel_pedal_pct=80.0, speed_mph=30.0)
            else:
                sim.set(accel_pedal_pct=0.0, speed_mph=0.0)
            sim.step(0.01)
            t = round(t + 0.01, 6)
    s = cs.load_series(path)
    assert s["kind"] == "stream"
    assert s["periods"]["1DB"] == pytest.approx(0.01, abs=0.001)
    assert s["periods"]["421"] == pytest.approx(0.06, abs=0.001)
    assert s["counts"]["1DB"] == pytest.approx(300, abs=2)
    m = cs.metrics(s)
    assert m["peak_a"] < -100 and m["ev_peak_a"] < -100


def test_format_report_handles_missing_values():
    r = cs.compare(series([{"t": 0, "current_a": -1.0}]), series([{"t": 0, "current_a": -2.0, "pack_v": 300.0}]))
    text = cs.format_report(r, series([]), series([], synthetic=False))
    assert "—" in text and "peak current (A)" in text
    assert cs._fmt(None) == "—" and cs._fmt(0.5) == "0.5" and cs._fmt(1234.5) == "1,234.5"
