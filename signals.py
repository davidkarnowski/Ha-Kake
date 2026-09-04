#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signal registry machinery — shared across vehicle profiles.

The *entries* (what the dashboard can display) come from the active vehicle
profile (vehicles/<name>.py, bound here by reader.set_vehicle() via use()).
This module owns what is vehicle-independent: the colour scales, the tile
renderer list, and the value/item resolvers the reader and UI share.

Both the reader (to know which items a user tile needs) and the browser
(/api/signals, to build the tile type/colour menus) read this. Adding a new
input is: decoder → item → profile SIGNALS entry → docs/SIGNALS.md → fixture
test. See docs/ADDING_SIGNALS.md.
"""

# Colour scales the UI knows. "stops" are [position 0..1, css colour].
COLOR_SCALES = {
    "soc":      {"label": "SOC (red → green)",       "stops": [[0, "#ef5350"], [0.2, "#ff9800"], [0.4, "#ffca28"], [0.6, "#4caf50"], [1, "#4fc3f7"]]},
    "good-high": {"label": "Higher is better",        "stops": [[0, "#ef5350"], [0.5, "#ffca28"], [1, "#4caf50"]]},
    "good-low":  {"label": "Lower is better",         "stops": [[0, "#4caf50"], [0.5, "#ffca28"], [1, "#ef5350"]]},
    "heat":      {"label": "Cold → hot",              "stops": [[0, "#42a5f5"], [0.4, "#4caf50"], [0.7, "#ffca28"], [1, "#ef5350"]]},
    "diverge":   {"label": "Draw ↔ charge",           "stops": [[0, "#ff9800"], [0.5, "#6b7a99"], [1, "#4fc3f7"]]},
    "mono":      {"label": "Accent only",             "stops": [[0, "#4fc3f7"], [1, "#4fc3f7"]]},
    "band":      {"label": "In-range band",           "stops": [[0, "#ef5350"], [0.15, "#4caf50"], [0.85, "#4caf50"], [1, "#ef5350"]]},
}

# Renderers the UI implements for scalar signals. Custom tiles keep their own.
TILE_TYPES = {
    "number":  "Big number",
    "ring":    "Ring (progress arc)",
    "arc":     "Arc gauge with needle",
    "dial":    "Round dial",
    "bar":     "Horizontal bar",
    "thermo":  "Vertical bar / thermometer",
    "battery": "Battery icon",
    "line":    "Line graph (history)",
    "area":    "Area graph (history)",
    "bars":    "Bar graph (history)",
    "text":    "Text / state",
    "lamp":    "On / off lamp",
}

SIGNALS = {}


def use(vehicle):
    """Bind the active vehicle profile's registry (reader.set_vehicle calls this)."""
    global SIGNALS
    SIGNALS = vehicle.SIGNALS


def get_value(record, key):
    """Resolve a signal key (with optional '.index') against a state record."""
    if "." in key:
        base, idx = key.split(".", 1)
        seq = record.get(base)
        try:
            return seq[int(idx)]
        except (TypeError, IndexError, ValueError):
            return None
    return record.get(key)


def signal_item(sig):
    s = SIGNALS.get(sig)
    return s["item"] if s else None


# default binding so `import signals` alone works (tests, tools)
from vehicles import get_vehicle as _gv   # noqa: E402
use(_gv())
