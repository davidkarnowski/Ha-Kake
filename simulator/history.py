# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bulk history generation — months of plausible rows in seconds.

Why this exists
---------------
The rest of `simulator/` answers "what does the dashboard do when a cell
degrades?". This module answers a different and, for day-to-day work, more
common question: **"what does this chart look like with six months of data
behind it?"**  Iterating on the degradation view, the history chart or a
report needs a populated database, and waiting six months (or running the
reader in real time) is not a way to iterate.

So: drive the same `LeafModel` at coarse time steps through a synthetic
calendar — commutes, errands, overnight charges, idle days, a slowly fading
pack — and write every sample through `Store.insert_reading()`, exactly the
call the reader makes, with exactly the record shape the profile's `decode()`
produces. Every existing API route and chart then works unmodified.

Honesty
-------
**Nothing here is a reading from any vehicle.** Generated databases are
stamped in the `meta` table (`synthetic = true`), every row carries
`adapter = 'sim-generated'` and `simulated: true` in its `extra` bag, and the
generator refuses to open the owner's real database at all. If you are
looking at a chart and cannot tell whether it is real, query
`SELECT value FROM meta WHERE key='synthetic'`.

What is modelled and what is invented
-------------------------------------
* The **charge shape** comes from the model (see the CHARGERS comment in
  `model.py`); it was fitted to real sessions.
* The **rhythm** — 07:xx departures, evening charges, a tenth of days unused —
  is invented wholesale. It is a plausible pattern, not this owner's pattern.
* The **fade** is a smooth curve with noise on top. Real degradation is
  lumpier and depends on how the car was used; this is here so the chart has
  a trend to draw, not so anyone can forecast anything.
"""

import datetime as dt
import json
import math
import os
import random
import sys

from . import model
from .units import c_to_f   # noqa: F401  (kept for callers that imported it from here)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MPH_PER_KMH = 0.621371

# The generator understands one profile's physics. A second vehicle needs its
# own calendar and its own model, not a flag.
SUPPORTED = ("leaf_ze0",)

DEFAULT_OUT = os.path.join(_ROOT, "web", "sim_history.db")


# ── the synthetic calendar ───────────────────────────────────────────────

class Climate:
    """Seasonal + diurnal outside air temperature.

    A mild coastal-California year: annual mean 16 °C, ±10 °C between winter
    and late summer, ±6 °C between dawn and mid-afternoon. Invented; it exists
    so the temperature charts have a season in them.
    """

    def __init__(self, mean_c=16.0, season_amp_c=9.0, diurnal_amp_c=6.0, peak_doy=205):
        self.mean = mean_c
        self.season = season_amp_c
        self.diurnal = diurnal_amp_c
        self.peak_doy = peak_doy

    def at(self, when):
        doy = when.timetuple().tm_yday
        hour = when.hour + when.minute / 60.0
        seasonal = self.season * math.cos(2 * math.pi * (doy - self.peak_doy) / 365.0)
        daily = self.diurnal * math.cos(2 * math.pi * (hour - 15.0) / 24.0)
        return self.mean + seasonal + daily


class Trend:
    """The slow arcs the degradation chart exists to show.

    `f` runs 0 -> 1 across the generated period. Capacity fades on a t^0.75
    curve (fast at first, then slower — the usual shape for calendar plus
    cycle loss), HX follows it, and the cell spread widens as the pack ages.
    A per-day wobble sits on top so a daily average is not a perfectly smooth
    line; per-reading noise is added separately, in the sampler.
    """

    def __init__(self, rng, capacity_start=24.8, capacity_end=23.2,
                 hx_start=20.5, hx_end=17.9, spread_start=26.0, spread_end=34.0):
        self.rng = rng
        self.cap0, self.cap1 = capacity_start, capacity_end
        self.hx0, self.hx1 = hx_start, hx_end
        self.sp0, self.sp1 = spread_start, spread_end

    def at(self, f):
        f = max(0.0, min(1.0, f))
        shape = f ** 0.75
        cap = self.cap0 + (self.cap1 - self.cap0) * shape
        hx = self.hx0 + (self.hx1 - self.hx0) * shape
        spread = self.sp0 + (self.sp1 - self.sp0) * f
        return {
            "capacity_ah": max(0.5, cap + self.rng.gauss(0, 0.045)),
            "hx": max(0.5, hx + self.rng.gauss(0, 0.09)),
            "cell_spread_mv": max(4.0, spread + self.rng.gauss(0, 1.4)),
            "insulation_kohm": max(120.0, 880.0 + self.rng.gauss(0, 28.0)),
        }


def plan_day(rng, day, weekday):
    """One day's activities as a list of dicts, in start order.

    kinds: "drive" (mins, avg_mph, label) and "charge" (charger, target_soc).
    Charges are proposed; the sampler decides whether the car actually needs
    one and stops when the target is reached.
    """
    r = rng.random()
    if r < 0.07:
        return []                                   # the car did not move today
    acts = []

    def drive(at_min, mins, mph, label):
        acts.append({"kind": "drive", "at": at_min, "mins": mins,
                     "mph": mph, "label": label})

    if weekday and rng.random() > 0.12:
        out = 7 * 60 + rng.randint(-40, 55)
        drive(out, rng.randint(11, 21), rng.uniform(29, 41), "commute")
        back = 17 * 60 + rng.randint(-70, 100)
        drive(back, rng.randint(11, 23), rng.uniform(28, 42), "commute")
        if rng.random() < 0.28:
            drive(back + rng.randint(70, 180), rng.randint(8, 20),
                  rng.uniform(22, 34), "errand")
    else:
        t = 9 * 60 + rng.randint(0, 200)
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3])):
            drive(t, rng.randint(12, 38), rng.uniform(26, 52), "errand")
            t += rng.randint(100, 320)
            if t > 21 * 60:
                break

    if not acts:
        return []

    # A long day out occasionally ends at a CHAdeMO post rather than at home.
    if rng.random() < 0.035:
        last = acts[-1]
        acts.append({"kind": "charge", "at": last["at"] + last["mins"] + rng.randint(5, 25),
                     "charger": "dcfc", "target": 80.0, "label": "DC fast"})

    last = [a for a in acts if a["kind"] == "drive"][-1]
    home = last["at"] + last["mins"] + rng.randint(20, 150)
    charger = "l1" if rng.random() < 0.22 else "l2"
    acts.append({"kind": "charge", "at": min(home, 23 * 60 + 30), "charger": charger,
                 "target": 100.0 if rng.random() < 0.7 else rng.uniform(82, 95),
                 "label": "home"})
    return sorted(acts, key=lambda a: a["at"])


# ── record shaping ───────────────────────────────────────────────────────
#
# The mapping from a model state to a reader record lives in
# `simulator.model.record_from_state` now (it is what `LeafModel.record()`
# and `/sim/record` use too, and it is held equal to encode -> decode by
# tests/test_sim_record.py). This wrapper only adds the generator's stamps.

def record_from_state(st, cells=False):
    """A reader-shaped record from one simulator state dict, stamped as
    generated history."""
    rec = model.record_from_state(st, cells=cells)
    rec.update({
        # the stamp, in every single row
        "simulated": True,
        "generated": True,
        "sim_source": "hakake_sim --generate",
    })
    return rec


# ── the generator ────────────────────────────────────────────────────────

class Generator:
    """Walks a synthetic calendar and writes rows through a real `Store`."""

    ADAPTER = "sim-generated"

    def __init__(self, sim, store, rng, climate, trend, sample_s=120.0,
                 idle_sample_s=1800.0, cells_per_day=4):
        self.sim = sim
        self.m = sim.model
        self.store = store
        self.rng = rng
        self.climate = climate
        self.trend = trend
        self.sample_s = float(sample_s)
        self.idle_sample_s = float(idle_sample_s)
        self.cells_every_s = 86400.0 / max(1, int(cells_per_day))
        self.now = None                      # naive local datetime cursor
        self.hard_end = dt.datetime.max
        self.rows = self.cell_rows = self.events = 0
        self._last_cells = None
        self._watch = {}
        self._drives = self._charges = self._idle_days = 0
        self._kwh_in = 0.0

    # ── writing ──────────────────────────────────────────────────────────

    def _emit(self, force_cells=False):
        want_cells = force_cells or (
            self._last_cells is None
            or (self.now - self._last_cells).total_seconds() >= self.cells_every_s)
        st = self.sim.state()
        rec = record_from_state(st, cells=want_cells)
        self.store.insert_reading(rec, ts=self.now, adapter=self.ADAPTER)
        self.rows += 1
        if want_cells:
            self.cell_rows += len(rec["cells"])
            self._last_cells = self.now
        self._transitions(rec)

    def _transitions(self, rec):
        """The reader writes an `events` row whenever a watched value changes;
        so does this. Same names, same normalisation, same table."""
        for name in self.store.vehicle.WATCH:
            cur = rec.get(name)
            prev = self._watch.get(name, "__unset__")
            if cur is None or cur == prev:
                continue
            self.store.insert_event(name, cur, None if prev == "__unset__" else prev,
                                    ts=self.now)
            self._watch[name] = cur
            self.events += 1

    # ── clock ────────────────────────────────────────────────────────────

    def _advance(self, seconds):
        self.sim.step(seconds)
        self.now = self.now + dt.timedelta(seconds=seconds)

    def _run(self, until, step_s, emit=True):
        """Step to `until` in `step_s` chunks, writing a row at each."""
        until = min(until, self.hard_end)
        while self.now < until:
            chunk = min(step_s, (until - self.now).total_seconds())
            if chunk <= 0:
                break
            self._advance(chunk)
            self.m.k["ambient_c"] = self.climate.at(self.now)
            if emit:
                self._emit()

    # ── the three states the car is ever in ──────────────────────────────

    def park(self, until):
        k = self.m.k
        # Parked and asleep: start_state "off" puts the model on the load
        # table's base_off row (a few watts — a real ZE0 loses on the order
        # of 1 %/day sitting). Nothing extra on top.
        k.update({"charging": False, "current_a": 0.0, "load_kw": 0.0,
                  "speed_mph": 0.0, "gear": "P", "handbrake": True,
                  "start_state": "off", "locked": True, "hvac_on": False,
                  "hvac_ac_on": False, "hvac_fan_speed": 0, "heater_level": 0,
                  "headlights": False, "parking_lights": False, "brake_pct": 0.0,
                  "accel_pedal_pct": 0.0,
                  "lv_volts": round(self.rng.uniform(12.35, 12.72), 2),
                  "sunload": 0})
        for d in model.DOOR_NAMES:
            k[f"door_{d}"] = False
        self._run(until, self.idle_sample_s)

    def drive(self, mins, mph, label):
        """One trip: unlock, doors, pull away, cruise with regen, park.

        The generator sets what a driver sets — speed, accelerator, brake,
        climate — and `LeafModel.current()` turns that into pack current
        through the load table and the (ASSERTED) motor model. The random
        walk on speed and pedal is here so a power chart has the *texture*
        of a drive rather than a step; the watts are the model's.
        """
        k = self.m.k
        amb0 = self.climate.at(self.now)
        cooling = amb0 > 24
        heating = amb0 < 12
        # Nobody starts a trip the pack cannot finish. This car has ~25 miles
        # of range at 35 % SOH, so the calendar's trip lengths get clipped to
        # what is actually in the battery — which is what keeps a generated
        # month from walking the SOC down to zero. Budget: the road load with
        # a driver's pedal on top (~1.3x cruise) plus the climate system.
        trip_kw = model.cruise_kw(mph) * 1.3 + 1.8 * cooling + 2.6 * heating
        avail_kwh = max(0.0, (k["soc"] - 12.0) / 100.0 * k["capacity_ah"]
                        * self.m.ocv_pack() / 1000.0)
        mins = max(3.0, min(float(mins), avail_kwh / max(0.5, trip_kw) * 60.0 * 0.85))
        end = min(self.now + dt.timedelta(minutes=mins), self.hard_end)
        k.update({"charging": False, "load_kw": 0.0, "current_a": 0.0, "locked": False,
                  "door_driver": True, "start_state": "ready",
                  "lv_volts": round(self.rng.uniform(13.8, 14.15), 2),
                  "hvac_on": cooling or heating,
                  "hvac_ac_on": cooling,
                  "hvac_fan_speed": 3 if (cooling or heating) else 0,
                  "heater_level": self.rng.randint(8, 22) if heating else 0,
                  "sunload": self.rng.randint(40, 210) if 8 < self.now.hour < 18 else 0,
                  "headlights": not (7 < self.now.hour < 18),
                  "parking_lights": not (7 < self.now.hour < 18)})
        self._emit()                                  # door open, still in P
        k.update({"door_driver": False, "gear": "D", "handbrake": False})
        first = True
        while self.now < end:
            frac = 1.0 - (end - self.now).total_seconds() / max(1.0, mins * 60.0)
            if first:
                sp, pedal, brake = mph * 0.45, 35.0, 0.0        # pulling away
                first = False
            elif frac > 0.88:
                sp, pedal, brake = mph * 0.3, 0.0, 25.0         # slowing, regen
            elif self.rng.random() < 0.18:
                sp, pedal, brake = (mph * self.rng.uniform(0.55, 0.85), 0.0,   # lift-off
                                    self.rng.uniform(0.0, 20.0))
            else:
                sp = mph * self.rng.uniform(0.82, 1.12)
                pedal, brake = self.rng.uniform(4.0, 9.0), 0.0   # holding speed
            k["speed_mph"] = max(0.0, min(120.0, round(sp, 1)))
            k["accel_pedal_pct"] = round(pedal, 1)
            k["brake_pct"] = round(brake, 1)
            self._advance(min(self.sample_s, (end - self.now).total_seconds() or 1.0))
            k["ambient_c"] = self.climate.at(self.now)
            self._emit()
            if k["soc"] <= 4.0:
                break                                 # coasted in on the turtle
        k.update({"speed_mph": 0.0, "gear": "P", "handbrake": True, "brake_pct": 0.0,
                  "accel_pedal_pct": 0.0, "door_driver": True})
        self._emit()
        self._drives += 1

    def charge(self, charger, target, deadline):
        """Plug in and let the model's own curve do the tapering."""
        k = self.m.k
        soc0 = k["soc"]
        self.sim.set(charger=charger)
        k.update({"charging": True, "gear": "P", "handbrake": True, "speed_mph": 0.0,
                  "start_state": "off", "locked": True, "hvac_on": False,
                  "hvac_ac_on": False, "hvac_fan_speed": 0, "heater_level": 0,
                  "lv_volts": round(self.rng.uniform(13.6, 14.0), 2)})
        for d in model.DOOR_NAMES:
            k[f"door_{d}"] = False
        step = self.sample_s if charger != "dcfc" else min(self.sample_s, 60.0)
        deadline = min(deadline, self.hard_end)
        while self.now < deadline and k["soc"] < target - 0.05:
            self._advance(min(step, (deadline - self.now).total_seconds() or 1.0))
            k["ambient_c"] = self.climate.at(self.now)
            self._emit()
        gained = max(0.0, k["soc"] - soc0)
        self._kwh_in += gained / 100.0 * k["capacity_ah"] * self.m.ocv_pack() / 1000.0
        k.update({"charging": False, "current_a": 0.0,
                  "lv_volts": round(self.rng.uniform(12.4, 12.7), 2)})
        self._charges += 1

    # ── the calendar ─────────────────────────────────────────────────────

    def run(self, start, end):
        self.now = start
        self.hard_end = end          # no row is ever stamped after this
        total_days = max(1.0, (end - start).total_seconds() / 86400.0)
        day = start.date()
        while self.now < end:
            midnight = dt.datetime.combine(day, dt.time()) + dt.timedelta(days=1)
            day_end = min(midnight, end)
            f = (self.now - start).total_seconds() / 86400.0 / total_days
            for name, value in self.trend.at(f).items():
                self.m.k[name] = value
            self.m.k["soh"] = round(self.m.k["capacity_ah"] / model.NOMINAL_CAPACITY_AH * 100.0, 2)

            acts = plan_day(self.rng, day, day.weekday() < 5)
            if not acts:
                self._idle_days += 1
            for i, a in enumerate(acts):
                at = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=a["at"])
                if at >= day_end:
                    break
                if at > self.now:
                    self.park(min(at, day_end))
                if self.now >= day_end:
                    break
                if a["kind"] == "drive":
                    if self.m.k["soc"] < 8:
                        continue                      # not enough charge to go anywhere
                    self.drive(a["mins"], a["mph"], a["label"])
                else:
                    nxt = acts[i + 1]["at"] if i + 1 < len(acts) else 24 * 60 + 7 * 60
                    limit = min(dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=nxt),
                                self.now + dt.timedelta(hours=14))
                    if a["charger"] == "dcfc" or self.m.k["soc"] < 72 or self.rng.random() < 0.25:
                        self.charge(a["charger"], a["target"], min(limit, end))
            self.park(day_end)
            day += dt.timedelta(days=1)

    def summary(self):
        return {"rows": self.rows, "cell_rows": self.cell_rows, "events": self.events,
                "drives": self._drives, "charges": self._charges,
                "idle_days": self._idle_days}


# ── entry point ──────────────────────────────────────────────────────────

def _refuse_real_db(path):
    """Never, under any circumstance, write generated rows into the car's
    database. The owner has 12,000+ irreplaceable real readings in it."""
    import store as store_mod
    real = os.path.realpath(store_mod.DEFAULT_DB)
    want = os.path.realpath(path)
    if want == real or os.path.basename(want) == os.path.basename(real):
        raise ValueError(
            f"refusing to generate into {path!r}: that is (or is named like) the "
            f"real database, {store_mod.DEFAULT_DB}. Pass --out web/sim_history.db "
            f"or any other path.")


def state_path_for(db_path):
    """The `_state.json` sibling of a generated database — what the dashboard
    serves as /api/status. `web/sim_x.db` -> `web/sim_x_state.json`, which the
    existing .gitignore rules already cover."""
    base = db_path[:-3] if db_path.endswith(".db") else db_path
    return base + "_state.json"


def generate(out=None, days=180, vehicle="leaf_ze0", seed=1, end=None,
             sample_s=120.0, idle_sample_s=1800.0, cells_per_day=4,
             capacity_start=None, capacity_end=None, fresh=True, log=None):
    """Write `days` of synthetic history to the SQLite database at `out`.

    Returns a summary dict. Deterministic for a given (seed, days, sample_s,
    end) — an agent iterating on a chart sees the same data each run, so a
    visual difference means the chart changed and nothing else did.
    """
    from store import Store                          # web/ is on sys.path above

    log = log or (lambda *a: None)
    out = os.path.abspath(out or DEFAULT_OUT)
    if vehicle not in SUPPORTED:
        raise ValueError(f"history generation is implemented for "
                         f"{', '.join(SUPPORTED)}, not {vehicle!r}")
    _refuse_real_db(out)
    if fresh:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(out + suffix)
            except FileNotFoundError:
                pass
        try:
            os.remove(state_path_for(out))
        except FileNotFoundError:
            pass

    end = end or dt.datetime.now().replace(microsecond=0, second=0, minute=0)
    start = end - dt.timedelta(days=float(days))

    rng = random.Random(seed)
    from . import make_sim
    sim = make_sim(vehicle=vehicle, seed=seed)
    climate = Climate()
    trend = Trend(rng,
                  capacity_start=24.8 if capacity_start is None else capacity_start,
                  capacity_end=23.2 if capacity_end is None else capacity_end)

    store = Store(out, vehicle=vehicle)
    # A generated database is a scratch file: durability is worth nothing and
    # a commit per row is the whole cost of the run.
    store.conn.execute("PRAGMA synchronous=OFF")
    _stamp(store, seed=seed, days=days, vehicle=vehicle, start=start, end=end)

    sim.set(soc=rng.uniform(45, 80), pack_temp_c=climate.at(start),
            cabin_temp_c=climate.at(start), ambient_c=climate.at(start),
            evap_c=climate.at(start), odometer_mi=61000.0 - days * 22.0,
            noise=0.6, weak_cell_offset_mv=0.0)
    gen = Generator(sim, store, rng, climate, trend, sample_s=sample_s,
                    idle_sample_s=idle_sample_s, cells_per_day=cells_per_day)
    log(f"  generating {days} days into {out} (seed {seed}) ...")
    gen.run(start, end)

    sm = gen.summary()
    sm.update({"path": out, "state_path": state_path_for(out), "seed": seed,
               "days": days, "vehicle": vehicle, "synthetic": True,
               "start": start.isoformat(), "end": end.isoformat()})
    _write_state(store, gen, sm)
    store.conn.commit()
    store.close()
    return sm


def _stamp(store, seed, days, vehicle, start, end):
    """Mark the file as synthetic, loudly and in the file itself, so that
    months from now nobody mistakes it for research data."""
    rows = {
        "synthetic": "true",
        "warning": "SYNTHETIC DATA — generated by hakake_sim --generate. "
                   "Not a reading from any vehicle. Do not cite, do not merge "
                   "into web/leaf_battery.db.",
        "generated_by": "simulator/history.py",
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z"),
        "generator_seed": str(seed),
        "generator_days": str(days),
        "generator_vehicle": vehicle,
        "generator_span": f"{start.isoformat()} .. {end.isoformat()}",
    }
    with store.conn:
        store.conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(rows.items()))
    store.start_session(Generator.ADAPTER, note="synthetic history (hakake_sim --generate)")


def _write_state(store, gen, summary):
    """A `_state.json` beside the database, shaped like the reader's, so
    `web/app.py --db <that database>` has a live-looking /api/status to serve
    without a reader running."""
    latest = record_from_state(gen.sim.state())
    latest.update({
        "status": "ok",
        "timestamp": _iso(gen.now),
        "last_ok": _iso(gen.now),
        "state_time": _iso(gen.now),
        "readings": gen.rows,
        "cycle_s": round(gen.sample_s, 1),
        "adapter_type": Generator.ADAPTER,
        "adapter_name": f"generated history ({summary['days']} d, seed {summary['seed']})",
        "adapter_port": "sim:generate",
        "replay": False,
        "simulated": True,
        "generated": True,
        "message": "SYNTHETIC DATA — generated history, not a reading from any vehicle",
    })
    path = state_path_for(store.path)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(latest, f)
    os.replace(tmp, path)


def _iso(naive_local):
    return naive_local.astimezone(dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
