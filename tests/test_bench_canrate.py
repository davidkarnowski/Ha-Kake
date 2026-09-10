# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""tools/bench_canrate.py — the parsing and the table, plus one short live run.

The bench measures the laptop against a virtual bus; the numbers it prints
are not pinned here (they are the laptop's), only that it computes and
formats them correctly and runs end to end.
"""

import asyncio
import os
import sys

import pytest

from conftest import ROOT  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))

import bench_canrate as bc  # noqa: E402


def test_parse_loads():
    assert bc.parse_loads("0.1,0.5,1") == [0.1, 0.5, 1.0]
    assert bc.parse_loads(" 2 , -1") == [1.0, 0.0]
    assert bc.parse_loads("") == list(bc.DEFAULT_LOADS)


def test_percentile():
    assert bc.percentile([], 0.9) is None
    assert bc.percentile([5, 1, 3], 0.0) == 1 and bc.percentile([5, 1, 3], 1.0) == 5
    assert bc.percentile(list(range(1, 11)), 0.9) == 9


def test_summarise_computes_the_row():
    row = bc.summarise(0.5, [0.001, 0.002, 0.003, 0.010], [0.012, 0.014], frames=1700, wall=1.0,
                       cpu_total=0.40, cpu_ecu=0.12, hits={"p421": (10, 10, 10), "p5A9": (10, 10, 4)},
                       expected_fps=1044.2, misses=1, broadcast=1044)
    assert row["intake_fps"] == 1700.0 and row["broadcast_fps"] == 1044.0 and row["expected_fps"] == 1044.2
    assert row["sched_median_ms"] == 2.5 and row["sched_p90_ms"] == 10.0
    assert row["full_median_ms"] == 13.0 and row["full_cycles"] == 2
    assert row["cpu_total_pct"] == 40.0 and row["cpu_ecu_pct"] == 12.0 and row["cpu_reader_pct"] == 28.0
    assert row["hits"]["p5A9"] == {"n": 10, "at_secs": 100.0, "at_0_2s": 40.0}
    assert row["uds_misses"] == 1
    empty = bc.summarise(1.0, [], [], 0, 0.0, 0.0, 0.0, {}, 0.0)
    assert empty["intake_fps"] is None and empty["sched_median_ms"] is None


def test_format_table_is_markdown():
    row = bc.summarise(1.0, [0.002], [0.013], 1693, 1.0, 0.39, 0.125, {"p421": (5, 5, 5)}, 1692.7, broadcast=1693)
    text = bc.format_table([row])
    lines = text.splitlines()
    assert lines[0].startswith("| bus load |") and lines[1].startswith("|---|")
    assert "| 1 | 1692.7 | 1693 | 1693 | 2 / 2 | 13 / 13 | 39 / 12.5 / 26.5 | 0 |" in text
    assert "| p421 |" in text and "100 / 100" in text
    assert "VIRTUAL BUS" in bc.header(8.0, "drive")


def test_a_short_live_bench_runs_end_to_end(leaf_profile):
    row = asyncio.run(bc.bench_one(0.2, 1.2, "idle", 1, lambda *a: None, interval=0.2))
    assert row["cycles"] >= 2 and row["intake_fps"] > 100
    assert row["broadcast_fps"] == pytest.approx(row["expected_fps"], rel=0.3)
    assert row["sched_median_ms"] is not None and row["cpu_total_pct"] is not None
    assert "p421" in row["hits"] and row["hits"]["p421"]["at_0_2s"] > 50
    assert "lbc01" in row["items"]
