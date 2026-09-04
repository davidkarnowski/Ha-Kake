<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Running without a car

> `python web/app.py --adapter replay`

The dashboard normally needs a Leaf, an ELM327 and a parking space. **Replay
mode needs none of them.** It runs the entire real stack — the reader, the
tile-driven scheduler, `elm327.py`'s command surface, the vehicle profile's
own `configure()` and `decode()`, the SQLite store, the API and the page —
against a *recorded session*, and puts real decoded values on the screen.

That is what makes "write a profile for your car" an honest invitation. You
can develop and review a profile with nothing but the repository, and so can
someone who has never seen the car it is for.

## Quick start

```bash
python web/app.py --adapter replay                          # the Leaf
python web/app.py --adapter replay --vehicle lancer_2009    # the Lancer
python web/app.py --adapter replay --fixture my_drive.json  # your own capture
python web/app.py --adapter replay --speed 10               # 10x the clock
```

Then open <http://127.0.0.1:5000>. It behaves exactly like the live dashboard,
because it *is* the live dashboard; only the transport is different.

The reader alone works the same way:

```bash
python web/reader.py --adapter replay --vehicle lancer_2009
```

### Replay never touches your data

| | live | replay |
|---|---|---|
| database | `web/leaf_battery.db` | `web/replay_<profile>.db` (gitignored, disposable) |
| state file | `web/battery_state.json` | `web/replay_<profile>_state.json` |

Delete either replay file whenever you like. Nothing in replay mode writes to
the real store, so a playback session can never be mistaken for — or mixed
into — years of real readings.

### Replay always says it is replay

- the startup log leads with `REPLAY MODE — recorded fixture, not a car` and
  names the fixture and the throwaway database
- `adapter_type` is `"replay"` and `adapter_name` carries the fixture name
- every record, and so `/api/status`, carries `replay: true`, plus
  `replay_fixture` and `replay_synthetic`

A UI can surface that flag; nothing has to guess from an adapter name.

## What replay will not do

**It never invents a value.** If the fixture has no answer for a command, the
adapter answers `NO DATA` — exactly what a car that stayed quiet would say —
and the tile shows nothing. The shipped Leaf session, for instance, has no
HVAC group 00, because the capture it came from has none.

It is also not a simulator. It replays what a car actually said, in the order
it said it. It cannot answer a command nobody ever asked the car, and it will
not respond to your driving.

## The session-fixture format

```json
{
  "hakake_replay": 1,
  "vehicle": "leaf_ze0",
  "title": "2012 Nissan Leaf (ZE0)",
  "adapter": "ELM327 v1.5",
  "synthetic": false,
  "captured": "2026-09-02T20:38:11Z",
  "source": ["tests/fixtures/lbc_raw_20260824.json — LBC groups 01-06, …"],
  "notes": "free text — how it was captured, what the car was doing",
  "frames": [
    {
      "t": 0.0,
      "uds":     {"79B": {"2101": ["7BB 10 29 61 01 FF FF F1 18", "…"]}},
      "passive": {"421": ["421 08 00 00"]}
    },
    {"t": 1.0, "passive": {"284": ["284 00 00 00 00 00 00 9B 21"]}}
  ]
}
```

- **`uds`** is keyed by the *request* header — what `ATSH` selects, i.e. which
  ECU you are talking to — then by the command as sent (`2101`, `010C`, `03`).
  The values are raw ELM327 lines, verbatim. They are filtered against the
  live `ATCRA` filter on the way out, the way real hardware does, so a
  capture that caught both `7E8` and `7E9` still behaves correctly when the
  reader targets one of them.
- **`passive`** is keyed by CAN ID and holds raw monitor lines (`ATCAF0` +
  `ATCRA` + `ATMA`), the shape `passive_capture()` returns.
- **`frames` are cumulative.** The view at time *t* is frames 0..i merged, so a
  frame only carries what changed. `t` is seconds from the start of the
  session; the session loops by default, and `--speed` scales the clock.
- **`synthetic`** must be `true` for anything not recorded from a real
  vehicle. A synthetic fixture is announced in the startup log and flagged in
  `/api/status`. Both fixtures shipped here are `false` — every line in them
  came off a car.

### Why a new format instead of reusing the raw captures

The existing fixtures in `tests/fixtures/` are each shaped for the tool that
made them: `lbc_raw_*.json` is one ECU's groups, `probe_*.json` is a one-shot
sweep of three different conversations, `walk_*.json` holds decoded *bytes*
(not transport lines) for one control being stepped. None of them can answer
"what did header X say to command Y at second N", which is the only question a
transport is ever asked. The session format is that question's shape — and it
is built *from* those captures rather than instead of them.

## Making a fixture

Both shipped fixtures are derived from raw captures already in the repository,
and the deriver is reproducible:

```bash
python record_session.py --derive                    # both profiles
python record_session.py --derive --vehicle leaf_ze0
```

To record your own car:

```bash
python record_session.py --seconds 120 --out my_drive.json
python record_session.py --vehicle lancer_2009 --seconds 60
```

The recorder polls exactly what the profile declares, once per `--period`
seconds, and writes every line verbatim. An item the car did not answer is
simply absent.

> **Before you share a fixture**, look at it, and run
> `python scripts/privacy_sweep.py`. Raw frames can carry the odometer, and on
> some cars the VIN.

## Replay vs. demo mode

`--demo` serves frozen JSON from `docs/demo/` — no reader, no transport, no
decoders. It exists so documentation screenshots are reproducible, and it is
deliberately inert. Replay is what you want for everything else: it is nearly
a superset, and unlike demo mode it actually exercises the code you are
changing.
