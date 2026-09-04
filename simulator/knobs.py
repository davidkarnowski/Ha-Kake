# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Knob registry — every condition the simulator can be put into.

A knob is a named, typed, bounded, documented input. The registry exists so an
agent (or a human) can discover the whole control surface with
`sim.knob_schema()` instead of reading source.

Validation policy (chosen deliberately, and documented in docs/SIMULATOR.md):

  * unknown name        -> ValueError naming it, with near matches from
                           difflib.get_close_matches
  * wrong type          -> ValueError
  * bad text choice     -> ValueError listing the valid choices
  * number out of range -> **clamped** to [min, max], and the clamp is recorded
                           in `Simulator.warnings`.  Clamping (rather than
                           rejecting) is what a rig wants: a scenario that says
                           `soc: 105` should run at 100 %, not abort mid-drive.
                           The clamped value is what `set()` returns, so the
                           caller always sees what actually happened.
"""

import difflib

_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}

# where a help sentence stops being a title and starts being an explanation
_LABEL_BREAKS = (";", " — ", " (", ", ", ". ", ": ")
LABEL_MAX = 40


def derive_label(help, name=""):
    """The first clause of a help string, as a control title.

    Cut at the earliest of the separators in _LABEL_BREAKS, then trimmed to
    LABEL_MAX characters on a word boundary. Falls back to the knob name with
    underscores and the `fault.` prefix removed, so it is never empty.
    """
    text = (help or "").strip()
    cut = len(text)
    for sep in _LABEL_BREAKS:
        i = text.find(sep)
        if 0 < i < cut:
            cut = i
    text = text[:cut].strip().rstrip(".:;,")
    if len(text) > LABEL_MAX:
        text = text[:LABEL_MAX].rsplit(" ", 1)[0].rstrip(".:;,")
    if not text:
        text = name.split(".")[-1].replace("_", " ").strip().capitalize()
    return text


class Knob:
    """One declared input. `type` is one of float / int / bool / text."""

    __slots__ = ("name", "type", "unit", "min", "max", "default", "help",
                 "choices", "category", "label")

    def __init__(self, name, type, default, help, unit="", min=None, max=None,
                 choices=None, category=None, label=None):
        self.name = name
        self.type = type
        self.unit = unit
        self.min = min
        self.max = max
        self.default = default
        self.help = help
        self.choices = tuple(choices) if choices else None
        # Every knob belongs to exactly one category. It is part of the schema
        # so a UI can group the control surface without a hardcoded knob list
        # (see the control panel in simulator/panel.html): the page reads the
        # categories out of the schema, whatever vehicle it is talking to.
        self.category = category or ("faults" if name.startswith("fault.") else "other")
        # A human title for a control ("Low beam", not "headlights"). Every
        # shipped knob declares one; a knob that does not gets the first clause
        # of its help text, so a UI never has to fall back to the raw name.
        self.label = label or derive_label(help, name)

    def schema(self):
        d = {"name": self.name, "type": self.type, "unit": self.unit,
             "min": self.min, "max": self.max, "default": self.default,
             "help": self.help, "category": self.category, "label": self.label}
        if self.choices:
            d["choices"] = list(self.choices)
        return d

    # ── coercion / validation ────────────────────────────────────────────

    def coerce(self, value):
        """Return (value, warning_or_None). Raises ValueError on bad input."""
        if self.type == "bool":
            return self._bool(value), None
        if self.type == "text":
            return self._text(value), None
        return self._number(value)

    def _bool(self, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in _TRUE:
                return True
            if s in _FALSE:
                return False
        raise ValueError(f"knob {self.name!r} is a bool; {v!r} is not "
                         f"(use true/false, on/off, 1/0)")

    def _text(self, v):
        if not isinstance(v, str):
            raise ValueError(f"knob {self.name!r} is text; got {type(v).__name__}")
        s = v.strip()
        if self.choices:
            for c in self.choices:
                if c.lower() == s.lower():
                    return c
            near = difflib.get_close_matches(s, self.choices, n=3, cutoff=0.4)
            hint = f"; did you mean {', '.join(near)}?" if near else ""
            raise ValueError(f"knob {self.name!r} must be one of "
                             f"{', '.join(self.choices)} — got {v!r}{hint}")
        return s

    def _number(self, v):
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, str):
            try:
                v = float(v.strip())
            except ValueError:
                raise ValueError(f"knob {self.name!r} is a number; got {v!r}") from None
        if not isinstance(v, (int, float)):
            raise ValueError(f"knob {self.name!r} is a number; got {type(v).__name__}")
        warn = None
        if self.min is not None and v < self.min:
            warn = f"{self.name}={v} clamped to minimum {self.min}"
            v = self.min
        if self.max is not None and v > self.max:
            warn = f"{self.name}={v} clamped to maximum {self.max}"
            v = self.max
        return (int(round(v)) if self.type == "int" else float(v)), warn


class KnobSet(dict):
    """An ordered {name: Knob} registry for one vehicle."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._category = None

    def group(self, category):
        """Every knob added after this call lands in `category`, until the
        next call. Purely declarative sugar so build_knobs() stays a list."""
        self._category = category
        return self

    def add(self, *a, **kw):
        kw.setdefault("category", self._category)
        k = Knob(*a, **kw)
        self[k.name] = k
        return k

    def categories(self):
        """Category names in the order their first knob was declared."""
        out = []
        for k in self.values():
            if k.category not in out:
                out.append(k.category)
        return out

    def defaults(self):
        return {n: k.default for n, k in self.items()}

    def schema(self):
        return {n: k.schema() for n, k in self.items()}

    def resolve(self, name):
        """The Knob called `name`, or ValueError with near matches."""
        if name in self:
            return self[name]
        near = difflib.get_close_matches(name, list(self), n=5, cutoff=0.4)
        if not near:
            # a bare 'cell_degraded' should still point at 'fault.cell_degraded'
            near = [n for n in self if n.rsplit(".", 1)[-1] == name.rsplit(".", 1)[-1]]
        if not near:
            near = difflib.get_close_matches(name, list(self), n=5, cutoff=0.2)
        hint = (f" — did you mean: {', '.join(near)}?" if near
                else f" — no knob resembles it; {len(self)} knobs available, "
                     f"call knob_schema() for the list")
        raise ValueError(f"unknown knob {name!r}{hint}")


def faults_of(kset):
    return tuple(n for n in kset if n.startswith("fault."))
