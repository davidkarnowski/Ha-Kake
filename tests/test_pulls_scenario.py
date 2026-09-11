# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`pulls`: five accelerations in a row, for watching the pack sag in 3D.

Shaped from the owner's drive of 2026-09-09, 14:05-14:08 PDT, whose 207 rows
each carry a cell set. What is pinned here is the calibration against that
stretch, because the scenario's whole point is that the sag on screen is the
one the car produced: each pull's peak current, the cell floor and the spread
it reaches, and the rest state it returns to. The pedal figures in the JSON
were solved for those peaks, so a change to the model's motor term will move
them and this test will say so.

Nothing here is evidence about a car. The numbers it compares against are,
but this is the simulator reproducing them.
"""

import json
import os

import pytest

from conftest import ROOT
from simulator import make_sim, scenario_names

SCENARIO = os.path.join(ROOT, "simulator", "scenarios", "pulls.json")

# (label, window, real peak A, real cell floor mV) from the 2026-09-09 stretch;
# the fourth is the -265 A case reported on 2026-09-08 (OWNER REPORT).
PULLS = [("moderate", (5, 17), -96, 3683),
         ("hard", (28, 42), -228, 3345),
         ("medium", (53, 65), -157, 3552),
         ("hardest", (76, 94), -265, None),
         ("mild", (105, 117), -56, 3765)]


def run_scenario(seconds=133.0, dt=0.5):
    sim = make_sim(vehicle="leaf_ze0", scenario="pulls", seed=7)
    out, t = [], 0.0
    while t < seconds:
        sim.step(dt); t += dt
        cells = sim.model.cells()
        out.append((t, sim.state()["current_a"], min(cells), max(cells) - min(cells)))
    return out


def test_it_ships_and_is_declarative():
    assert "pulls" in scenario_names()
    doc = json.load(open(SCENARIO))
    assert doc["vehicle"] == "leaf_ze0" and doc["seed"] == 7
    # the resistance is the regression through the real rows, not a guess
    assert doc["knobs"]["internal_resistance_ohm"] == 0.186
    for word in ("MEASURED", "ASSERTED", "2026-09-09"):
        assert word in doc["description"], word
    assert "cell_spread_mv is STEPPED BY THIS TIMELINE" in doc["description"], \
        "the spread does not emerge from per-pair resistance, and the scenario must say so"


@pytest.mark.parametrize("label,window,real_a,floor", PULLS)
def test_each_pull_reaches_the_current_the_car_reached(label, window, real_a, floor):
    lo, hi = window
    deep = min((r for r in run_scenario() if lo <= r[0] <= hi), key=lambda r: r[1])
    assert abs(deep[1] - real_a) <= 3, f"{label}: {deep[1]:.1f} A against the car's {real_a} A"
    if floor is not None:                      # within 60 mV of the observed cell floor
        assert abs(deep[2] - floor) <= 60, f"{label}: cell floor {deep[2]} against {floor} mV"


def test_the_spread_opens_under_load_and_closes_at_rest():
    """The real stretch fits spread_mV = 32 + 1.06 x |I|: 35 mV parked, 274 mV at
    -228 A. A flat spread would paint every pair the same colour in the 3D tile,
    which is the thing this scenario exists to avoid."""
    rows = run_scenario()
    rest = [r for r in rows if abs(r[1]) < 1 and r[0] > 20]
    assert rest, "the car should be sitting still somewhere in the run"
    assert all(r[3] <= 45 for r in rest), "parked, the pack should look tight"
    for _, (lo, hi), real_a, _ in PULLS:
        deep = min((r for r in rows if lo <= r[0] <= hi), key=lambda r: r[1])
        want = 32 + 1.06 * abs(deep[1])
        assert abs(deep[3] - want) <= 25, f"spread {deep[3]} against the fit's {want:.0f} mV"
    hardest = min(rows, key=lambda r: r[1])
    assert hardest[3] > 250, "the deepest pull should open the pack right up"
    assert hardest[2] < 3350, "and put the lowest pair near the floor the car saw"
