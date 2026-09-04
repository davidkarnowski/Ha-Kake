# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Leaf's current fusion, and the wrap that used to poison it.

Group 05's current field is a signed 16-bit count / 1024, so it wraps at
±32.0 A (resolved on the 2026-09-03 drive; docs/SIGNALS.md). apply_policy
learns `s2_offset = group05 - sensor2` to correct sensor 2's dead zone near
zero — but it used to learn from every fresh sample, so above the rail it was
learning the 64 A fold instead. Over that drive the fused current strayed from
sensor 2 by a median 5.9 A and up to 41 A while moving, and the
`discharging and cur > 0 -> 0` clamp then zeroed many driving rows outright.
"""
import pytest

import vehicles.leaf_ze0 as leaf


def _cache(s2, g05, pack_v=380.0):
    return {"current_a": s2, "hv_current2_a": s2, "g05_current_a": g05,
            "pack_v": pack_v, "discharging": s2 < 0}


def test_offset_is_learned_from_a_believable_sample():
    """Near zero the two reads agree and the small difference is a real offset."""
    state = {}
    c = _cache(-1.27, -1.18)
    leaf.apply_policy(c, {}, state)
    # the first sample is stored unrounded; the EMA rounds thereafter
    assert state["s2_offset"] == pytest.approx(-1.18 - -1.27)
    assert c["s2_offset_stale"] is False
    assert c["current_fused"] is True


def test_a_wrapped_sample_never_becomes_the_offset():
    """The regression. Sensor 2 at -34.11 A, group 05 folded to +31.71 A: the
    65.8 A difference is the wrap, not an offset, and must not be learned."""
    state = {}
    leaf.apply_policy(_cache(-1.27, -1.18), {}, state)      # a good offset first
    good = state["s2_offset"]

    c = _cache(-34.11, 31.71)                                # the real pair, from the drive
    leaf.apply_policy(c, {}, state)
    assert state["s2_offset"] == good, "learned from a wrapped sample"
    assert c["s2_offset_stale"] is True
    # and the fused value stays close to the sensor that has no ceiling
    assert abs(c["current_a"] - -34.11) < 1.0


def test_a_big_difference_in_band_is_also_refused():
    """Both reads inside the rail but far apart means a fast transient caught
    between two polls ~0.5-1 s apart, not a stable offset."""
    state = {}
    leaf.apply_policy(_cache(-1.27, -1.18), {}, state)
    good = state["s2_offset"]
    leaf.apply_policy(_cache(-28.0, -5.0), {}, state)        # 23 A apart, both in band
    assert state["s2_offset"] == good
    assert state["s2_offset_stale"] is True


def test_a_sample_at_the_rail_is_refused_even_if_the_pair_looks_close():
    """31.9 A is a hair under the fold; a pair that agrees there is luck."""
    state = {}
    leaf.apply_policy(_cache(31.9, 31.7), {}, state)
    assert state.get("s2_offset") is None


def test_driving_current_is_not_zeroed_by_the_discharging_clamp():
    """With the offset uncorrupted, a real discharge stays a real discharge."""
    state = {}
    leaf.apply_policy(_cache(-1.27, -1.18), {}, state)
    c = _cache(-60.54, -15.53)                               # peak of the drive
    leaf.apply_policy(c, {}, state)
    assert c["current_a"] < -55.0
    assert c["power_kw"] < -20.0
