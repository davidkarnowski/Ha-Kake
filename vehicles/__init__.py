# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vehicle profiles — the seam that makes the reader vehicle-agnostic.

A profile is a module in this package exporting:

  NAME, TITLE      str — id (== the filename) and human name ("2012 Nissan Leaf (ZE0)")
  ITEMS            dict id -> {kind, period, label, ...} — everything pollable.
                   kind maps into TARGETS; UDS-style kinds need "cmd" (+
                   optional "timeout", "est"); monitor kinds need "id"/"secs".
  TARGETS          dict kind -> (tx, rx) for UDS request/response headers, or
                   None for passive monitor capture (ATCAF0 + ATCRA + ATMA).
  KIND_ORDER       tuple — poll order within a cycle (minimise ECU switching)
  TILES            list of built-in dashboard tiles ({id, name, items}) — may
                   be empty; user signal tiles work for any profile.
  DEFAULT_SPAN     dict tile id -> span (built-ins only)
  DEFAULT_TILES    list — the out-of-the-box tile config (built-in and/or
                   signal tiles)
  ITEM_KEYS        dict item -> keys to drop from the cache when disabled
  WATCH            tuple of record keys logged to the events table on change
  FAST_ONLY        set of item ids for --fast mode
  SIGNALS          dict key -> registry entry (label, unit, min/max, dec,
                   item, color, hist, alt/alt_unit, kind) — what the UI offers
  configure(elm)   async — full adapter setup for this vehicle
  decode(responses) -> (record, alive) — responses is {item_id: raw lines};
                   alive is False when the primary ECU gave nothing, None when
                   no primary item was polled this cycle.
  apply_policy(cache, calib, state)   optional — per-vehicle sensor policy
                   (fusion, calibration); `state` is a dict the profile owns.

History columns (optional, but needed for anything graphable)
-------------------------------------------------------------
  HISTORY_COLS     dict column -> spec. Record keys listed here get real,
                   indexed SQLite columns in `readings`; everything else the
                   decode produces rides in the `extra` JSON bag and cannot be
                   charted or aggregated. web/store.py builds the schema, the
                   insert, the downsampled history() and daily_health() from
                   this — a profile never edits the store. Spec keys:
                     kind          "real" | "int" | "bool" | "text" (required)
                     type          SQL type; default REAL/INTEGER/INTEGER/TEXT
                     key           record key; default the column name. May be
                                   dotted for a list element ("temps.0") or a
                                   callable(record) for a derived value.
                     hist          name this value gets in history() entries;
                                   omit to store it without charting it
                     round         decimals in history() (default 2)
                     hist_f        also emit the °F twin under this name
                     daily         {"avg"|"min"|"max": output name} for
                                   daily_health()
                     daily_filter  True: daily_health() skips rows where this
                                   column is NULL (one column at most)
                     index         name of a partial index on (ts_epoch) where
                                   this column is 1
  EXTRA_SKIP       optional tuple — record keys never worth storing in `extra`
                   (raw dumps, lists already stored in columns)
  DB_FILE          optional str — a database file of this profile's own, in
                   web/. Unset (the default) means the shared
                   web/leaf_battery.db; rows are separated by the `vehicle`
                   column either way.

The `cells` table is an optional profile-specific extra, not a general
mechanism: a record carrying a `cells` list of per-cell millivolts gets one row
per cell, which is what the Leaf's 96 cell pairs need. Profiles that emit no
`cells` key never touch that table.

`get_vehicle(name)` resolves: explicit arg -> HAKAKE_VEHICLE env ->
config.local.json "vehicle" -> "leaf_ze0", and validates the profile.
`validate_profile(mod)` returns the list of problems (empty == valid) and is
runnable standalone:  python vehicles/__init__.py [name ...]
"""

import importlib
import inspect
import json
import os

DEFAULT = "leaf_ze0"
_cache = {}
_active = None

_REQUIRED = ("NAME", "TITLE", "ITEMS", "TARGETS", "KIND_ORDER", "TILES",
             "DEFAULT_SPAN", "DEFAULT_TILES", "ITEM_KEYS", "WATCH",
             "FAST_ONLY", "SIGNALS", "configure", "decode")

_SQL_TYPE = {"real": "REAL", "int": "INTEGER", "bool": "INTEGER", "text": "TEXT"}


def _config_vehicle():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.local.json")
    try:
        with open(path) as f:
            return json.load(f).get("vehicle")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def available():
    d = os.path.dirname(os.path.abspath(__file__))
    return sorted(f[:-3] for f in os.listdir(d)
                  if f.endswith(".py") and not f.startswith("_"))


def history_cols(mod):
    """The profile's HISTORY_COLS with defaults filled in: column -> spec with
    at least `type`, `key` and `kind`. Declaration order is column order."""
    out = {}
    for col, spec in (getattr(mod, "HISTORY_COLS", None) or {}).items():
        s = dict(spec)
        s.setdefault("kind", "real")
        s.setdefault("type", _SQL_TYPE.get(s["kind"], "REAL"))
        s.setdefault("key", col)
        out[col] = s
    return out


def get_vehicle(name=None):
    global _active
    name = name or os.environ.get("HAKAKE_VEHICLE") or _config_vehicle() or DEFAULT
    if name in _cache:
        _active = _cache[name]
        return _cache[name]
    try:
        mod = importlib.import_module(f"vehicles.{name}")
    except ImportError as e:
        raise ValueError(f"unknown vehicle profile {name!r} (have: {', '.join(available())})") from e
    problems = validate_profile(mod)
    if problems:
        raise ValueError(f"vehicle profile {name!r} is invalid:\n  " + "\n  ".join(problems))
    for k, v in mod.SIGNALS.items():          # registry defaults the UI relies on
        v.setdefault("kind", "number")
        v.setdefault("key", k)
    _cache[name] = mod
    _active = mod
    return mod


def active_vehicle(v=None):
    """The profile a module-level consumer (the store) should use: an explicit
    module or name, else the last one `get_vehicle()` bound in this process,
    else the configured default."""
    if v is None:
        return _active or get_vehicle()
    if isinstance(v, str):
        return get_vehicle(v)
    return v


# ── contract validation ──────────────────────────────────────────────────
#
# Returns problems instead of raising per-problem: an author fixing a new
# profile wants the whole list at once, and `assert` disappears under -O.

def validate_profile(mod):
    """Check a profile module against the contract. Returns a list of
    human-readable problems; empty means valid."""
    p = []
    name = getattr(mod, "NAME", None) or getattr(mod, "__name__", "?").rsplit(".", 1)[-1]

    missing = [a for a in _REQUIRED if not hasattr(mod, a)]
    if missing:
        return [f"{name}: missing required attribute(s): {', '.join(missing)}"]

    fname = getattr(mod, "__name__", "").rsplit(".", 1)[-1]
    if not isinstance(mod.NAME, str) or (fname and mod.NAME != fname):
        p.append(f"{name}: NAME must be the module's filename ({fname!r}), found {mod.NAME!r}")
    if not isinstance(mod.TITLE, str) or not mod.TITLE.strip():
        p.append(f"{name}: TITLE must be a non-empty string, found {mod.TITLE!r}")

    # ── TARGETS / KIND_ORDER ──
    if not isinstance(mod.TARGETS, dict) or not mod.TARGETS:
        p.append(f"{name}: TARGETS must be a non-empty dict kind -> (tx, rx) or None")
        return p
    for kind, t in mod.TARGETS.items():
        if t is None:
            continue
        if not (isinstance(t, (tuple, list)) and len(t) == 2 and all(isinstance(x, str) for x in t)):
            p.append(f"{name}: TARGETS[{kind!r}] must be (tx, rx) hex header strings or None, found {t!r}")
    if not isinstance(mod.KIND_ORDER, (tuple, list)):
        p.append(f"{name}: KIND_ORDER must be a tuple of kinds, found {type(mod.KIND_ORDER).__name__}")
    else:
        for k in mod.KIND_ORDER:
            if k not in mod.TARGETS:
                p.append(f"{name}: KIND_ORDER lists kind {k!r}, which is not in TARGETS")

    # ── ITEMS ──
    if not isinstance(mod.ITEMS, dict) or not mod.ITEMS:
        p.append(f"{name}: ITEMS must be a non-empty dict")
        return p
    for i, it in mod.ITEMS.items():
        if not isinstance(it, dict):
            p.append(f"{name}: item {i!r} must be a dict, found {type(it).__name__}")
            continue
        kind = it.get("kind")
        if kind not in mod.TARGETS:
            p.append(f"{name}: item {i!r} has kind {kind!r}, which is not a key of TARGETS "
                     f"({', '.join(map(repr, mod.TARGETS))})")
            continue
        if kind not in tuple(mod.KIND_ORDER or ()):
            p.append(f"{name}: item {i!r} has kind {kind!r}, which KIND_ORDER never polls")
        if not isinstance(it.get("period"), (int, float)) or it["period"] < 0:
            p.append(f"{name}: item {i!r} needs a numeric period >= 0 (0 = every cycle), "
                     f"found {it.get('period')!r}")
        if not it.get("label"):
            p.append(f"{name}: item {i!r} needs a non-empty label")
        if mod.TARGETS[kind] is None:
            if "id" not in it or "secs" not in it:
                p.append(f"{name}: passive item {i!r} (kind {kind!r}) needs 'id' (CAN id) "
                         f"and 'secs' (capture window)")
        elif not it.get("cmd"):
            p.append(f"{name}: UDS item {i!r} (kind {kind!r}) needs a 'cmd' request string")

    # ── tiles ──
    if not isinstance(mod.TILES, (list, tuple)):
        p.append(f"{name}: TILES must be a list (possibly empty)")
    else:
        for t in mod.TILES:
            if not isinstance(t, dict) or "id" not in t or "items" not in t:
                p.append(f"{name}: every TILES entry needs 'id' and 'items', found {t!r}")
                continue
            for i in t["items"]:
                if i not in mod.ITEMS:
                    p.append(f"{name}: tile {t['id']!r} references unknown item {i!r}")
            if t["id"] not in mod.DEFAULT_SPAN:
                p.append(f"{name}: tile {t['id']!r} has no DEFAULT_SPAN entry")
    builtin = {t["id"] for t in mod.TILES if isinstance(t, dict) and "id" in t}
    for t in mod.DEFAULT_TILES:
        if not isinstance(t, dict) or "id" not in t:
            p.append(f"{name}: every DEFAULT_TILES entry needs an 'id', found {t!r}")
            continue
        if t["id"] not in builtin and t.get("signal") not in mod.SIGNALS:
            p.append(f"{name}: default signal tile {t['id']!r} names signal "
                     f"{t.get('signal')!r}, which is not in SIGNALS")

    # ── item bookkeeping ──
    for i, keys in mod.ITEM_KEYS.items():
        if i not in mod.ITEMS:
            p.append(f"{name}: ITEM_KEYS has an entry for unknown item {i!r}")
        if not isinstance(keys, (tuple, list)) or not all(isinstance(k, str) for k in keys):
            p.append(f"{name}: ITEM_KEYS[{i!r}] must be a tuple of record-key strings, found {keys!r}")
    if not isinstance(mod.WATCH, tuple):
        p.append(f"{name}: WATCH must be a tuple of record keys, found {type(mod.WATCH).__name__}")
    elif not all(isinstance(w, str) for w in mod.WATCH):
        p.append(f"{name}: WATCH must contain only record-key strings, found {mod.WATCH!r}")
    for i in mod.FAST_ONLY:
        if i not in mod.ITEMS:
            p.append(f"{name}: FAST_ONLY names unknown item {i!r}")

    # ── signals ──
    for k, s in mod.SIGNALS.items():
        if not isinstance(s, dict):
            p.append(f"{name}: signal {k!r} must be a dict, found {type(s).__name__}")
            continue
        if s.get("item") not in mod.ITEMS:
            p.append(f"{name}: signal {k!r} references unknown item {s.get('item')!r}")
        kind = s.get("kind", "number")
        if kind not in ("number", "bool", "text"):
            p.append(f"{name}: signal {k!r} has kind {kind!r}; expected number, bool or text")
        elif kind == "number":
            miss = [f for f in ("unit", "min", "max") if f not in s]
            if miss:
                p.append(f"{name}: number signal {k!r} is missing {', '.join(miss)}")
        if not s.get("label"):
            p.append(f"{name}: signal {k!r} needs a label")
        if s.get("unit") == "°F" and not (s.get("alt") and s.get("alt_unit") == "°C"):
            p.append(f"{name}: °F signal {k!r} must carry its °C twin as "
                     f"'alt' + 'alt_unit': '°C' (house rule: always °C and °F)")

    # ── history columns ──
    p += _validate_history(mod, name)

    # ── callables ──
    if not inspect.iscoroutinefunction(getattr(mod, "configure", None)):
        p.append(f"{name}: configure(elm) must be an async function")
    if not callable(getattr(mod, "decode", None)):
        p.append(f"{name}: decode(responses) must be callable")
    ap = getattr(mod, "apply_policy", None)
    if ap is not None and not callable(ap):
        p.append(f"{name}: apply_policy must be callable (or absent)")
    return p


def _validate_history(mod, name):
    p, hist_names, filters = [], {}, []
    raw = getattr(mod, "HISTORY_COLS", None)
    if raw is None:
        return p
    if not isinstance(raw, dict):
        return [f"{name}: HISTORY_COLS must be a dict column -> spec, found {type(raw).__name__}"]
    for col, spec in raw.items():
        if not (isinstance(col, str) and col.isidentifier()):
            p.append(f"{name}: HISTORY_COLS key {col!r} must be a plain SQL column name")
            continue
        if col in ("id", "ts", "ts_epoch", "adapter", "vehicle", "extra"):
            p.append(f"{name}: HISTORY_COLS may not redefine the built-in column {col!r}")
        if not isinstance(spec, dict):
            p.append(f"{name}: HISTORY_COLS[{col!r}] must be a dict, found {type(spec).__name__}")
            continue
        kind = spec.get("kind", "real")
        if kind not in _SQL_TYPE:
            p.append(f"{name}: HISTORY_COLS[{col!r}] kind {kind!r}; expected one of "
                     f"{', '.join(_SQL_TYPE)}")
        key = spec.get("key", col)
        if not (callable(key) or isinstance(key, str)):
            p.append(f"{name}: HISTORY_COLS[{col!r}] key must be a record key, a dotted "
                     f"index ('temps.0') or a callable(record), found {key!r}")
        h = spec.get("hist")
        if h is not None:
            if not isinstance(h, str):
                p.append(f"{name}: HISTORY_COLS[{col!r}] hist must be a string, found {h!r}")
            elif h in hist_names:
                p.append(f"{name}: HISTORY_COLS[{col!r}] and [{hist_names[h]!r}] both use the "
                         f"history name {h!r}")
            else:
                hist_names[h] = col
        if "round" in spec and not isinstance(spec["round"], int):
            p.append(f"{name}: HISTORY_COLS[{col!r}] round must be an int, found {spec['round']!r}")
        if spec.get("hist_f") and not h:
            p.append(f"{name}: HISTORY_COLS[{col!r}] has hist_f but no hist to derive it from")
        d = spec.get("daily")
        if d is not None:
            if not isinstance(d, dict):
                p.append(f"{name}: HISTORY_COLS[{col!r}] daily must be a dict "
                         f"{{'avg'|'min'|'max': output name}}, found {d!r}")
            else:
                for fn, out in d.items():
                    if fn not in ("avg", "min", "max"):
                        p.append(f"{name}: HISTORY_COLS[{col!r}] daily has aggregate {fn!r}; "
                                 f"expected avg, min or max")
                    if not (isinstance(out, str) and out.isidentifier()):
                        p.append(f"{name}: HISTORY_COLS[{col!r}] daily[{fn!r}] must name the "
                                 f"output column, found {out!r}")
        if spec.get("daily_filter"):
            filters.append(col)
        if "index" in spec and not (isinstance(spec["index"], str) and spec["index"].isidentifier()):
            p.append(f"{name}: HISTORY_COLS[{col!r}] index must be the index's name, "
                     f"found {spec['index']!r}")
    if len(filters) > 1:
        p.append(f"{name}: at most one HISTORY_COLS column may set daily_filter "
                 f"(found {', '.join(filters)})")
    # a graphable signal wants a column to graph
    cols = set(raw)
    for k, s in getattr(mod, "SIGNALS", {}).items():
        if isinstance(s, dict) and s.get("hist"):
            names = {sp.get("hist") for sp in raw.values() if isinstance(sp, dict)}
            names |= {sp.get("hist_f") for sp in raw.values() if isinstance(sp, dict)}
            if s["hist"] not in names and s["hist"] not in cols:
                p.append(f"{name}: signal {k!r} charts history key {s['hist']!r}, which no "
                         f"HISTORY_COLS entry produces")
    return p


if __name__ == "__main__":                      # python vehicles/__init__.py [name ...]
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import importlib as _il
    names = sys.argv[1:] or available()
    bad = 0
    for n in names:
        try:
            m = _il.import_module(f"vehicles.{n}")
        except ImportError as e:
            print(f"{n}: cannot import ({e})")
            bad += 1
            continue
        probs = validate_profile(m)
        if probs:
            bad += 1
            print(f"{n}: {len(probs)} problem(s)")
            for x in probs:
                print(f"  - {x}")
        else:
            print(f"{n}: OK ({m.TITLE}, {len(m.ITEMS)} items, {len(m.SIGNALS)} signals, "
                  f"{len(history_cols(m))} history columns)")
    sys.exit(1 if bad else 0)
