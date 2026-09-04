#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small vehicle-independent helpers shared across the project.

Nothing here knows about a particular car. It exists so the generic layers
(the transport, the reader, the web app) stop reaching into `leaf_decoders`
for things that were never Leaf-specific in the first place — the temperature
formatter was, until 2026-09, imported into the vehicle-agnostic reader from a
module named after one car. `leaf_decoders` re-exports both temperature
helpers, so its long-standing callers are unaffected.
"""

import os


def env(name, *aliases, default=None):
    """Environment variable, with backward-compatible older names.

    The project standardised on a ``HAKAKE_`` prefix when it grew past one car
    (``HAKAKE_VEHICLE`` came first). The older ``LEAF_*`` names still work —
    silently, because they are documented in the README and in use on the
    owner's machine — but the new name wins when both are set. Empty is
    treated as unset, so ``HAKAKE_X= LEAF_X=abc`` still finds ``abc``.
    """
    for n in (name,) + aliases:
        v = os.environ.get(n)
        if v:
            return v
    return default


def c_to_f(c):
    """Celsius → Fahrenheit, rounded to 0.1."""
    return None if c is None else round(c * 9.0 / 5.0 + 32.0, 1)


def fmt_temp(c):
    """'34 °C / 93 °F' — the project's standard temperature string."""
    if c is None:
        return "--"
    return f"{c:.0f} °C / {c_to_f(c):.0f} °F" if float(c).is_integer() else f"{c:.1f} °C / {c_to_f(c):.1f} °F"

