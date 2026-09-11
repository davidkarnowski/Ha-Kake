<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: CC-BY-SA-4.0
-->

# The raw output console

A debug pane: a terminal showing what the transport is actually saying, under
the dashboard's own tiles. Tile id `console`, name **Raw output**, full width,
**off by default** on every vehicle.

It exists because a decoded value is only trustworthy when you can see the bytes
that produced it. The Lancer's stored trouble codes appear in this pane the
moment mode 03 answers, with the raw frames that produced them directly above —
which is the pairing that makes a readout evidence rather than magic.

## What it is not

**Not a capture tool.** It drops frames on purpose and it tells you how many.
Nothing in it is evidence of what was on the bus.

- To record a drive properly: `record_session.py` (and `docs/REPLAY.md`).
- To log frames losslessly: the MQTT bridge (`bridge/`, `docs/MQTT.md`).

The file it writes (`web/console.jsonl`) is gitignored, size-capped, cleared
every time the tile is armed, and holds raw frames from your own car. It is a
window that happens to be persisted so two processes can share it, not a log.

## What each transport can honestly show

| Source | What appears | Where it comes from |
|---|---|---|
| Broadcast frames | `421 08 00 00` — byte for byte, the shape the decoders and fixtures use | every frame, on native CAN (`--adapter can`), MQTT (`--adapter mqtt`) and the simulated bus (`--sim-can`); on an **ELM327, only the ids the profile polls and only during each `ATMA` dwell** |
| UDS answers | `2101 -> 7BB 10 29 61 01 / 7BB 21 …` — grouped with the request that asked | every transport |
| Adapter replies | `ATI -> ELM327 v1.5`, `NO DATA`, `?`, a refusal (`refused 2101: the ev bus is listen-only`) | ELM327 paths, and the façade's own equivalents |
| Decoded text values | any `text`-kind signal the profile carries, as it changes — the Lancer's stored and pending codes today | any profile with a text signal |
| Reader events | connect, reconnect, bus down, link dropped, asleep, awake again, the console arming itself | every transport |

The ELM327 row is a real limitation and the pane says so in those words, in its
own footer, whenever the primary transport is an ELM327.

## Why it decimates, and where

Car-CAN carries roughly 1,700 frames a second. Nothing useful reaches a browser
at that rate and nothing useful is read by a human, so the thinning happens in
the **reader**, on the way into the ring — not in the browser, which would have
had to receive it all first.

Three rules, all of them visible on screen:

1. **Only the ids the enabled tiles poll** — a passive item's own id, a UDS
   item's response header. Usually 10–15 ids rather than the whole bus.
2. **A per-id rate cap** — at most *N* frames of each id per second (default 10,
   `opts.rate` in the tile's ⋯ menu, 1–200).
3. **Everything mode** (`opts.everything`) shows every id on the bus **and says
   it is lossy**, because the cap still applies.

Every dropped frame is counted, per id, and the counts are published in the
record and shown in the pane's stats line. A pane that silently thinned its own
data would be worse than no pane.

The **kind filter** is the other half of the answer: frames are the only
firehose, and they are one checkbox.

Only `frame` entries are ever decimated. A UDS answer, an adapter reply, a text
value and a reader event are answers and events, not a stream — dropping one
would lose exactly the thing the person was watching for.

## How it is wired

```
 transports ──tap──▶ Ring (bounded, in the reader) ──flush──▶ web/console.jsonl
                                                                    │
                                             GET /api/console ◀──────┘
                                                     │
                                             web/static/console.js
```

- **Arming.** The reader reads `web/tiles.json` (the same mtime path the cell
  log uses) and builds a `console.Console` only when the tile is enabled. No
  tile, no object, no tap, no file — and turning the tile off drops all three.
- **The taps.** Native CAN, MQTT and the simulated bus all reach the reader
  through `cantransport.CanFacade`, so one additive `tap` attribute there covers
  all three. `mqttsource.MqttSource.event_tap` carries the one thing the façade
  never sees: a bridge message this source had to throw away. An ELM327 has no
  façade, so its frames come from the `ATMA` lines `poll_bus` already holds.
  A tap that raises is dropped, never propagated: a debug pane must not be able
  to take a bus down.
- **Frames captured for a UDS request are not tapped as frames.** The reader
  emits one grouped `uds` entry per request instead; printing the same `7BB`
  bytes twice is noise.
- **The ring** is fixed-capacity (a few thousand entries), thread-safe (frames
  arrive on a python-can notifier thread) and hands out a monotonically
  increasing sequence number. That number is the cursor.
- **The file** is the state file's pattern — one writer process, one reader
  process, a file between them — so this needed no new IPC.

## An entry

```json
{"seq": 41, "t": 120909.33, "wall": 1789141190.504,
 "bus": "car", "kind": "frame", "id": "421", "text": "421 08 00 00"}
```

`t` is monotonic (ordering and ages), `wall` is epoch seconds (what the pane
shows). `kind` is one of `frame`, `uds`, `adapter`, `text`, `event`. `id` is the
CAN id where one is meaningful and `""` where it is not.

## The endpoint

    GET /api/console?since=<cursor>&kind=frame,uds&ids=421,5B3&limit=200

- `since` is an **opaque cursor**, not a timestamp — two frames can share a
  millisecond, so a timestamp cannot address them.
- `kind` and `ids` are comma-separated; an unknown kind is ignored, not obeyed.
- `limit` is capped and returns the **newest** matches. A pane that fell behind
  wants the end of the stream, not the start of a backlog — which also means you
  cannot page *forward* through a backlog with it. That is deliberate.
- The answer is `{"entries": [...], "cursor": "...", "stats": {...},
  "kinds": [...]}`. An empty answer hands back the cursor you sent, so an idle
  tile does not lose its place.
- `stats` comes from the reader's own record: `kept`, `dropped`,
  `dropped_by_id`, `capacity`, `rate_cap`, `everything`, the id list, and
  `partial` when the transport can only show part of the bus. When the tile has
  never been armed the answer is an empty window with `{"on": false}` — not a
  404, because a tile being off is not an error.
- **Read-only**, like everything else here: `POST` gets a 405.

## The pane

- Monospace, newest at the bottom, auto-scrolling only while it is already at
  the bottom.
- **Pause** — not a nicety. A scrolling pane at any real frame rate is
  unreadable, and pausing is how a person actually reads a line. Pausing stops
  the polling; the reader's window keeps filling regardless.
- **Kind filter** (frames / UDS / adapter / values / events). This one goes to
  the server, because it is the one filter that changes how much travels;
  toggling it re-reads the window from the start of the file.
- **Id filter** and **known ids only** are applied in the browser: typing an id
  should not refetch, and the lines on screen are the ones being looked through.
  The "known" list is the profile's own — every item's `can_id` from
  `/api/signals`. Nothing in the browser knows what a Leaf is.
- **A byte that changed** from that id's previous frame is highlighted. That is
  how a human finds a signal. A first sighting highlights nothing: "everything
  changed" says nothing.
- **Click a line to copy it** as `ID B0 B1 …` — straight into a fixture.

## Turning it on

Open the ⋯ / Tiles panel, enable **Raw output**. The reader picks the change up
on its next cycle and says so:

```
  [reader] raw output console armed: 12 polled id(s), 10/s per id -> web/console.jsonl
```

The tile's ⋯ menu carries the two options that change what the reader keeps
(every id, and the rate cap). Both are written into `web/tiles.json`, the reader
re-arms on them, and the drop counts on screen then belong to the rules on
screen.

## The read-only rule

`web/console.py` has no transport handle in it at all — only text that has
already arrived. There is no path from the pane to the bus, and a test asserts
that the module imports no transport and defines no `send`. `SECURITY.md` is
unaffected by this feature: it adds no service byte and sends nothing.

## Where the code is

| Piece | File |
|---|---|
| The ring, the decimation, the file | `web/console.py` |
| Arming, the taps, the entries | `web/reader.py` (`refresh_console`, `attach_console`, `console_item`, `console_text`, `console_event`) |
| The frame tap | `cantransport.py` (`CanFacade.tap`), `mqttsource.py` (`MqttSource.event_tap`) |
| The endpoint | `web/app.py` (`/api/console`) |
| The tile | `web/templates/tiles/console.html`, `web/static/console.js`, styles in `web/static/tiles.css` |
| The framework tile it rides on | `vehicles/__init__.py` (`FRAMEWORK_TILES`) |
| Tests | `tests/test_console.py` |
