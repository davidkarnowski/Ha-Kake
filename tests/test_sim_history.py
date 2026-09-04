# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bulk history generation (`hakake_sim.py --generate`).

What these tests are guarding:

1. **Row shape.** Generated rows go in through `Store.insert_reading()` with
   the record the profile's `decode()` would have produced, so every existing
   API route and chart works against a generated database unmodified. If this
   drifts, the generator quietly stops being useful for UI work.
2. **Determinism.** An agent iterating on a chart must see the same data on
   every run, or a visual difference means nothing.
3. **The trends the charts exist to show** — a real fade, widening spread,
   seasonal temperature.
4. **That it is obviously synthetic**, and that it cannot touch the owner's
   real database.
"""

import datetime as dt
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web"))

from simulator import history                                   # noqa: E402
from store import Store                                         # noqa: E402
import vehicles.leaf_ze0 as leaf                                 # noqa: E402

DAYS = 6
END = dt.datetime(2026, 6, 1, 18, 0, 0)      # fixed, so the tests are not seasonal


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("hist") / "gen.db")
    summary = history.generate(out=out, days=DAYS, seed=1, end=END)
    store = Store(out, vehicle="leaf_ze0")
    yield summary, store
    store.close()


# ── it produced something, fast ──────────────────────────────────────────

def test_it_writes_readings_cells_and_events(generated):
    sm, store = generated
    assert sm["rows"] > 100 * DAYS / 2
    assert store.count() == sm["rows"]
    assert sm["cell_rows"] > 0 and sm["events"] > 0
    assert sm["drives"] > 0 and sm["charges"] > 0


def test_every_row_is_stamped_with_the_vehicle_and_a_generated_adapter(generated):
    _, store = generated
    rows = store.conn.execute(
        "SELECT DISTINCT vehicle, adapter FROM readings").fetchall()
    assert [tuple(r) for r in rows] == [("leaf_ze0", "sim-generated")]


def test_the_columns_the_charts_query_are_populated(generated):
    """Not "some columns are non-null" — every history column the profile
    declares with a `hist` or `daily` name, because those are exactly the ones
    /api/history and /api/health read."""
    _, store = generated
    charted = [c for c, s in leaf.HISTORY_COLS.items() if s.get("hist") or s.get("daily")]
    counts = store.conn.execute(
        "SELECT " + ", ".join(f"COUNT({c})" for c in charted) + " FROM readings").fetchone()
    empty = [c for c, n in zip(charted, counts) if not n]
    assert not empty, f"never populated: {empty}"


def test_history_and_health_routes_have_something_to_draw(generated):
    _, store = generated
    # NB: Store.history() buckets from the first row to *wall-clock now*, and
    # this fixture's END is deliberately in the past, so the bucket count is
    # smaller than it would be for a database generated with the default
    # end-of-period (now). What matters here is that every bucket is drawable.
    hist = store.history(minutes=None, max_points=800)
    assert len(hist) > 30
    assert all("t" in h and h.get("soc") is not None for h in hist)
    assert [h["t"] for h in hist] == sorted(h["t"] for h in hist)
    health = store.daily_health()
    assert DAYS - 1 <= len(health) <= DAYS + 2
    cells = store.cell_history(limit=5)
    assert len(cells["cells"]) == 5 and len(cells["cells"][0]) == 96


def test_events_cover_the_profiles_watch_list(generated):
    _, store = generated
    seen = {r["name"] for r in store.conn.execute("SELECT DISTINCT name FROM events")}
    for name in ("gear", "locked", "door_any", "hvac_on"):
        assert name in seen, f"no {name} transitions were recorded"
    assert seen <= set(leaf.WATCH), "only the profile's watched names may appear"


# ── the data looks like a car being used ─────────────────────────────────

def test_the_soc_cycles_daily_without_ever_running_flat(generated):
    _, store = generated
    per_day = store.conn.execute(
        "SELECT substr(ts,1,10) d, MIN(soc), MAX(soc) FROM readings GROUP BY d").fetchall()
    swings = [hi - lo for _, lo, hi in (tuple(r) for r in per_day)]
    assert max(swings) > 20, "at least one day must show a real charge/discharge cycle"
    assert min(lo for _, lo, _ in (tuple(r) for r in per_day)) > 3.0


def test_both_charging_and_driving_rows_exist_with_the_right_signs(generated):
    _, store = generated
    charging = store.conn.execute(
        "SELECT COUNT(*) FROM readings WHERE current_a > 1 AND power_kw > 0").fetchone()[0]
    driving = store.conn.execute(
        "SELECT COUNT(*) FROM readings WHERE speed_mph > 5 AND current_a < 0").fetchone()[0]
    assert charging > 20 and driving > 20
    # house rule: sign of power always agrees with sign of current
    assert store.conn.execute(
        "SELECT COUNT(*) FROM readings WHERE current_a * power_kw < 0").fetchone()[0] == 0


def test_charge_rows_show_the_taper_not_a_flat_line(generated):
    """Parked-and-plugged rows only — regen while driving also puts current
    positive, and that is not a charge curve."""
    _, store = generated
    # `current_a > 0.1`, not 0.5: the accessories a charging car runs (the
    # DC-DC, the pump, the charger's own electronics — LOADS_W["base_charging"])
    # now come off the charge power, so the last trickly rows near 100 % are a
    # couple of hundred milliamps net rather than half an amp. They are still
    # charge rows, and dropping them was hiding the top of the taper.
    plugged = "speed_mph = 0 AND gear = 'P' AND current_a > 0.1"
    low = store.conn.execute(
        f"SELECT AVG(power_kw) FROM readings WHERE {plugged} AND soc < 60").fetchone()[0]
    high = store.conn.execute(
        f"SELECT AVG(power_kw) FROM readings WHERE {plugged} AND soc > 93").fetchone()[0]
    assert low and high and high < low / 2, "charge power must fall as the pack fills"


def test_there_are_idle_gaps_not_a_uniform_sawtooth(generated):
    """A car is parked most of the time; the row spacing must show it."""
    _, store = generated
    ts = [r[0] for r in store.conn.execute("SELECT ts_epoch FROM readings ORDER BY ts_epoch")]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    idle = [g for g in gaps if g > 900]
    busy = [g for g in gaps if g <= 130]
    assert len(idle) > 15 * DAYS / 2, "long parked gaps must dominate the calendar"
    assert len(busy) > 50, "and the active stretches must be sampled finely"


def test_no_row_is_stamped_in_the_future(generated):
    _, store = generated
    last = store.conn.execute("SELECT MAX(ts_epoch) FROM readings").fetchone()[0]
    assert last <= END.astimezone(dt.timezone.utc).timestamp() + 1


# ── the long arcs the degradation chart exists for ───────────────────────

def test_capacity_and_soh_decline_and_the_spread_widens(tmp_path):
    sm = history.generate(out=str(tmp_path / "long.db"), days=180, seed=2, end=END)
    store = Store(sm["path"], vehicle="leaf_ze0")
    try:
        health = store.daily_health()
        assert len(health) > 170
        first = [d["capacity_ah"] for d in health[:10]]
        last = [d["capacity_ah"] for d in health[-10:]]
        assert sum(first) / len(first) - sum(last) / len(last) > 1.0, "no visible fade"
        assert all(d["soh"] == pytest.approx(d["capacity_ah"] / 66.0 * 100.0, abs=0.2)
                   for d in health)
        # a trend, but not a ruler-straight line: the chart needs scatter
        caps = [d["capacity_ah"] for d in health]
        assert len(set(caps)) > len(caps) * 0.9
        assert sum(1 for a, b in zip(caps, caps[1:]) if b > a) > 20, "too smooth to be real"
        sp_first = sum(d["cell_spread"] for d in health[:10]) / 10
        sp_last = sum(d["cell_spread"] for d in health[-10:]) / 10
        assert sp_last > sp_first + 3, "the cell spread must widen over months"
    finally:
        store.close()


def test_the_ambient_temperature_has_a_season_in_it():
    c = history.Climate()
    jan = c.at(dt.datetime(2026, 1, 15, 6))
    jul = c.at(dt.datetime(2026, 7, 15, 15))
    assert jul - jan > 20
    noon = c.at(dt.datetime(2026, 7, 15, 15))
    dawn = c.at(dt.datetime(2026, 7, 15, 3))
    assert noon - dawn > 5, "and a day in it too"


# ── determinism ──────────────────────────────────────────────────────────

def _digest(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute(
            "SELECT COUNT(*), ROUND(SUM(soc), 3), ROUND(SUM(power_kw), 3), "
            "ROUND(SUM(capacity_ah), 3), MIN(ts), MAX(ts) FROM readings").fetchone()
    finally:
        c.close()


def test_the_same_seed_gives_the_same_database(tmp_path):
    a = history.generate(out=str(tmp_path / "a.db"), days=4, seed=7, end=END)
    b = history.generate(out=str(tmp_path / "b.db"), days=4, seed=7, end=END)
    assert a["rows"] == b["rows"] and a["events"] == b["events"]
    assert _digest(a["path"]) == _digest(b["path"])


def test_a_different_seed_gives_a_different_database(tmp_path):
    a = history.generate(out=str(tmp_path / "a.db"), days=4, seed=7, end=END)
    b = history.generate(out=str(tmp_path / "b.db"), days=4, seed=8, end=END)
    assert _digest(a["path"]) != _digest(b["path"])


def test_regenerating_over_a_file_replaces_it_rather_than_appending(tmp_path):
    out = str(tmp_path / "again.db")
    first = history.generate(out=out, days=4, seed=7, end=END)
    second = history.generate(out=out, days=4, seed=7, end=END)
    assert first["rows"] == second["rows"]
    assert _digest(out)[0] == first["rows"]


# ── it must be impossible to mistake for real data ───────────────────────

def test_the_database_says_it_is_synthetic(generated):
    _, store = generated
    meta = dict(store.conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["synthetic"] == "true"
    assert "SYNTHETIC" in meta["warning"] and "not a reading" in meta["warning"].lower()
    assert meta["generator_seed"] == "1"
    assert meta["generated_by"].endswith("history.py")


def test_every_row_carries_the_stamp_in_its_extra_bag(generated):
    _, store = generated
    for r in store.conn.execute("SELECT extra FROM readings LIMIT 50"):
        extra = json.loads(r[0])
        assert extra["simulated"] is True and extra["generated"] is True


def test_the_state_sibling_is_written_and_shouts(generated):
    sm, _ = generated
    with open(sm["state_path"]) as f:
        st = json.load(f)
    assert st["status"] == "ok" and st["simulated"] is True
    assert "SYNTHETIC" in st["message"]
    assert st["soc"] is not None and st["capacity_ah"] is not None
    assert history.state_path_for("/tmp/x.db") == "/tmp/x_state.json"


def test_it_refuses_to_write_to_the_real_database(tmp_path):
    """The owner's file holds 12,000+ irreplaceable readings. Not by accident,
    not by a --out typo, not ever."""
    import store as store_mod
    with pytest.raises(ValueError, match="refusing"):
        history.generate(out=store_mod.DEFAULT_DB, days=1)
    with pytest.raises(ValueError, match="refusing"):
        history.generate(out=str(tmp_path / "leaf_battery.db"), days=1)
    assert not os.path.exists(tmp_path / "leaf_battery.db")


def test_an_unsupported_vehicle_is_refused_clearly(tmp_path):
    with pytest.raises(ValueError, match="leaf_ze0"):
        history.generate(out=str(tmp_path / "l.db"), days=1, vehicle="lancer_2009")


# ── the record shaping itself ────────────────────────────────────────────

def test_record_from_state_resolves_every_declared_history_column():
    from simulator import make_sim
    from store import _resolve
    from vehicles import history_cols

    sim = make_sim(vehicle="leaf_ze0", knobs={"charging": True, "hvac_on": True,
                                              "hvac_ac_on": True, "hvac_fan_speed": 3},
                   seed=1)
    rec = history.record_from_state(sim.state(), cells=True)
    cols = history_cols(leaf)
    missing = [c for c, s in cols.items() if _resolve(rec, s["key"]) is None]
    assert not missing, f"HISTORY_COLS keys the generator never fills: {missing}"
    assert len(rec["cells"]) == 96
    assert rec["cell_min"] == min(rec["cells"])
    assert rec["cells"][rec["cell_max_idx"]] == rec["cell_max"]
