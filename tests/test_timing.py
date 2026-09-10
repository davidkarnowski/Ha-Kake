# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The timing architecture (docs/TIMING.md), without a car:

  * acquisition time per item — `item_ts` / `item_ts_epoch` in the record,
    `item_ts_epoch` in the stored row, `item_ts` rebuilt by playback;
  * two clocks — a transport with a source clock (the native CAN façade, MQTT)
    gives `frame_ts` per passive item and `clock_offset_s`, stored on the
    session; an ELM gives neither;
  * `ts_source` on every row (laptop | driver | bridge), added to old databases;
  * peak-preserving decimation — `peak: True` keys keep min / max / time-of-each
    between stored rows, from decode() output only, reset on store; the pull
    detector sees the envelope;
  * the page's "read at" helper (playback.js, under node);
  * the bench tool's --timing self-test on replay.
"""
import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import types

import pytest

from conftest import ROOT, fixture  # noqa: E402

import reader as rd  # noqa: E402
import vehicles  # noqa: E402
import cantransport as ct  # noqa: E402
from store import Store  # noqa: E402

RAW = fixture("lbc_raw_20260824.json")["groups"]
PLAYBACK_JS = os.path.join(ROOT, "web", "static", "playback.js")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run(coro):
    return asyncio.run(coro)


class FakeELM:
    """An ELM that answers the LBC groups from the fixture and nothing else."""
    adapter_type = "fake"
    adapter_name = "FakeELM 1"
    adapter_port = "mem"

    def __init__(self):
        self.target = None
        self.closed = False

    async def send(self, cmd, wait=0, timeout=0):
        up = cmd.upper()
        if up.startswith("ATSH"):
            self.target = up.split()[-1]
            return []
        if up == "ATI":
            return ["ELM327 v1.5"]
        if up.startswith("AT") or not cmd:
            return []
        return RAW.get(cmd, [])

    async def close(self):
        self.closed = True


class FakeNative(FakeELM):
    """The same, wearing what the native CAN façade exposes: a frame table with
    both clocks per id, a source-clock offset, and whose clock it is."""
    adapter_type = "can"
    ts_source = "driver"
    SPEED = 0.05
    PASSIVE_INSTANT = True

    def __init__(self, table=None, offset=0.042):
        super().__init__()
        self.table = table or {}
        self.offset = offset

    def source_times(self):
        return dict(self.table)

    def clock_offset(self):
        return self.offset

    def marker(self):
        return {"can_bus": "car", "listen_only": False}


@pytest.fixture
def env(isolated_reader, leaf_profile, tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STORE_PERIOD", 0.0)
    monkeypatch.setattr(rd, "BACKOFF_MIN", 0.01)
    monkeypatch.setattr(rd, "BACKOFF_MAX", 0.02)
    monkeypatch.setattr(rd, "ASLEEP_INTERVAL", 0.01)
    store = Store(str(tmp_path / "t.db"))
    yield tmp_path, store
    store.close()


async def run_for(reader, seconds):
    task = asyncio.ensure_future(reader.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _iso_to_epoch(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ── the façade: two clocks ───────────────────────────────────────────────

def test_the_facade_keeps_both_clocks_and_reports_their_offset():
    src = ct.FrameSource()
    src.name = "fake source 1"
    elm = ct.CanFacade(src, settle=0, log=lambda *a: None)
    assert elm.clock_offset() is None                    # nothing seen yet
    assert elm.ts_source == "driver"                     # python-can's clock, by default
    for _ in range(5):
        elm._on_frame(time.time() - 0.050, "421", b"\x08\x00\x00", {})
    wall, src_t = elm.source_times()["421"]
    assert abs((wall - src_t) - 0.050) < 0.02
    assert abs(elm.clock_offset() - 0.050) < 0.02
    # a frame with no source timestamp (t = 0) is not a clock sample
    before = len(elm.clock_samples)
    elm._on_frame(0.0, "358", b"\x00", {})
    assert len(elm.clock_samples) == before and elm.source_times()["358"][1] == 0.0
    assert elm.freshness()["421"] < 0.1                  # the monotonic table is untouched


def test_the_source_says_whose_clock_it_is():
    import mqttsource as ms
    assert ct.FrameSource.ts_source == "driver"
    assert ms.MqttSource.ts_source == "bridge"
    src = ct.FrameSource()
    src.ts_source = "bridge"
    assert ct.CanFacade(src, settle=0, log=lambda *a: None).ts_source == "bridge"


# ── the reader: acquisition time per item ────────────────────────────────

def test_every_item_is_stamped_with_when_it_was_read(env):
    tmp_path, store = env
    r = rd.Reader(interval=0, adapter_pref=None, store=store)
    t0 = time.time()
    rec, alive = run(r.poll_once(FakeELM()))
    assert alive is True
    for i in rec["timing"]:
        e = rec["item_ts_epoch"][i]
        assert t0 - 0.01 <= e <= time.time() + 0.01
        assert abs(_iso_to_epoch(rec["item_ts"][i]) - e) < 0.002       # ISO ms ↔ epoch
        assert rec["item_ts"][i].endswith("Z")
    assert rec["ts_source"] == "laptop"                  # an ELM has no clock of its own
    assert "clock_offset_s" not in rec and "frame_ts" not in rec


def test_a_passive_item_takes_its_newest_frame_time_and_the_source_clock(env):
    tmp_path, store = env
    old_wall, old_src = time.time() - 1.5, time.time() - 1.542
    elm = FakeNative(table={"421": (old_wall, old_src), "358": (time.time(), 0.0)}, offset=0.042)
    r = rd.Reader(interval=0, adapter_pref="can", store=store)
    r.ts_source = elm.ts_source
    rec, _ = run(r.poll_once(elm))
    assert rec["item_ts_epoch"]["p421"] == pytest.approx(old_wall, abs=0.002)   # the frame's arrival, not the ATMA return
    assert rec["frame_ts"]["p421"] == pytest.approx(old_src, abs=0.002)         # the source's own clock
    assert "p358" not in rec["frame_ts"]                                       # no source timestamp → no frame_ts
    assert rec["item_ts_epoch"]["p358"] == pytest.approx(time.time(), abs=0.5)
    assert rec["clock_offset_s"] == 0.042 and rec["ts_source"] == "driver"
    assert rec["item_ts_epoch"]["lbc01"] == pytest.approx(time.time(), abs=0.5)  # UDS answers are stamped as they return


def test_stored_rows_carry_the_times_and_playback_rebuilds_them(env, monkeypatch):
    tmp_path, store = env

    async def fake_detect(prefer=None, log=None):
        return FakeELM()

    async def fake_configure(elm):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    r = rd.Reader(interval=0.01, adapter_pref=None, store=store)
    run(run_for(r, 0.3))
    assert store.count() >= 1
    row = store.conn.execute("SELECT ts_source, extra FROM readings ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ts_source"] == "laptop"
    ex = json.loads(row["extra"])
    assert "lbc01" in ex["item_ts_epoch"] and "item_ts" not in ex          # the ISO twin is rebuilt, not stored
    assert "ts_source" not in ex                                            # its own column
    now = time.time()
    fr = store.frames(now - 60, now + 60)
    last = fr["records"][-1]
    assert last["ts_source"] == "laptop" and last["playback"] is True
    assert abs(_iso_to_epoch(last["item_ts"]["lbc01"]) - last["item_ts_epoch"]["lbc01"]) < 0.002
    # an ELM session has no source clock to record
    assert store.conn.execute("SELECT clock_offset_s FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0] is None


def test_a_source_clock_offset_is_published_and_stored_on_the_session(env, monkeypatch):
    tmp_path, store = env
    elm = FakeNative(offset=0.031)

    async def fake_detect(prefer=None, log=None):
        return elm

    async def fake_configure(e):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    r = rd.Reader(interval=0.01, adapter_pref="can", store=store)
    run(run_for(r, 0.3))
    assert r.ts_source == "driver"
    with open(tmp_path / "state.json") as f:
        state = json.load(f)
    assert state["clock_offset_s"] == 0.031 and state["ts_source"] == "driver"
    assert store.conn.execute("SELECT ts_source FROM readings LIMIT 1").fetchone()[0] == "driver"
    assert store.conn.execute("SELECT clock_offset_s FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0] == 0.031
    # never applied: the row's time is this machine's clock, offset or not
    row = store.conn.execute("SELECT ts_epoch FROM readings ORDER BY id DESC LIMIT 1").fetchone()
    assert abs(row[0] - time.time()) < 5


# ── ts_source on an old database ─────────────────────────────────────────

def test_old_databases_gain_ts_source_and_the_session_offset_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE readings (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, ts_epoch REAL NOT NULL,
                               adapter TEXT, soc REAL, extra TEXT);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, started TEXT NOT NULL, ended TEXT,
                               adapter TEXT, note TEXT);
        INSERT INTO readings (ts, ts_epoch, soc) VALUES ('2026-08-25T00:00:00Z', 1.0, 42.0);
    """)
    c.commit(); c.close()
    s = Store(path, vehicle="leaf_ze0")
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(readings)")}
    assert "ts_source" in cols
    assert {r["name"] for r in s.conn.execute("PRAGMA table_info(sessions)")} >= {"clock_offset_s", "vehicle"}
    s.insert_reading({"soc": 43.0}, ts="2026-08-26T00:00:00Z", ts_source="laptop")
    s.insert_reading({"soc": 44.0, "ts_source": "bridge"}, ts="2026-08-26T00:00:05Z")
    s.insert_reading({"soc": 45.0}, ts="2026-08-26T00:00:10Z")
    got = [r[0] for r in s.conn.execute("SELECT ts_source FROM readings ORDER BY id")]
    assert got == [None, "laptop", "bridge", None]      # old rows stay NULL: saying "laptop" would be a guess
    sid = s.start_session("can")
    s.set_session_clock_offset(sid, 0.0123)
    assert s.conn.execute("SELECT clock_offset_s FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 0.0123
    s.close()


# ── peaks between rows ───────────────────────────────────────────────────

def test_peak_keys_come_from_the_profile_and_the_validator_checks_them(leaf_profile):
    assert vehicles.peak_keys(leaf_profile) == ["pack_v", "current_a", "power_kw", "cell_min"]
    lancer = vehicles.get_vehicle("lancer_2009")
    assert vehicles.peak_keys(lancer) == []
    # a stub profile with the Lancer's attributes and bad peak flags
    ns = types.SimpleNamespace(**{k: getattr(lancer, k) for k in dir(lancer) if not k.startswith("__")})
    ns.HISTORY_COLS = dict(lancer.HISTORY_COLS)
    ns.HISTORY_COLS["rpm"] = dict(ns.HISTORY_COLS["rpm"], peak="yes")
    ns.HISTORY_COLS["gear_text"] = {"kind": "text", "peak": True}
    ns.SIGNALS = dict(lancer.SIGNALS)
    first = next(iter(ns.SIGNALS))
    ns.SIGNALS[first] = dict(ns.SIGNALS[first], peak=1)
    problems = "\n".join(vehicles.validate_profile(ns))
    assert "HISTORY_COLS['rpm'] peak must be True or False" in problems
    assert "HISTORY_COLS['gear_text'] cannot be a peak" in problems
    assert f"signal '{first}' peak must be True or False" in problems
    # a SIGNALS peak is collected too
    ns2 = types.SimpleNamespace(HISTORY_COLS={}, SIGNALS={"x": {"peak": True}, "temps_f.0": {"peak": True}})
    assert vehicles.peak_keys(ns2) == ["x"]


def test_ts_source_is_a_reserved_history_column():
    """`ts_source` is one of the columns the store writes itself, and
    `docs/ADDING_A_VEHICLE.md` says a profile may not redefine it. Until
    2026-09-10 the validator did not check it, so a profile that declared the
    column would have produced it twice in the readings DDL and failed at
    CREATE TABLE with `duplicate column name`."""
    lancer = vehicles.get_vehicle("lancer_2009")
    for reserved in ("ts_source", "ts", "ts_epoch", "adapter", "vehicle", "extra", "id"):
        ns = types.SimpleNamespace(**{k: getattr(lancer, k) for k in dir(lancer) if not k.startswith("__")})
        ns.HISTORY_COLS = dict(lancer.HISTORY_COLS)
        ns.HISTORY_COLS[reserved] = {"kind": "text"}
        problems = "\n".join(vehicles.validate_profile(ns))
        assert f"may not redefine the built-in column {reserved!r}" in problems, reserved


def test_the_envelope_is_kept_from_decoded_values_only_and_reset_on_take(env, monkeypatch):
    tmp_path, store = env
    seq = iter([{"current_a": -10.0, "pack_v": 380.0, "soc": 50.0},
                {"current_a": -80.0, "pack_v": 351.5, "soc": 49.9},
                {},                                                 # nothing decoded: the sticky cache is not a sample
                {"current_a": -30.0, "pack_v": 372.0, "soc": 49.8}])
    monkeypatch.setattr(rd.VEHICLE, "decode", lambda responses: (next(seq), True))
    r = rd.Reader(interval=0, adapter_pref=None, store=store)
    elm = FakeELM()
    stamps = []
    for _ in range(4):
        rec, _ = run(r.poll_once(elm))
        stamps.append(rec["item_ts_epoch"]["lbc01"])
        assert "current_a_min" not in rec                      # the envelope rides with the stored row, not the state
    pk = r.take_peaks()
    assert pk["current_a_min"] == -80.0 and pk["current_a_max"] == -10.0 and pk["current_a_n"] == 3
    assert pk["current_a_tmin"] == pytest.approx(stamps[1], abs=0.002)   # timed by the item that produced it
    assert pk["current_a_tmax"] == pytest.approx(stamps[0], abs=0.002)
    assert pk["pack_v_min"] == 351.5 and pk["pack_v_max"] == 380.0
    assert "soc_min" not in pk and "power_kw_min" not in pk              # not a peak key / never decoded
    assert r.take_peaks() == {}                                          # reset


def test_the_stored_row_carries_the_envelope_and_the_next_starts_fresh(env, monkeypatch):
    tmp_path, store = env
    vals = iter([-5.0, -90.0, -20.0, -7.0, -6.0, -6.5, -6.0, -6.0, -6.0, -6.0, -6.0, -6.0])

    def decode(responses):
        v = next(vals, -6.0)
        return {"current_a": v, "pack_v": 380.0, "power_kw": round(380.0 * v / 1000, 3), "soc": 50.0}, True

    monkeypatch.setattr(rd.VEHICLE, "decode", decode)
    monkeypatch.setattr(rd, "STORE_PERIOD", 0.12)

    async def fake_detect(prefer=None, log=None):
        return FakeELM()

    async def fake_configure(elm):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    r = rd.Reader(interval=0.03, adapter_pref=None, store=store)
    run(run_for(r, 0.5))
    rows = [(x[0], json.loads(x[1])) for x in store.conn.execute("SELECT current_a, extra FROM readings ORDER BY id")]
    assert len(rows) >= 3
    # the first row goes out on the first cycle (one sample: -5); the -90 lands
    # between the first and second rows, and only the envelope carries it
    k, (sample, ex) = next((k, r) for k, r in enumerate(rows) if r[1].get("current_a_min") == -90.0)
    assert k >= 1 and sample > -90.0
    assert ex["current_a_max"] >= -20.0 and ex["current_a_n"] >= 2
    assert ex["power_kw_min"] == pytest.approx(-34.2, abs=0.01)
    assert all(r[1]["current_a_min"] > -90.0 for r in rows[k + 1:])       # reset after the row
    assert rows[0][1]["current_a_n"] == 1 and rows[0][1]["current_a_min"] == -5.0
    # the pull detector sees the true peak, at the peak's own time
    now = time.time()
    pulls = store.pulls(now - 60, now + 60, amps=40)
    assert len(pulls) == 1 and pulls[0]["peak_a"] == -90.0
    assert pulls[0]["t_peak"] == pytest.approx(ex["current_a_tmin"], abs=0.002)


def test_pulls_without_an_envelope_behave_as_before(tmp_store):
    t0 = dt.datetime(2026, 9, 9, 18, 0, tzinfo=dt.timezone.utc)
    for i, a in enumerate([-5, -50, -60, -5, -5]):
        tmp_store.insert_reading({"current_a": float(a), "power_kw": a * 0.38}, ts=t0 + dt.timedelta(seconds=5 * i))
    p = tmp_store.pulls(t0.timestamp() - 1, t0.timestamp() + 100, amps=40)
    assert len(p) == 1 and p[0]["peak_a"] == -60 and p[0]["n"] == 2
    # a row whose sample missed the peak but whose envelope caught it
    tmp_store.insert_reading({"current_a": -8.0, "power_kw": -3.0, "current_a_min": -120.0, "power_kw_min": -45.0,
                              "current_a_tmin": t0.timestamp() + 62.5},
                             ts=t0 + dt.timedelta(seconds=65))
    p = tmp_store.pulls(t0.timestamp() - 1, t0.timestamp() + 100, amps=40)
    assert len(p) == 2 and p[1]["peak_a"] == -120.0 and p[1]["peak_kw"] == -45.0
    assert p[1]["t_peak"] == t0.timestamp() + 62.5


# ── the page's helper, from node ─────────────────────────────────────────

HARNESS = """
  globalThis.window = globalThis; require(process.argv[1]);
  const P = window.Playback, out = [];
"""


def run_node(script):
    r = subprocess.run(["node", "-e", script, PLAYBACK_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_item_age_measures_from_now_live_and_from_the_frame_in_playback():
    out = run_node(HARNESS + """
      const live = {timestamp: '2026-09-09T18:00:00Z', item_ts_epoch: {lbc01: 1000.0, lbc04: 940.0}, item_age: {lbc01: 9, lbc04: 9}};
      out.push(P.itemAge(live, 'lbc01', 1003500));           // 3.5 s after it was read
      out.push(P.itemAge(live, 'lbc04', 1003500));           // 63.5 s
      out.push(P.itemAge(live, 'lbc02', 1003500));           // unknown item, no item_age either → null
      const pb = {playback: true, timestamp: '1970-01-01T00:16:50Z', item_ts_epoch: {lbc01: 1000.0, lbc04: 940.0}};
      out.push(P.itemAge(pb, 'lbc01', 99999999));            // frame at 1010 s: 10 s, whatever now is
      out.push(P.itemAge(pb, 'lbc04', 99999999));            // 70 s
      const old = {item_age: {lbc01: 4.2}};                  // a record without item_ts: the reader's age as is
      out.push(P.itemAge(old, 'lbc01', 5000));
      out.push(P.itemAge({timestamp: '1970-01-01T00:16:50Z', item_ts_epoch: {lbc01: 1012.0}}, 'lbc01', null)); // clamped at 0
      out.push(P.fmtAge(3.4), P.fmtAge(125), P.fmtAge(7200), P.fmtAge(null), P.fmtAge(-1));
      console.log(JSON.stringify(out));
    """)
    assert out[:3] == [3.5, 63.5, None]
    assert out[3:5] == [10, 70]
    assert out[5] == 4.2 and out[6] == 0
    assert out[7:] == ["3s ago", "2m ago", "2.0h ago", "", ""]


def test_the_page_uses_the_helper_for_its_read_at_badges():
    page = open(os.path.join(ROOT, "web", "templates", "index.html"), encoding="utf-8").read()
    assert "Playback.fmtAge(Playback.itemAge(data, el.dataset.ageFor, ageNow))" in page


# ── the timing self-test ─────────────────────────────────────────────────

def test_the_bench_timing_mode_runs_the_reader_on_replay(isolated_reader, leaf_profile):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import bench_transport as bt
    res = run(bt.timing_run("replay", seconds=0.4, interval=0.02, log=lambda *a: None))
    assert res["adapter"]["type"] == "replay" and res["adapter"]["ts_source"] == "laptop"
    assert res["cycles"]["n"] >= 3 and res["cycles"]["period"]["n"] >= 2
    assert res["cycles"]["jitter_s"] is not None
    assert "lbc01" in res["items"] and res["items"]["lbc01"]["n"] >= 3
    assert "lbc01" in res["ages"]
    assert res["offset"] is None                                        # replay has no source clock
    assert res["rows"] == 0                                             # 0.4 s is under one STORE_PERIOD
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bt.print_timing(res)
    text = buf.getvalue()
    assert "rows stamped by the laptop clock" in text and "source clock: none" in text
    assert not os.path.exists(rd.STATE_FILE)                            # the self-test publishes nothing


def test_scheduling_is_monotonic_and_storage_is_wall_clock():
    """The audit, as a test: the scheduler's bookkeeping uses loop.time();
    rows and acquisition stamps use the wall clock; neither leaks into the other."""
    src = open(os.path.join(ROOT, "web", "reader.py"), encoding="utf-8").read()
    assert "self.item_last[i] = loop.time()" in src
    assert "self.item_ts[i] = now" in src and "now = time.time()" in src
    assert "self.store.insert_reading(row, ts=now, adapter=elm.adapter_type, ts_source=self.ts_source)" in src
    assert "now = dt.datetime.now(dt.timezone.utc)" in src
    assert "time.time() - self.item_last" not in src and "loop.time() - self.item_ts" not in src
