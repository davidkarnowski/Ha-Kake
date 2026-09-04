# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit helpers for the simulator — stdlib only, no imports from the repo root.

`simulator/model.py` is pure Python by rule (no I/O, nothing imported from
outside the package), so it cannot reach `util.c_to_f`. These are the same
formulae, kept byte-for-byte compatible with `util.py` so a value the model
emits as `x_f` equals what `leaf_decoders` would compute from the same `x_c`.

The house rule (docs/SIGNALS.md) is that every temperature is shown in °F with
°C alongside, but nothing sim-side prints a temperature — the model emits the
numbers as `x_c`/`x_f` pairs and each page formats its own — so this module is
conversions only. (`util.fmt_temp`, the CLI's "34 °C / 93 °F", is a separate
thing and is deliberately left alone.)
"""


def c_to_f(c):
    """Celsius -> Fahrenheit, rounded to 0.1. Identical to util.c_to_f."""
    return None if c is None else round(c * 9.0 / 5.0 + 32.0, 1)


def f_to_c(f):
    """Fahrenheit -> Celsius, rounded to 0.1."""
    return None if f is None else round((f - 32.0) * 5.0 / 9.0, 1)
