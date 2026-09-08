<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The 3D battery pack tile

> Tile **Battery pack — 3D** in the tiles menu. Leaf profile only.

The cell-pair grid tells you *which* pair is low. This tile tells you *where*
it is: the pack drawn as it sits under the car — the tall block under the rear
seat, the flat stacks on either side of the floor — with every one of the 96
measured cell pairs as its own body, coloured by voltage, in a viewport you
can orbit, zoom and pan. Hover a pair for its value; click to pin it.

It follows the same `lbc02` cell read as the grid (every 20 s on the default
schedule), so the two panels always show the same frame. Under `--adapter sim`
put `fault.cell_degraded` on and one pair turns red; under `--adapter replay`
it follows the recording; under `--demo` it shows the demo frame.

## Colour scales (⋯ menu → *Colour by*)

| Scale | What `t = 0` (red) … `t = 1` (blue) means | Use it for |
|---|---|---|
| **deviation from pack mean** (default) | −50 mV … +50 mV from the pack's mean at that instant | The weak-cell view. Under load every pair sags together; this shows who sags *more*. |
| absolute mV (grid scale) | the frame's lowest pair … its highest | Exactly the grid tile's colouring. |
| drop from own rest voltage | −300 mV … 0 mV below the pair's first value this session | A per-pair internal-resistance proxy during a drive log or playback. |

Other options: *Values* (label the lowest and highest pair, hover only, or
every pair), *Case* opacity, a view preset (iso, top, rear block, driver
side), auto-rotate. All of it persists in the tile's `opts` like any other
tile setting.

## How the model is built

There is no CAD file. `vehicles/leaf_ze0.py` carries the geometry as data —
`PACK_MODULE` (303 × 223 × 35 mm), `PACK_CASE` (the envelope: 1570 × 1188 mm
tray, 265 mm rear hump), `PACK_LAYOUT` (one entry per stack of modules) and
`PACK_SENSORS` (the four temperature sensors). `web/app.py` hands that to the
page, `web/static/pack_layout.js` turns it into 96 boxes (pure maths,
node-tested), and `web/static/pack3d.js` draws them with three.js: one
instanced mesh, one colour per instance, module outlines, terminal studs, a
translucent case, DOM labels tracked in 3D.

Each module is drawn as **two half-slabs split through its thickness**, one
per measured pair, because that is how the 2s2p module is built: the two
series pairs are stacked through it. The first pair of a module sits on the
upper (or, in the rear block, the passenger-side) half.

three.js ships as ES modules only, so `pack3d.js` is the page's one module
script, reached through an import map that points at the vendored copy in
`web/static/vendor/three/`. Nothing loads from a CDN; the car has no internet.

## Where each cell pair is — and how sure we are

Indices are the dashboard's 0-based cell-pair numbers. LeafSpy shows the same
pair as index + 1.

| Stack | Modules | Pairs | Confidence |
|---|---|---|---|
| Rear block, under the rear seat — 24 modules on edge in one row across the car, terminals up | 1–24 | 0–47 | section **published**; order within the block (passenger end → driver end) **assumed** |
| Driver side, stack 1 (rear footwell, 2-high) | 25–26 | 48–51 | **assumed** |
| Driver side, stack 2 (2-high) | 27–28 | 52–55 | **assumed** |
| Driver side, stack 3 (under the front seat, 4-high) | 29–32 | 56–63 | **assumed** |
| Driver side, stack 4 (4-high) | 33–36 | 64–71 | **assumed** |
| Passenger side, stack 4 (front, 4-high) | 37–40 | 72–79 | **assumed** |
| Passenger side, stack 3 (4-high) | 41–44 | 80–87 | **assumed** |
| Passenger side, stack 2 (2-high) | 45–46 | 88–91 | **assumed** |
| Passenger side, stack 1 (2-high) | 47–48 | 92–95 | **assumed** |

What the sources actually say (details and links in `docs/SIGNALS.md`, "Cell
order in the pack"): the three sections and their module counts are
published; a 2013 teardown gives the floor stacks as "2-high packs of 4 and
4-high packs of 8" per side; and one forum post with a diagram for the 2013+
24 kWh pack states the series order as rear block, then driver side, then
passenger side. Nobody has published the order *inside* each section, which
floor stacks are 2-high and which 4-high front-to-back, or confirmed any of
it on a 2011–2012 car.

Each `PACK_LAYOUT` entry carries a `verify` note saying what is assumed, the
tile shows *(position unverified)* in its readout for such pairs, and the
service manual's EVB "cell voltage loss inspection" figure (EVB-67) is the
one-figure lookup that would settle it. When it is settled, correct the table
in the profile — moving a stack is a data edit, not a code change — and drop
the `verify` note.

## Reading it under load

At rest a healthy pack is one colour. The interesting picture is during an
acceleration: a pair with higher internal resistance sags more than its
neighbours and shows up on the *deviation* scale while the current is
flowing, then recovers. Today's 20 s cell cadence rarely catches that; the
cell-log capture mode and playback (see `docs/PLAYBACK.md`) are what make
it visible.
