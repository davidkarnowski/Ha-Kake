# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store tests — in-memory SQLite, no car needed."""
import datetime as dt
import json
import os

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import leaf_decoders as ld  # noqa: E402
import vehicles  # noqa: E402
from store import Store, to_utc  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "lbc_raw_20260824.json")


@pytest.fixture(autouse=True)
def _restore_active():
    """The store binds the process-wide active profile; don't leak one test's
    choice into the next."""
    before = vehicles._active
    yield
    vehicles._active = before


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"), vehicle="leaf_ze0")
    yield s
    s.close()


@pytest.fixture
def rec():
    with open(FIXTURE) as f:
        return ld.decode_reading(json.load(f)["groups"])


def test_to_utc_naive_is_local():
    d = to_utc("2026-02-19T19:33:52.830540")
    assert d.tzinfo == dt.timezone.utc
    local = dt.datetime(2026, 2, 19, 19, 33, 52).astimezone()
    assert abs((d - local).total_seconds()) < 1


def test_to_utc_z():
    d = to_utc("2026-08-24T18:00:00Z")
    assert d.hour == 18 and d.tzinfo == dt.timezone.utc


def test_insert_and_latest(store, rec):
    rid = store.insert_reading(rec, ts="2026-08-24T18:00:00Z", adapter="ble")
    assert rid == 1
    assert store.count() == 1
    row = store.latest()
    assert row["soc"] == pytest.approx(76.87, abs=0.01)
    assert row["temp1_c"] == 34
    assert row["temp_avg_c"] == pytest.approx(34.75, abs=0.06)
    assert row["cell_min_idx"] == rec["cell_min_idx"]
    assert row["power_kw"] < 0
    assert row["discharging"] == 1
    extra = json.loads(row["extra"])
    assert "hv_current1_a" in extra and "cells" not in extra
    n_cells = store.conn.execute("SELECT COUNT(*) FROM cells WHERE reading_id=?", (rid,)).fetchone()[0]
    assert n_cells == 96


def test_history_shape_and_fahrenheit(store, rec):
    store.insert_reading(rec, ts=dt.datetime.now(dt.timezone.utc))
    h = store.history(minutes=60)
    assert len(h) == 1
    e = h[0]
    assert e["t"].endswith("Z")
    assert e["temp_avg"] == pytest.approx(34.8, abs=0.06)
    assert e["temp_avg_f"] == pytest.approx(94.6, abs=0.1)
    assert e["discharging"] is True
    assert e["capacity_ah"] == pytest.approx(23.157, abs=0.001)


def test_history_range_filter(store, rec):
    now = dt.datetime.now(dt.timezone.utc)
    store.insert_reading(rec, ts=now - dt.timedelta(hours=5))
    store.insert_reading(rec, ts=now - dt.timedelta(minutes=5))
    assert len(store.history(minutes=60)) == 1
    assert len(store.history(minutes=None)) == 2


def test_history_downsamples(store):
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(200):
        store.insert_reading({"soc": 50 + i * 0.1, "power_kw": -1.0}, ts=now - dt.timedelta(seconds=200 - i))
    h = store.history(minutes=10, max_points=20)
    assert 3 <= len(h) <= 25
    assert sum(e["n"] for e in h) == 200
    assert h[0]["soc"] < h[-1]["soc"]
    assert all(e["power_kw"] == -1.0 for e in h)


def test_daily_health(store, rec):
    store.insert_reading(rec, ts="2026-08-24T18:00:00Z")
    store.insert_reading(dict(rec, capacity_ah=24.0, soh=36.4), ts="2026-08-25T18:00:00Z")
    d = store.daily_health()
    assert [x["day"] for x in d] == ["2026-08-24", "2026-08-25"]
    assert d[1]["capacity_ah"] == 24.0
    assert d[0]["temp_avg_f"] == pytest.approx(94.6, abs=0.1)


def test_cell_history(store, rec):
    store.insert_reading(rec, ts="2026-08-24T18:00:00Z")
    store.insert_reading(rec, ts="2026-08-24T18:01:00Z")
    ch = store.cell_history(limit=5)
    assert len(ch["t"]) == 2 and len(ch["cells"][0]) == 96


def test_migrate_legacy(store, tmp_path):
    hist = [
        {"t": "2026-02-19T19:33:52", "soc": 74.75, "pack_v": 391.8, "spread": 32, "temp_avg": 17.8},
        {"t": "2026-02-19T19:34:52", "soc": 74.80, "pack_v": 391.9, "spread": 32, "power_kw": 2.0, "current_a": 5.0},
        {"t": "2026-02-19T19:35:52", "soc": 74.70, "pack_v": 391.0, "spread": 33, "power_kw": 0.4, "current_a": 1.0},
    ]
    hj = tmp_path / "h.json"
    hj.write_text(json.dumps(hist))
    jl = tmp_path / "log.jsonl"
    jl.write_text(json.dumps({"timestamp": "2026-02-15T23:01:54", "soc_pct": 73.03, "capacity_ah": 25.05,
                              "soh_pct": 38.0, "temps_degc": [20, 21, 19, 19], "cell_mv": [4000] * 96}) + "\n")
    n = store.migrate_legacy(str(hj), str(jl))
    assert n == 4
    assert store.migrate_legacy(str(hj), str(jl)) == 0  # idempotent
    rows = store.conn.execute("SELECT power_kw, discharging FROM readings WHERE power_kw IS NOT NULL ORDER BY ts_epoch").fetchall()
    assert rows[0][0] == 2.0 and rows[0][1] == 0     # SOC rose → charging → positive
    assert rows[1][0] == -0.4 and rows[1][1] == 1    # SOC fell → discharge → negative
    first = store.conn.execute("SELECT temp_avg_c, capacity_ah, cell_spread FROM readings ORDER BY ts_epoch LIMIT 1").fetchone()
    assert first[0] == 19.8 and first[1] == 25.05 and first[2] == 0


def test_promoted_columns_written(store, rec):
    rec = dict(rec, hvac_ac_on=True, hvac_compressor_rpm=1976, hvac_fan_speed=4,
               cabin_temp_c=24, gear="D", speed_mph=0)
    store.insert_reading(rec, ts="2026-08-24T18:00:00Z")
    row = store.latest()
    assert row["hvac_ac_on"] == 1 and row["hvac_compressor_rpm"] == 1976
    assert row["hvac_fan_speed"] == 4 and row["cabin_temp_c"] == 24
    assert row["gear"] == "D" and row["speed_mph"] == 0
    # promoted keys must NOT also live in extra
    import json as _j
    extra = _j.loads(row["extra"] or "{}")
    for k in ("hvac_ac_on", "hvac_compressor_rpm", "gear", "cabin_temp_c"):
        assert k not in extra


def test_ac_query_is_columnar(store, rec):
    for on, rpm in ((True, 2000), (False, 0), (True, 2500)):
        store.insert_reading(dict(rec, hvac_ac_on=on, hvac_compressor_rpm=rpm),
                             ts=dt.datetime.now(dt.timezone.utc))
    n = store.conn.execute("SELECT COUNT(*) FROM readings WHERE hvac_ac_on=1").fetchone()[0]
    avg = store.conn.execute("SELECT AVG(hvac_compressor_rpm) FROM readings WHERE hvac_ac_on=1").fetchone()[0]
    assert n == 2 and avg == 2250


def test_migrate_and_backfill_from_old_db(tmp_path):
    import store as st
    path = str(tmp_path / "old.db")
    # build a DB with the columns absent and hvac data only in `extra`
    import sqlite3
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE readings (id INTEGER PRIMARY KEY, ts TEXT, ts_epoch REAL,
        soc REAL, extra TEXT); CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);""")
    c.execute("INSERT INTO readings (ts, ts_epoch, soc, extra) VALUES (?,?,?,?)",
              ("2026-08-25T00:00:00Z", 1.0, 55.0,
               '{"hvac_ac_on": true, "hvac_compressor_rpm": 2598, "gear": "P", "cabin_temp_c": 22}'))
    c.commit(); c.close()
    s = st.Store(path, vehicle="leaf_ze0")   # triggers migration + backfill
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(readings)")}
    assert {"hvac_ac_on", "hvac_compressor_rpm", "gear", "cabin_temp_c"} <= cols
    row = s.conn.execute("SELECT hvac_ac_on, hvac_compressor_rpm, gear, cabin_temp_c FROM readings").fetchone()
    assert row["hvac_ac_on"] == 1 and row["hvac_compressor_rpm"] == 2598
    assert row["gear"] == "P" and row["cabin_temp_c"] == 22
    # idempotent: reopening doesn't error or double-run
    n0 = s.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    s.close(); s2 = st.Store(path, vehicle="leaf_ze0")
    assert s2.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == n0
    s2.close()


def test_events_on_time_ignores_sample_spacing(store):
    # A/C on at t=1000, off at t=1120 — no readings between; on_time must be 120s
    store.insert_event("hvac_ac_on", True, None, ts=dt.datetime.fromtimestamp(1000, dt.timezone.utc))
    store.insert_event("hvac_ac_on", False, True, ts=dt.datetime.fromtimestamp(1120, dt.timezone.utc))
    assert store.on_time("hvac_ac_on", 1000, 1200) == 120
    # window starting after the 'on' event still sees the state carried in
    assert store.on_time("hvac_ac_on", 1050, 1200) == 70


def test_events_query_and_prev(store):
    for v, p, t in ((True, None, 10), (False, True, 20), (True, False, 30)):
        store.insert_event("hvac_ac_on", v, p, ts=dt.datetime.fromtimestamp(t, dt.timezone.utc))
    evs = store.events("hvac_ac_on")
    assert [e["value"] for e in evs] == ["1", "0", "1"]
    assert [e["prev"] for e in evs] == [None, "1", "0"]
    assert store.on_time("hvac_ac_on", 0, 40) == 10 + 10   # on 10-20 and 30-40


# ── the schema is the profile's declaration ──────────────────────────────

# Exactly the readings columns every existing database was created with,
# before HISTORY_COLS existed. The Leaf profile must still produce these.
LEGACY_READINGS = """CREATE TABLE readings (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    ts_epoch REAL NOT NULL,
    adapter TEXT,
    soc REAL, pack_v REAL, current_a REAL, power_kw REAL, discharging INTEGER,
    capacity_ah REAL, soh REAL, hx REAL, lv_volts REAL, insulation_kohm INTEGER,
    temp1_c REAL, temp2_c REAL, temp3_c REAL, temp4_c REAL, temp_avg_c REAL,
    cell_min INTEGER, cell_max INTEGER, cell_avg INTEGER, cell_spread INTEGER,
    cell_min_idx INTEGER, cell_max_idx INTEGER,
    balancing_active INTEGER,
    hvac_ac_on INTEGER, hvac_compressor_rpm INTEGER, hvac_on INTEGER,
    hvac_fan_on INTEGER, hvac_fan_speed INTEGER, hvac_heater_level INTEGER,
    cabin_temp_c REAL, hvac_ambient_c REAL, hvac_evap_c REAL,
    gear TEXT, speed_mph REAL,
    extra TEXT
)"""


def _cols(conn, table="readings"):
    return {r["name"]: r["type"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_leaf_schema_is_unchanged(store):
    """A fresh Leaf database must have exactly the columns (and SQL types) the
    hardcoded schema produced, plus the new `vehicle` stamp."""
    import sqlite3
    ref = sqlite3.connect(":memory:")
    ref.row_factory = sqlite3.Row
    ref.execute(LEGACY_READINGS)
    want = _cols(ref)
    got = _cols(store.conn)
    assert got.pop("vehicle") == "TEXT"
    assert got == want
    idx = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='readings'")}
    assert {"idx_readings_epoch", "idx_readings_ac"} <= idx


def test_profile_columns_are_created_and_populated(tmp_path):
    """A profile that declares its own history columns gets real, queryable
    columns — the Lancer can chart a coolant temperature without touching the
    store."""
    s = Store(str(tmp_path / "lancer.db"), vehicle="lancer_2009")
    cols = _cols(s.conn)
    assert cols["coolant_temp_c"] == "REAL" and cols["rpm"] == "REAL"
    assert cols["fuel_sys"] == "TEXT" and cols["mil_on"] == "INTEGER"
    assert "soc" not in cols                      # no Leaf battery columns
    s.insert_reading({"coolant_temp_c": 92.0, "coolant_temp_f": 197.6, "rpm": 812.0,
                      "engine_load_pct": 27.8, "module_v": 14.31, "throttle_pct": 14.9,
                      "fuel_sys": "closed loop", "mil_on": True, "dtc_count": 12,
                      "baro_kpa": 100, "hp_torque": 123},
                     ts="2026-08-28T18:00:00Z")
    row = s.latest()
    assert row["coolant_temp_c"] == 92.0 and row["rpm"] == 812.0
    assert row["module_v"] == 14.31 and row["mil_on"] == 1
    assert json.loads(row["extra"]) == {"hp_torque": 123, "coolant_temp_f": 197.6}
    # graphable: the declared hist names (and the °F twin) come out of history()
    h = s.history(minutes=None)[0]
    assert h["coolant_temp"] == 92.0 and h["coolant_temp_f"] == 197.6
    assert h["rpm"] == 812 and h["module_v"] == 14.31
    assert "soc" not in h
    # and aggregatable
    n = s.conn.execute("SELECT COUNT(*) FROM readings WHERE coolant_temp_c > 90").fetchone()[0]
    assert n == 1
    d = s.daily_health()
    assert d[0]["day"] == "2026-08-28" and d[0]["rpm"] == 812.0 and d[0]["coolant_max_c"] == 92.0
    s.close()


def test_profile_columns_downsample(tmp_path):
    s = Store(str(tmp_path / "lancer.db"), vehicle="lancer_2009")
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(120):
        s.insert_reading({"rpm": 800 + i, "coolant_temp_c": 90.0},
                         ts=now - dt.timedelta(seconds=120 - i))
    h = s.history(minutes=10, max_points=10)
    assert 3 <= len(h) <= 15 and sum(e["n"] for e in h) == 120
    assert h[0]["rpm"] < h[-1]["rpm"] and all(e["coolant_temp"] == 90.0 for e in h)
    s.close()


def test_two_profiles_share_a_file_without_mixing(tmp_path):
    """Additive columns make one file work for two cars; the `vehicle` stamp
    keeps each profile's history to its own rows."""
    path = str(tmp_path / "both.db")
    leaf = Store(path, vehicle="leaf_ze0")
    leaf.insert_reading({"soc": 55.0}, ts="2026-08-28T18:00:00Z")
    leaf.insert_event("hvac_ac_on", True, None, ts=dt.datetime.fromtimestamp(1000, dt.timezone.utc))
    lancer = Store(path, vehicle="lancer_2009")
    lancer.insert_reading({"rpm": 800.0}, ts="2026-08-28T18:01:00Z")
    # the Lancer's columns were added to the existing table, nothing dropped
    cols = _cols(lancer.conn)
    assert {"soc", "rpm", "coolant_temp_c"} <= set(cols)
    assert leaf.count() == 1 and lancer.count() == 1
    assert [h.get("soc") for h in leaf.history(minutes=None)] == [55.0]
    assert [h.get("rpm") for h in lancer.history(minutes=None)] == [800.0]
    assert len(lancer.events("hvac_ac_on")) == 0        # not the Lancer's event
    assert len(leaf.events("hvac_ac_on")) == 1
    leaf.close(); lancer.close()


def test_unstamped_legacy_rows_stay_visible(tmp_path):
    """Rows written before the `vehicle` column existed are NULL. Attributing
    them would be a guess, so they stay visible to whoever opens the file."""
    import sqlite3
    path = str(tmp_path / "legacy.db")
    c = sqlite3.connect(path)
    c.execute(LEGACY_READINGS)
    c.execute("INSERT INTO readings (ts, ts_epoch, soc) VALUES ('2026-08-25T00:00:00Z', 1.0, 42.0)")
    c.commit(); c.close()
    s = Store(path, vehicle="leaf_ze0")
    assert s.count() == 1 and s.history(minutes=None)[0]["soc"] == 42.0
    s.insert_reading({"soc": 43.0}, ts="2026-08-26T00:00:00Z")
    stamps = [r[0] for r in s.conn.execute("SELECT vehicle FROM readings ORDER BY id")]
    assert stamps == [None, "leaf_ze0"]
    s.close()


def test_open_a_full_old_database_loses_nothing(tmp_path):
    """The migration against a database built by the *old* code: additive only,
    idempotent, and every existing value still reads back."""
    import sqlite3
    path = str(tmp_path / "old_full.db")
    c = sqlite3.connect(path)
    c.executescript(LEGACY_READINGS + """;
        CREATE INDEX idx_readings_epoch ON readings(ts_epoch);
        CREATE TABLE cells (reading_id INTEGER NOT NULL, idx INTEGER NOT NULL,
                            mv INTEGER NOT NULL, PRIMARY KEY (reading_id, idx));
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, started TEXT NOT NULL, ended TEXT,
                               adapter TEXT, note TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, ts_epoch REAL NOT NULL,
                             name TEXT NOT NULL, value TEXT, prev TEXT);""")
    c.execute("""INSERT INTO readings (ts, ts_epoch, adapter, soc, pack_v, capacity_ah, soh, hx,
                     lv_volts, temp_avg_c, cell_spread, gear, extra)
                 VALUES ('2026-08-25T00:00:00Z', 100.0, 'ble', 55.0, 391.2, 23.1, 35.5, 62.0,
                         12.4, 21.5, 32, 'P', '{"hvac_evap_c": 9.5}')""")
    c.execute("INSERT INTO cells (reading_id, idx, mv) VALUES (1, 0, 3999)")
    c.execute("INSERT INTO events (ts, ts_epoch, name, value, prev) "
              "VALUES ('2026-08-25T00:00:00Z', 100.0, 'gear', 'P', NULL)")
    c.commit(); c.close()
    before = sqlite3.connect(path)
    before.row_factory = sqlite3.Row
    old_row = dict(before.execute("SELECT * FROM readings").fetchone())
    before.close()

    s = Store(path, vehicle="leaf_ze0")
    row = dict(s.conn.execute("SELECT * FROM readings").fetchone())
    for k, v in old_row.items():
        if v is not None:
            assert row[k] == v, k                  # nothing rewritten
    assert row["vehicle"] is None
    assert row["hvac_evap_c"] == 9.5               # backfilled from `extra`
    assert s.cell_history()["cells"] == [[3999]]
    assert len(s.events("gear")) == 1
    assert s.daily_health()[0]["capacity_ah"] == 23.1
    n = s.count()
    s.close()

    s2 = Store(path, vehicle="leaf_ze0")           # idempotent
    assert s2.count() == n
    assert dict(s2.conn.execute("SELECT * FROM readings").fetchone())["soc"] == 55.0
    s2.close()


def test_db_file_is_opt_in(tmp_path, monkeypatch):
    """A profile may claim its own file; by default every profile keeps writing
    the shared web/leaf_battery.db."""
    import types
    import store as st
    assert st.DEFAULT_DB.endswith("leaf_battery.db")
    prof = types.SimpleNamespace(NAME="fake_1999", HISTORY_COLS={"x": {"kind": "real"}})
    assert Store.__init__.__defaults__ == (None, None)     # Store() still takes no args
    monkeypatch.setattr(st, "DIR", str(tmp_path))
    s = Store(vehicle=prof)
    assert s.path == st.DEFAULT_DB                          # unset -> shared file
    s.close()
    prof.DB_FILE = "fake_1999.db"
    s = Store(vehicle=prof)
    assert s.path == os.path.join(str(tmp_path), "fake_1999.db")
    assert os.path.exists(s.path)
    s.close()
