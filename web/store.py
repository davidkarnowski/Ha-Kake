#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SQLite time-series store — vehicle-agnostic.

Never prunes. One row per poll in `readings`; every row is stamped with the
vehicle profile that produced it. Timestamps are stored as UTC ISO-8601 with a
trailing 'Z' plus a float epoch column for fast range queries and bucketed
downsampling.

Which record keys get real, indexed columns (instead of riding in the `extra`
JSON bag) is the *profile's* decision: `vehicles/<profile>.py` declares
HISTORY_COLS, and this module builds the schema, the insert, the downsampled
history and the daily aggregate from that declaration. Nothing here knows what
a Leaf is. See the contract docstring in `vehicles/__init__.py`.

Usage:
    store = Store()                      # active profile, web/leaf_battery.db
    store.insert_reading(record)         # record from the profile's decode()
    store.history(minutes=1440)          # downsampled list of dicts
    store.daily_health()                 # one row per day for degradation charts
    store.migrate_legacy(json_path, jsonl_path)
"""

import datetime as dt
import json
import os
import sqlite3
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(DIR))            # repo root, for `vehicles`

from vehicles import active_vehicle, history_cols   # noqa: E402

DEFAULT_DB = os.path.join(DIR, "leaf_battery.db")

# record keys the store consumes or that are transport noise — never in `extra`.
# A profile adds its own bulky/raw keys through EXTRA_SKIP.
BASE_SKIP = {"cells", "timestamp", "status", "adapter_type", "adapter_name", "adapter_port"}


def _coerce(kind, v):
    if v is None:
        return None
    if kind == "bool":
        return int(bool(v))
    if kind == "int":
        return int(v)
    if kind == "real":
        return float(v)
    return str(v)


def _resolve(rec, key):
    """Record value for a HISTORY_COLS key: a plain name, a dotted list index
    ("temps.0", the same notation the signal registry uses), or a callable
    taking the record (for values a profile derives itself)."""
    if callable(key):
        return key(rec)
    if "." in key:
        base, _, idx = key.partition(".")
        seq = rec.get(base)
        if isinstance(seq, (list, tuple)) and idx.isdigit():
            i = int(idx)
            return seq[i] if i < len(seq) else None
        return None
    return rec.get(key)


# Tables that are the same for every vehicle. `readings` is built from the
# active profile's declaration; see _readings_ddl().
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    mv INTEGER NOT NULL,
    PRIMARY KEY (reading_id, idx)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL,
    ended TEXT,
    adapter TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL, ts_epoch REAL NOT NULL,
    name TEXT NOT NULL,        -- 'hvac_ac_on','gear','locked','door_any',...
    value TEXT,                -- new state as text ('1'/'0'/'D'/...)
    prev TEXT                  -- previous state ('1'/'0'/... or NULL at first sight)
);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(name, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_readings_epoch ON readings(ts_epoch);
"""


def ev_norm(v):
    """Normalise a watched value to stable text for transition diffing."""
    if v is None:
        return None
    if v is True:
        return "1"
    if v is False:
        return "0"
    return str(v)


def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_utc(ts):
    """Accept naive-local or aware ISO strings / datetimes → aware UTC datetime."""
    if isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
    else:
        d = ts
    if d.tzinfo is None:
        d = d.astimezone()  # interpret as local time
    return d.astimezone(dt.timezone.utc)


def _iso_z(d):
    return d.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path=None, vehicle=None):
        """`vehicle` is a profile module, a profile name, or None for the one
        the process has bound (reader.set_vehicle → vehicles.active_vehicle)."""
        self.vehicle = active_vehicle(vehicle)
        self.vname = self.vehicle.NAME
        self.cols = history_cols(self.vehicle)
        self.path = path or self._default_path()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(self._readings_ddl() + BASE_SCHEMA)
        self._migrate_columns()

    def _default_path(self):
        """A profile may opt out of the shared file with DB_FILE; by default
        every profile writes web/leaf_battery.db (rows are separated by the
        `vehicle` column, not by the filename)."""
        name = getattr(self.vehicle, "DB_FILE", None)
        return os.path.join(DIR, name) if name else DEFAULT_DB

    # ── schema (generated from the profile's declaration) ─────────────────

    def _readings_ddl(self):
        cols = ",\n    ".join(f"{c} {s['type']}" for c, s in self.cols.items())
        return f"""
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    ts_epoch REAL NOT NULL,
    adapter TEXT,
    {cols},
    vehicle TEXT,
    extra TEXT
);
"""

    def _migrate_columns(self):
        """Additive, self-healing migration for personal DBs: add any column
        this profile declares that the file does not have yet, then backfill it
        from the `extra` JSON of existing rows. Nothing is ever dropped or
        renamed, so a database written by an older version — or by a different
        profile — keeps working untouched."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(readings)")}
        added = []
        for col, s in self.cols.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {s['type']}")
                added.append(col)
        if "vehicle" not in have:
            self.conn.execute("ALTER TABLE readings ADD COLUMN vehicle TEXT")
        for table in ("events", "sessions"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if cols and "vehicle" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN vehicle TEXT")
        for col, s in self.cols.items():
            if s.get("index"):
                self.conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {s['index']} ON readings(ts_epoch) WHERE {col}=1")
        self.conn.commit()
        # Databases from before the promoted-columns release never had their
        # `extra` unpacked; do that once, then only ever backfill new columns.
        if not self.conn.execute("SELECT 1 FROM meta WHERE key='promote_backfill_v1'").fetchone():
            self._backfill(list(self.cols))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('promote_backfill_v1', ?)",
                (utc_now_iso(),))
            self.conn.commit()
        elif added:
            self._backfill(added)
            self.conn.commit()

    def _backfill(self, cols):
        """Populate `cols` from existing rows' `extra` JSON (only where the
        column is still NULL — never overwrites a real value)."""
        keys = {c: self.cols[c]["key"] for c in cols
                if isinstance(self.cols[c]["key"], str) and "." not in self.cols[c]["key"]}
        if not keys:
            return
        try:
            for col, key in keys.items():
                self.conn.execute(
                    f"UPDATE readings SET {col}=json_extract(extra, '$.{key}') "
                    f"WHERE {col} IS NULL AND json_extract(extra, '$.{key}') IS NOT NULL")
        except sqlite3.OperationalError:
            self._backfill_py(keys)          # SQLite build without json1

    def _backfill_py(self, keys):
        for r in self.conn.execute("SELECT id, extra FROM readings WHERE extra IS NOT NULL").fetchall():
            ex = json.loads(r["extra"] or "{}")
            sets, vals = [], []
            for col, key in keys.items():
                if ex.get(key) is not None:
                    sets.append(f"{col}=?")
                    vals.append(_coerce(self.cols[col]["kind"], ex[key]))
            if sets:
                vals.append(r["id"])
                self.conn.execute(f"UPDATE readings SET {', '.join(sets)} WHERE id=?", vals)

    def close(self):
        self.conn.close()

    # ── which rows belong to this vehicle ────────────────────────────────
    #
    # Rows written from here on carry the profile name. Rows written before
    # this column existed are NULL: they are shown to every profile, because
    # attributing them after the fact would be a guess. Two profiles sharing
    # one file therefore no longer mix *new* data.

    def _vfilter(self, alias=""):
        p = f"{alias}." if alias else ""
        return f"({p}vehicle IS NULL OR {p}vehicle = ?)", [self.vname]

    # ── writes ───────────────────────────────────────────────────────────

    def insert_reading(self, rec, ts=None, adapter=None):
        """Insert one decoded record. Returns the new reading id."""
        d = to_utc(ts or rec.get("timestamp") or dt.datetime.now(dt.timezone.utc))
        row = {
            "ts": _iso_z(d),
            "ts_epoch": d.timestamp(),
            "adapter": adapter or rec.get("adapter_type"),
            "vehicle": self.vname,
        }
        for col, s in self.cols.items():
            row[col] = _coerce(s["kind"], _resolve(rec, s["key"]))
        skip = set(row) | BASE_SKIP | set(getattr(self.vehicle, "EXTRA_SKIP", ()))
        skip |= {s["key"] for s in self.cols.values() if isinstance(s["key"], str)}
        extra = {k: v for k, v in rec.items() if k not in skip}
        row["extra"] = json.dumps(extra) if extra else None

        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self.conn:
            cur = self.conn.execute(f"INSERT INTO readings ({cols}) VALUES ({qs})", list(row.values()))
            rid = cur.lastrowid
            cells = rec.get("cells")
            if cells:
                self.conn.executemany(
                    "INSERT INTO cells (reading_id, idx, mv) VALUES (?, ?, ?)",
                    [(rid, i, mv) for i, mv in enumerate(cells)],
                )
        return rid

    def start_session(self, adapter, note=None):
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO sessions (started, adapter, note, vehicle) VALUES (?, ?, ?, ?)",
                (utc_now_iso(), adapter, note, self.vname),
            )
            return cur.lastrowid

    def end_session(self, sid):
        with self.conn:
            self.conn.execute("UPDATE sessions SET ended=? WHERE id=?", (utc_now_iso(), sid))

    # ── events (state transitions) ───────────────────────────────────────

    def insert_event(self, name, value, prev=None, ts=None):
        d = to_utc(ts or dt.datetime.now(dt.timezone.utc))
        with self.conn:
            self.conn.execute(
                "INSERT INTO events (ts, ts_epoch, name, value, prev, vehicle) VALUES (?, ?, ?, ?, ?, ?)",
                (_iso_z(d), d.timestamp(), name, ev_norm(value), ev_norm(prev), self.vname))

    def events(self, name=None, t0=None, t1=None):
        vf, a = self._vfilter()
        q = f"SELECT ts, ts_epoch, name, value, prev FROM events WHERE {vf}"
        if name:
            q += " AND name=?"; a.append(name)
        if t0 is not None:
            q += " AND ts_epoch>=?"; a.append(t0)
        if t1 is not None:
            q += " AND ts_epoch<=?"; a.append(t1)
        return self.conn.execute(q + " ORDER BY ts_epoch", a).fetchall()

    def on_time(self, name, t0, t1, on="1"):
        """Seconds `name` held value `on` within [t0, t1], from transition events.
        The state at t0 is the last event at/before t0 (default off if none)."""
        vf, a = self._vfilter()
        pre = self.conn.execute(
            f"SELECT value FROM events WHERE {vf} AND name=? AND ts_epoch<=? "
            f"ORDER BY ts_epoch DESC LIMIT 1", a + [name, t0]).fetchone()
        state = bool(pre) and pre["value"] == on
        evs = self.conn.execute(
            f"SELECT ts_epoch, value FROM events WHERE {vf} AND name=? AND ts_epoch>? "
            f"AND ts_epoch<=? ORDER BY ts_epoch", a + [name, t0, t1]).fetchall()
        total, cur = 0.0, t0
        for e in evs:
            if state:
                total += e["ts_epoch"] - cur
            cur = e["ts_epoch"]
            state = e["value"] == on
        if state:
            total += t1 - cur
        return total

    # ── reads ────────────────────────────────────────────────────────────

    def count(self):
        vf, a = self._vfilter()
        return self.conn.execute(f"SELECT COUNT(*) FROM readings WHERE {vf}", a).fetchone()[0]

    def latest(self):
        vf, a = self._vfilter()
        r = self.conn.execute(
            f"SELECT * FROM readings WHERE {vf} ORDER BY ts_epoch DESC LIMIT 1", a).fetchone()
        return dict(r) if r else None

    def history(self, minutes=None, max_points=1500):
        """Readings in the last `minutes` (None = everything), downsampled by
        time-bucket averaging to at most ~max_points rows. Each entry is
        {"t", "n"} plus one key per history column the profile declared with a
        `hist` name (temperatures also get their °F twin)."""
        vf, a = self._vfilter()
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        if minutes:
            since = now - minutes * 60
            first = since
        else:
            since = 0
            row = self.conn.execute(f"SELECT MIN(ts_epoch) FROM readings WHERE {vf}", a).fetchone()
            first = row[0] if row and row[0] else now
        span = max(now - first, 1)
        n = self.conn.execute(
            f"SELECT COUNT(*) FROM readings WHERE {vf} AND ts_epoch >= ?", a + [since]).fetchone()[0]
        if n <= max_points:
            rows = self.conn.execute(
                f"SELECT * FROM readings WHERE {vf} AND ts_epoch >= ? ORDER BY ts_epoch", a + [since]
            ).fetchall()
            return [self._row_to_hist(dict(r)) for r in rows]

        bucket = span / max_points
        sel = ["MIN(ts) AS ts", "AVG(ts_epoch) AS ts_epoch"]
        sel += [f"AVG({c}) AS {c}" for c, s in self.cols.items()
                if s.get("hist") and s["kind"] != "text"]
        sel.append("COUNT(*) AS n")
        rows = self.conn.execute(
            f"""SELECT {', '.join(sel)}
                FROM readings WHERE {vf} AND ts_epoch >= ?
                GROUP BY CAST((ts_epoch - ?) / ? AS INTEGER)
                ORDER BY ts_epoch""",
            a + [since, first, bucket],
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["ts"] = _iso_z(dt.datetime.fromtimestamp(d["ts_epoch"], dt.timezone.utc))
            out.append(self._row_to_hist(d))
        return out

    def _row_to_hist(self, d):
        out = {"t": d["ts"]}
        for col, s in self.cols.items():
            name = s.get("hist")
            if not name:
                continue
            v = d.get(col)
            if s["kind"] == "bool":
                out[name] = None if v is None else bool(round(v))
            elif s["kind"] == "text":
                out[name] = v
            else:
                out[name] = None if v is None else round(v, s.get("round", 2))
            if s.get("hist_f"):
                out[s["hist_f"]] = None if v is None else round(v * 9 / 5 + 32, 1)
        out["n"] = d.get("n", 1)
        return out

    def daily_health(self):
        """One row per UTC day, aggregated over the history columns the profile
        marked `daily` (the Leaf: capacity/SOH/HX/temps/spread/12V/insulation).
        Empty for a profile that marks none."""
        aggs, filt = [], None
        for col, s in self.cols.items():
            for fn, name in (s.get("daily") or {}).items():
                aggs.append(f"{fn.upper()}({col}) AS {name}")
            if s.get("daily_filter"):
                filt = col
        if not aggs:
            return []
        vf, a = self._vfilter()
        where = f"WHERE {vf}" + (f" AND {filt} IS NOT NULL" if filt else "")
        rows = self.conn.execute(
            f"""SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n, {', '.join(aggs)}
                FROM readings {where}
                GROUP BY day ORDER BY day""", a
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k, v in list(d.items()):
                if isinstance(v, float):
                    d[k] = round(v, 3)
            for col, s in self.cols.items():
                twin = s.get("hist_f")
                avg = (s.get("daily") or {}).get("avg")
                if twin and avg and d.get(avg) is not None:
                    d[twin] = round(d[avg] * 9 / 5 + 32, 1)
            out.append(d)
        return out

    def cell_history(self, limit=30):
        """Per-cell voltages for the last `limit` full reads, as
        {"t": [...], "cells": [[mV]...]} for rank-stability analysis. Only
        profiles whose records carry a `cells` list ever fill this table."""
        vf, a = self._vfilter("r")
        ids = self.conn.execute(
            f"""SELECT r.id, r.ts FROM readings r
                WHERE {vf} AND EXISTS (SELECT 1 FROM cells c WHERE c.reading_id = r.id)
                ORDER BY r.ts_epoch DESC LIMIT ?""",
            a + [limit],
        ).fetchall()
        ids = list(reversed(ids))
        out = {"t": [], "cells": []}
        for rid, ts in ids:
            mvs = [row[0] for row in self.conn.execute(
                "SELECT mv FROM cells WHERE reading_id=? ORDER BY idx", (rid,))]
            out["t"].append(ts)
            out["cells"].append(mvs)
        return out

    # ── migration ────────────────────────────────────────────────────────

    def migrate_legacy(self, history_json=None, jsonl_path=None, state_json=None):
        """One-time import of battery_history.json / battery_log_*.jsonl.
        Old power values were stored unsigned (positive while discharging); the
        sign is recovered from the SOC trend the same way the old dashboard did.
        Idempotent via the meta table."""
        done = self.conn.execute("SELECT value FROM meta WHERE key='migrated'").fetchone()
        if done:
            return 0
        n = 0
        if history_json and os.path.exists(history_json):
            with open(history_json) as f:
                hist = json.load(f)
            last_dir = -1
            prev_soc = None
            for h in hist:
                soc = h.get("soc")
                if soc is not None and prev_soc is not None:
                    if soc - prev_soc > 0.005:
                        last_dir = 1
                    elif soc - prev_soc < -0.005:
                        last_dir = -1
                if soc is not None:
                    prev_soc = soc
                pw = h.get("power_kw")
                cur = h.get("current_a")
                rec = {
                    "soc": soc, "pack_v": h.get("pack_v"), "cell_spread": h.get("spread"),
                    "power_kw": None if pw is None else abs(pw) * last_dir,
                    "current_a": None if cur is None else abs(cur) * last_dir,
                    "discharging": None if pw is None else last_dir < 0,
                    "temp_avg_c": h.get("temp_avg"),
                }
                self.insert_reading(rec, ts=h["t"], adapter="legacy-json")
                n += 1
        if jsonl_path and os.path.exists(jsonl_path):
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    j = json.loads(line)
                    cells = j.get("cell_mv") or []
                    temps = j.get("temps_degc") or []
                    rec = {
                        "soc": j.get("soc_pct"), "capacity_ah": j.get("capacity_ah"),
                        "soh": j.get("soh_pct"), "hx": j.get("hx_pct"),
                        "lv_volts": j.get("lv_volts"), "insulation_kohm": j.get("insulation_kohm"),
                        "temps": temps, "cells": cells,
                    }
                    if cells:
                        mn, mx = min(cells), max(cells)
                        rec.update({
                            "cell_min": mn, "cell_max": mx, "cell_avg": round(sum(cells) / len(cells)),
                            "cell_spread": mx - mn, "cell_min_idx": cells.index(mn),
                            "cell_max_idx": cells.index(mx), "pack_v": round(sum(cells) / 1000.0, 1),
                        })
                    self.insert_reading(rec, ts=j["timestamp"], adapter="legacy-jsonl")
                    n += 1
        if state_json and os.path.exists(state_json):
            with open(state_json) as f:
                st = json.load(f)
            if st.get("status") == "ok" and st.get("timestamp") and st.get("capacity_ah"):
                temps = st.get("temps") or []
                rec = dict(st)
                rec["temp_avg_c"] = round(sum(temps) / len(temps), 1) if temps else None
                cells = st.get("cells") or []
                if cells:
                    rec["cell_min_idx"] = cells.index(min(cells))
                    rec["cell_max_idx"] = cells.index(max(cells))
                pw = st.get("power_kw")
                if pw is not None and st.get("discharging") is not None:
                    sign = -1 if st["discharging"] else 1
                    rec["power_kw"] = abs(pw) * sign
                    if st.get("current_a") is not None:
                        rec["current_a"] = abs(st["current_a"]) * sign
                self.insert_reading(rec, ts=st["timestamp"], adapter="legacy-state")
                n += 1
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('migrated', ?)", (utc_now_iso(),))
        return n


if __name__ == "__main__":
    s = Store()
    if "--migrate" in sys.argv:
        root = os.path.dirname(DIR)
        hj = os.path.join(DIR, "battery_history.json")
        jl = sorted(f for f in os.listdir(root) if f.startswith("battery_log_") and f.endswith(".jsonl"))
        n = s.migrate_legacy(hj, os.path.join(root, jl[0]) if jl else None,
                             os.path.join(DIR, "battery_state.json"))
        print(f"migrated {n} legacy readings")
    print(f"{s.path}: {s.count()} readings ({s.vname})")
    for d in s.daily_health():
        print(f"  {d['day']}: n={d['n']} cap={d.get('capacity_ah')} Ah soh={d.get('soh')}% "
              f"temp={d.get('temp_avg_c')}°C/{d.get('temp_avg_f')}°F spread={d.get('cell_spread')}")
