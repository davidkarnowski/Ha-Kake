# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline decoder tests — run with `./venv/bin/python -m pytest` (no car needed).

Fixture: tests/fixtures/lbc_raw_20260824.json — raw ELM327 lines for LBC groups
01-06 captured over BLE on 2026-08-24 (car IGN-ON, idle, ~35 °C pack).
"""
import json
import os

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import leaf_decoders as ld  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "lbc_raw_20260824.json")


@pytest.fixture(scope="module")
def raw():
    with open(FIXTURE) as f:
        return json.load(f)["groups"]


# ── helpers ──────────────────────────────────────────────────────────────

def test_c_to_f():
    assert ld.c_to_f(0) == 32.0
    assert ld.c_to_f(100) == 212.0
    assert ld.c_to_f(35) == 95.0
    assert ld.c_to_f(-40) == -40.0
    assert ld.c_to_f(None) is None


def test_fmt_temp():
    assert ld.fmt_temp(34) == "34 °C / 93 °F"
    assert ld.fmt_temp(17.8) == "17.8 °C / 64.0 °F"
    assert ld.fmt_temp(None) == "--"


def test_is_no_data():
    assert ld.is_no_data([])
    assert ld.is_no_data(["NO DATA"])
    assert ld.is_no_data(["CAN ERROR"])
    assert not ld.is_no_data(["7BB 10 29 61 01 FF FF F9 E9"])


# ── ISO-TP ───────────────────────────────────────────────────────────────

def test_parse_isotp_multiframe(raw):
    d = ld.parse_isotp(raw["2101"])
    assert len(d) == 39
    assert d[:4] == bytearray([0xFF, 0xFF, 0xF1, 0x18])


def test_parse_isotp_ignores_other_ids():
    lines = ["7BB 10 29 61 01 FF FF F9 E9", "7BC 21 02 87 FF FF FC 44 FF", "7BB 21 02 87 FF FF FC 44 FF"]
    d = ld.parse_isotp(lines)
    assert len(d) == 11


def test_parse_isotp_single_frame():
    d = ld.parse_isotp(["7BB 05 61 01 AA BB CC 00 00"])
    assert d == bytearray([0xAA, 0xBB, 0xCC])


def test_parse_isotp_garbage_lines():
    d = ld.parse_isotp(["SEARCHING...", "7BB 10 29 61 01 FF FF F9 E9", "BUFFER FULL", ">"])
    assert d == bytearray([0xFF, 0xFF, 0xF9, 0xE9])


# ── Group 01 ─────────────────────────────────────────────────────────────

def test_group01(raw):
    g = ld.decode_group01(ld.parse_isotp(raw["2101"]))
    assert g["hv_current1_a"] == pytest.approx(-3.727, abs=0.001)
    assert g["hv_current2_a"] == pytest.approx(-1.527, abs=0.001)
    assert g["pack_v"] == pytest.approx(383.87, abs=0.01)
    assert g["lv_volts"] == pytest.approx(12.677, abs=0.001)
    assert g["insulation_kohm"] == 885
    assert g["hx"] == pytest.approx(17.96)
    assert g["soc"] == pytest.approx(76.87, abs=0.01)
    assert g["capacity_ah"] == pytest.approx(23.1568, abs=0.0001)
    assert g["soh"] == pytest.approx(35.1, abs=0.05)


def test_group01_short_payload():
    assert ld.decode_group01(bytearray(10)) == {}


# ── Group 02 ─────────────────────────────────────────────────────────────

def test_group02(raw):
    cells = ld.decode_group02(ld.parse_isotp(raw["2102"]))
    assert len(cells) == 96
    assert cells[0] == 0x0F9C
    assert all(3500 < v < 4300 for v in cells)
    st = ld.cell_stats(cells)
    assert st["cell_count"] == 96
    assert st["cell_spread"] == st["cell_max"] - st["cell_min"]
    assert cells[st["cell_min_idx"]] == st["cell_min"]
    # pack sum from cells should agree with group-01 pack voltage within 1 V
    g01 = ld.decode_group01(ld.parse_isotp(raw["2101"]))
    assert abs(st["pack_v_cells"] - g01["pack_v"]) < 1.0


def test_group02_padding_stops():
    data = bytearray([0x0F, 0xA0] * 3 + [0xFF, 0xFF] + [0x0F, 0xA0])
    assert ld.decode_group02(data) == [4000, 4000, 4000]


# ── Group 03 ─────────────────────────────────────────────────────────────

def test_group03(raw):
    g = ld.decode_group03(ld.parse_isotp(raw["2103"]))
    cells = ld.decode_group02(ld.parse_isotp(raw["2102"]))
    # tentative: min/max here should be near the cell list extremes
    assert abs(g["g03_cell_min_mv"] - min(cells)) < 30
    assert abs(g["g03_cell_max_mv"] - max(cells)) < 30


# ── Group 04 ─────────────────────────────────────────────────────────────

def test_group04(raw):
    t = ld.decode_group04(ld.parse_isotp(raw["2104"]))
    assert [x["c"] for x in t] == [34, 35, 34, 36]
    assert [x["f"] for x in t] == [93.2, 95.0, 93.2, 96.8]
    assert t[0]["raw"] == 0x017C


def test_group04_negative_temp():
    data = bytearray([0x02, 0x00, 0xF6] * 4)  # -10 °C
    t = ld.decode_group04(data)
    assert t[0]["c"] == -10 and t[0]["f"] == 14.0


# ── Group 05 ─────────────────────────────────────────────────────────────

def test_group05(raw):
    g = ld.decode_group05(ld.parse_isotp(raw["2105"]))
    assert g["discharging"] is True
    assert g["current_a"] == pytest.approx(-1.580, abs=0.001)
    assert g["g05_cell_max_mv"] == 3965
    assert g["g05_cell_min_mv"] == 3953
    assert g["g05_insulation_kohm"] == 885
    assert len(g["cell_groups"]) == 10
    assert all(950 < v < 970 for v in g["cell_groups"])
    assert len(g["segment_deltas"]) == 10


def test_group05_current_agrees_with_group01(raw):
    g05 = ld.decode_group05(ld.parse_isotp(raw["2105"]))
    g01 = ld.decode_group01(ld.parse_isotp(raw["2101"]))
    assert abs(g05["current_a"] - g01["hv_current2_a"]) < 0.2


# ── Group 06 ─────────────────────────────────────────────────────────────

def test_group06(raw):
    g = ld.decode_group06(ld.parse_isotp(raw["2106"]))
    assert len(g["balancing"]) == 96
    assert all(0 <= f <= 3 for f in g["balancing"])
    assert g["balancing"][0] == 0   # 0x04 → first pair bits = 00
    assert g["balancing"][1] == 0
    assert g["balancing"][2] == 1   # 0x04 = 0000 0100 → third pair = 01
    assert g["balancing"][3] == 0


# ── Combined ─────────────────────────────────────────────────────────────

def test_decode_reading(raw):
    rec = ld.decode_reading(raw)
    assert rec["soc"] == pytest.approx(76.87, abs=0.01)
    assert rec["cell_count"] == 96
    assert rec["temps"] == [34, 35, 34, 36]
    assert rec["temps_f"] == [93.2, 95.0, 93.2, 96.8]
    assert rec["temp_avg_c"] == 34.8
    assert rec["temp_avg_f"] == pytest.approx(94.55, abs=0.06)
    assert rec["discharging"] is True
    # power is signed: negative while discharging; current is group-01 sensor 2, group 05 kept aside
    assert rec["current_a"] == pytest.approx(-1.527, abs=0.001)
    assert rec["g05_current_a"] == pytest.approx(-1.580, abs=0.001)
    assert rec["power_kw"] < 0
    assert rec["power_kw"] == pytest.approx(rec["pack_v"] * rec["current_a"] / 1000.0, abs=0.001)
    # pack_v comes from group 01, not the cell sum
    assert rec["pack_v"] == pytest.approx(383.87, abs=0.01)


def test_decode_reading_skips_no_data(raw):
    rec = ld.decode_reading({"2101": ["NO DATA"], "2104": raw["2104"]})
    assert "soc" not in rec
    assert rec["temps_f"] == [93.2, 95.0, 93.2, 96.8]
    assert "power_kw" not in rec


def test_decode_reading_power_falls_back_to_cell_sum(raw):
    rec = ld.decode_reading({"2102": raw["2102"], "2105": raw["2105"]})
    assert rec["pack_v"] == rec["pack_v_cells"]
    assert rec["current_a"] == pytest.approx(-1.580, abs=0.001)   # no group 01 → group 05 value
    assert rec["power_kw"] < 0
