<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Playback — the dashboard replaying what it recorded

> Playback shows *what the dashboard recorded*, to study the car. Replay
> (`docs/REPLAY.md`) re-runs *what the car said* through the reader, to
> exercise the stack. Different questions, deliberately different tools.

_The page side (mode switch, timeline, transport) lands with the next phase;
this page documents the data contract it builds on._

## The endpoints

**`GET /api/sessions?gap=600`** — recorded sessions, newest first. A session is
a run of readings with no silence longer than `gap` seconds (default 10 min,
clamped 60 s … 24 h). Each entry: `started` / `ended` (ISO) and
`start_epoch` / `end_epoch`, `duration_s`, `n` rows, `soc_start` / `soc_end`,
`adapter`, and `cells` — whether any row in it carries cell voltages. Sessions
are derived from the data itself rather than read from the `sessions` table,
which has no epoch column, no link to readings, and an open-ended row for
every session that ended in a crash.

**`GET /api/playback/frames?from=&to=&max=3600&cells=0`** — stored readings in
an epoch-seconds range as frames:

```json
{"t":        [1756144656.6, ...],     // one per frame, ascending
 "records":  [{...}, ...],            // the /api/status shape, rebuilt per row
 "hist":     [{...}, ...],            // the /api/history shape, same rows
 "cells_at": [0, 4, 8, ...]}          // frame indices whose record carries cells
```

`records[k]` is what `/api/status` would have returned at that moment, so the
page can hand it to the same paint functions: the `extra` bag, every column,
the temperature lists and °F twins rebuilt from the °C columns, `cells` when
asked for. Each record is stamped `playback: true`, `status: "ok"`, and
`timestamp` / `last_ok` at the row's time.

- `max` (1 … 3600, default 3600) thins a long range to the **last real row of
  each time bucket** — never an average, because a frame is a state (gear,
  doors, cells), not a line.
- `cells=1` joins the cell voltages, which multiply the payload by about six;
  ask only when the cell grid or the 3D pack is on screen.
- `from` and `to` are epoch seconds (the store's own clock); `400` when either
  is missing or reversed.

**What a frame cannot carry:** the adapter's port and name, the reader's
`readings` counter and the raw `balancing` list are not stored, so those show
blank. Everything a tile paints is there — a test inserts a real capture and
asserts the frame comes back with every key the tiles read.

**Resolution:** rows land every 5 s (`STORE_PERIOD`), cell voltages every 20 s
on the default schedule. A drive log needs the cell-log capture mode (see
`docs/PACK3D.md`) to make an acceleration event visible.

## Demo mode

`docs/demo/sessions.json` and `docs/demo/frames.json` carry one canned session
(the demo state at the store period over the last two minutes of the demo
history — the same frame with a moving clock, no invented values) so `--demo`
has something to scrub.
