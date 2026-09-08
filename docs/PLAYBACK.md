<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Playback — the dashboard replaying what it recorded

> Playback shows *what the dashboard recorded*, to study the car. Replay
> (`docs/REPLAY.md`) re-runs *what the car said* through the reader, to
> exercise the stack. Different questions, deliberately different tools.

## Using it

Press **Playback** in the header (or open `/?playback=1`). The page stops
polling the car, a PLAYBACK badge appears, and a timeline card opens above the
tiles:

- **Session** — a recorded run, newest first, labelled with its date, length,
  SOC start → end, row count, whether cell voltages were read, and the
  adapter. Sessions are gaps in the data: ten minutes of silence starts a new
  one (`/api/sessions?gap=`).
- **The strip** — SOC as a line, pack current as an area (discharge drawn
  downward), a tick at the bottom wherever cells were read, the playhead.
  Click to seek. **Drag to zoom** into a stretch — the frames are re-fetched
  at full resolution for just that range. *Whole session* zooms back out.
- **Transport** — ⟨ frame / ▶ Play / frame ⟩, −1 min / −10 s / +10 s / +1 min,
  and speeds ½× 1× 4× 10× 60× (real seconds × speed; a 5 s row every 5 s at
  1×). The clock shows the frame's wall time and the offset from the window's
  start. Keys: space plays and pauses, ← → step a frame, shift + ← → jump
  ten seconds.
- **Every tile follows** — the gauges, the history graphs (up to the
  playhead), the cell grid and the 3D pack, user tiles, the body and climate
  cards. The status dot says *Recorded <time>*; nothing is "stale".
- **Alerts** stay silent unless the timeline's *alerts* box is ticked, and
  the engine forgets its hysteresis whenever you switch mode or tick the box,
  so a rule can be reviewed against an old drive without it firing on the
  way in.
- **Links** — the URL updates to `?playback=1&from=<epoch>&to=<epoch>` as you
  brush, so a moment can be pasted into the worklog. With `?shot` the page
  renders that first frame once and holds it.
- **Back to live** resumes polling.

Under `--demo` there is one canned session; under `--adapter replay` or
`--adapter sim` playback reads that run's own throwaway database, never
`web/leaf_battery.db`.

## What it is built from

The page keeps one paint path. Live mode's `poll()` fans `/api/status` and
`/api/history` out to five sinks — `updateTrend`, `updateDash`,
`updateSparkline`, `TileStudio.update`, `TileStudio.history`. Playback's
`renderFrame(k)` feeds the same five from `records[k]` and `hist[0..k]`, so
no tile has a playback branch. The clock is `web/static/playback.js` — a pure
transport (seek, play, pause, speed, step, jump, tick) with node tests —
driven from one `requestAnimationFrame` loop. It lives in the browser on
purpose: the reader may be live on the car while two browsers scrub two
different afternoons; what it borrows from the simulator is the vocabulary
(`N×`, fixed jumps), not its Python clock.

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
on the default schedule. For a drive log arm the **cell log** from the ⋯ menu
of the cell grid or the 3D pack (`docs/PACK3D.md`): the cell read runs every
cycle and every fresh read gets its own row, so the strip's cell ticks come
every cycle and stepping through an acceleration shows each pair sag.

## Demo mode

`docs/demo/sessions.json` and `docs/demo/frames.json` carry one canned session
(the demo state at the store period over the last two minutes of the demo
history — the same frame with a moving clock, no invented values) so `--demo`
has something to scrub.
