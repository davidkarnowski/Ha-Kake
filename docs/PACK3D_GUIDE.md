<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Building a 3D pack model for another vehicle

> The 3D pack tile draws *any* battery a profile can describe as a table:
> modules of one size, placed in stacks, each stack saying how its modules map
> to the values the car reports. This is the contract, the method that
> produced the Leaf's table in an afternoon with an AI agent, and a worked
> sketch for a Prius NiMH pack. `docs/PACK3D.md` describes the tile itself.

## 1. What a profile declares

Five optional attributes in `vehicles/<profile>.py`. The tile appears only if
`PACK_LAYOUT` exists and the profile lists a `pack3d` tile in `TILES`.
`vehicles/__init__.py`'s `validate_profile()` checks `PACK_LAYOUT` and
`PACK_MODES` on every load (`PACK_MODULE`, `PACK_CASE` and `PACK_SENSORS` are
not validated)
(`python vehicles/__init__.py <name>` runs it standalone).

| Attribute | What it is |
|---|---|
| `PACK_MODULE` | `{"L", "W", "T"}` — one module's long side, short side and thickness, in mm. Every module is drawn the same size. |
| `PACK_CASE` | `{"L", "W", "H", "hump": {"L", "W", "H"}}` — the envelope in mm (a tray, plus an optional taller section at the rear). Drawn translucent; purely visual. |
| `PACK_LAYOUT` | a list of **stacks** (below). This is the model. |
| `PACK_SENSORS` | `[{"n", "x", "y", "z", "where"}]` — discrete temperature sensors as balls, coloured on their own range and labelled in °F and °C from the record's `temps_f` (index-matched: sensor 0 reads `temps_f[0]`). |
| `PACK_MODES` | a list of **value modes** (below): which list in the record colours the bodies, in what unit, with which scales. Absent, the Leaf default applies: `cells` in mV. |

### A stack

```python
{"name": "Rear block (under rear seat)",   # what the readout and pane call it
 "kind": "edge",     # "edge": modules on edge in one row across the car (stack axis z)
                     # "flat": modules lying flat, stacked upward (stack axis y)
 "x": -603, "z": 0,  # the stack's centre, mm, car coordinates (x forward, z → passenger side)
 "n": 24,            # modules in the stack
 "first": 0,         # the first value index this stack carries (0-based)
 "split": 2,         # values per module, sliced through its thickness (default 2)
 "group": 1,         # or: modules per value (Prius: 2); group > 1 forces split 1
 "verify": ""}       # what is still assumed about this stack ("" = verified); shown in the readout
```

The axes: **x** forward (toward the nose), **y** up, **z** toward the
passenger side; the driver side is −z. In an `edge` stack module 0 sits at
the +z end; in a `flat` stack module 0 is at the bottom. Inside a module the
first slice is on the +axis side. `first` counts *values*, not modules: a
stack of `n` modules with `split` s carries values `first … first + n·s − 1`;
with `group` g it carries `first … first + n/g − 1`. Every value index from 0
to N−1 must be covered exactly once across the stacks — the validator checks.

### A mode

```python
{"id": "volt", "key": "cells", "name": "cell pair", "unit": "mV",
 "scales": ["abs", "fixed", "dev", "drop"],   # which colour scales the ⋯ menu offers
 "dev": 50, "drop": 300,             # ± span of the deviation scale; the drop scale's full-red distance
 "fixed": [3000, 4200],              # the fixed scale's bounds — required if "fixed" is offered
 "invert": False}                    # True for temperatures: hot should read red
```

**Anything you leave out is inherited, not empty.** The tile merges each mode
over `PackLayout.DEFAULT_MODE`, whose numbers are the Leaf's cell pairs in mV:
`scales` `abs / fixed / dev / drop`, `dev` 50, `drop` 300, `fixed`
`[3000, 4200]`. A mode in another unit that omits them gets millivolt spans on
a degree scale, so state all four explicitly unless the unit really is mV.

`key` names a list in the record (what `/api/status` carries) with at least
N entries, one per value index. With several modes the ⋯ menu gains a
*Show* selector; the readout, pane, flashes and thresholds all follow the
chosen mode's unit. The scales:

| Scale | Colour | For |
|---|---|---|
| `abs` | the frame's lowest value → highest, through the grid tile's own `cellColor`, so a voltage pair is the same colour in both panels | any mode |
| `fixed` | a range that never moves: `fixed: [lo, hi]`, or the tile's own two numbers; outside values clamp | a colour that means the same value in every frame, for playback and drive logs |
| `dev` | deviation from the frame's mean, ±`dev` | finding the value that departs from its siblings under load |
| `drop` | drop from the value's own first reading this session, `drop` → full red | voltages: an internal-resistance proxy during a drive |

### Bodies and values

The tile draws **bodies** (one per module slice) and colours them by
**values** (`body.v` indexes the mode's list). For the Leaf the two coincide:
48 modules × 2 slices = 96 bodies = 96 cell pairs. For a grouped pack several
bodies share one value and are coloured alike; the label appears once per
value, and the pane names the modules a value spans. The pure maths is
`web/static/pack_layout.js` (node-tested); the drawing is
`web/static/pack3d.js`.

## 2. The method — how the Leaf's table was made, agentically

This took one afternoon with an AI agent and no CAD file. The steps are
reusable; the point is to record confidence as you go, because the first
table *will* be partly wrong.

1. **Collect the published shape.** Module dimensions and mass, module count,
   the number of measured values and how they group (2s2p → two values per
   module; a NiMH pack with one tap per two modules → `group: 2`), the pack
   envelope, where the sections sit in the car. Sources that worked:
   manufacturer specs, Wikipedia, battery-repurposing shops (they publish
   module dimensions), teardown write-ups and videos ("2-high packs of 4 and
   4-high packs of 8 per side" was one sentence in a blog post).
2. **Find the numbering.** What you need is the ECU's value order mapped to
   physical position. The **service manual** is the authority — the Leaf's
   is one paragraph on page EVB-20 (module MD1 at the far passenger end of
   the rear stack, MD25–28 under the rear driver footwell …). Owner forums
   often quote it; a mature diagnostic app's help file lists what it exposes.
   Treat AI-generated summaries as leads, not sources: one reversed the two
   sides of the Leaf pack.
3. **Write the table with `verify` notes.** Put every assumption in the
   stack's `verify` string — the direction inside a stack, which of two
   stacks is rearmost, which slice carries the odd value. The tile shows the
   note in its readout, so nobody mistakes a guess for a fact, and fixing it
   later is a data edit.
4. **Spike first.** Before touching the dashboard, build a standalone page:
   the table, synthetic values with one deliberately weak cell, three.js from
   a CDN. Look at it, rotate it, fix the proportions. The Leaf's spike is
   `research/pack3d_spike/index.html`; it found the label-layer id clash that
   would otherwise have shipped.
5. **Wire the tile.** Add `PACK_*` to the profile, `pack3d` to `TILES` with
   the items that produce the value list *and* the temperatures (the Leaf's
   `lbc02`, `lbc06`, `lbc04`), a default height in `DEFAULT_H`. Run
   `python vehicles/__init__.py <name>`. Add a node test that the layout
   yields the right body and value counts (copy `tests/test_pack3d.py`).
6. **Verify on the car.** With the car parked and the dashboard live: a
   known-weak value should light where the table says it is. If the car has
   one module warmer than the rest after a charge, the sensor balls say
   whether your sensor positions are right. Then edit the table and drop the
   `verify` notes as facts arrive. The record of what was verified and how
   goes in `docs/SIGNALS.md`.

Pitfalls met on the way: a top-level `const` in the page is **not** a
`window` property, and the module reads `window.PACK`; a label layer must be
a class, not an id another control shares; a rounded box cannot be scaled per
instance, so bodies of different sizes are separate instanced meshes (the
tile handles that); three.js physical lights need intensity π for a top face
to show its plain colour.

## 3. Mapping modes for different packs

- **One voltage per slice of a module** (Leaf 2s2p, most EV pouch packs):
  `split: 2` (default), mode `key` = the voltage list.
- **One voltage per group of modules** (Prius / Camry NiMH: 28 prismatic
  modules, 14 measured blocks): `group: 2` on the stack, mode key = the
  block-voltage list; each body of a group takes its shared value.
- **Temperatures per module** (packs whose BMS reports a temperature for
  every module or block): a second mode `{"id": "temp", "key":
  "module_temps_f", "name": "module", "unit": "°F", "scales": ["abs", "dev"],
  "dev": 5, "invert": true}` (`name` is required, and a non-mV mode should
  state its own `dev` / `drop` / `fixed` — see the note under the mode above);
  the ⋯ menu's *Show* switches the bodies from volts to degrees, hottest red.
- **A few discrete sensors** (the Leaf's four): `PACK_SENSORS`, coloured on
  their own range and selectable, independent of the bodies' mode.
- **Both** at once is the common case: voltages on the bodies, sensors as
  balls, a temperature mode if the ECU gives one per module.

## 4. A worked sketch — 2nd-generation Prius NiMH (2004–2009)

Numbers here are from public descriptions and repurposing shops, **not
verified on a car**; they are a starting table, with `verify` notes doing
their job.

```python
PACK_MODULE = {"L": 285, "W": 106, "T": 20}       # prismatic 7.2 V module, approx.
PACK_CASE = {"L": 700, "W": 380, "H": 160}       # behind the rear seat back, approx.
PACK_LAYOUT = [
    {"name": "Single row behind the rear seat", "kind": "edge", "x": 0, "z": 0, "n": 28, "first": 0, "group": 2,
     "verify": "block 1 at which end, and the case dimensions, are assumed"},
]
PACK_SENSORS = [   # three thermistors along the row, approx. positions
    {"n": "T1", "x": 0, "y": 80, "z": -200, "where": "driver end of the row"},
    {"n": "T2", "x": 0, "y": 80, "z": 0,    "where": "middle of the row"},
    {"n": "T3", "x": 0, "y": 80, "z": 200,  "where": "passenger end of the row"},
]
PACK_MODES = [
    {"id": "volt", "key": "blocks", "name": "block", "unit": "V", "scales": ["abs", "dev"], "dev": 0.5},
]
```

The decoder would put the 14 block voltages in `record["blocks"]` and the
thermistors in `temps_f`; the validator confirms 28 modules ÷ 2 cover values
0–13; the tile draws 28 bodies, pairs of them the same colour, and a click on
any body opens its block with "modules 3–4" in the pane.

## 5. Checklist

- [ ] `PACK_MODULE`, `PACK_CASE`, `PACK_LAYOUT` (with `verify` notes), `PACK_SENSORS`, `PACK_MODES` in the profile
- [ ] `pack3d` in `TILES` with the items that produce the value list and the temperatures; `DEFAULT_SPAN` and `DEFAULT_H`
- [ ] `python vehicles/__init__.py <name>` → OK
- [ ] a node test of the layout's body and value counts (pattern: `tests/test_pack3d.py`)
- [ ] `docs/SIGNALS.md`: the value order and what verified it
- [ ] the spike, then the owner's browser check with a known-weak value
