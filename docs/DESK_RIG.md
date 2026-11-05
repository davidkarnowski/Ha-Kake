<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: CC-BY-SA-4.0
-->

# The desk rig: a remote car, a broker and a slow link on one laptop

Some faults need a network to appear. A reader that reaches its car through a broker, a tunnel and a cellular hop
meets links that go quiet for ten seconds and then deliver everything at once, and nothing on a desk does that by
itself. The desk rig does: the whole MQTT chain runs on the loopback address, and the link in the middle can be
delayed, stalled, or made to hold one request back.

```
simulated ECUs ── virtual CAN ── bridge ──▶ mosquitto ◀── laggy link ◀── reader + dashboard
(simulator/canbus.py)   (bridge/hakake_bridge.py)  (loopback)  (tools/laggy_link.py)  (web/app.py --adapter mqtt)
```

Every part is the real one except the car: the simulator's ECUs answer on an in-process virtual bus, the bridge is
the bridge a Raspberry Pi runs, and the application is started exactly as for a remote car. **All data is simulated.**
The simulator is a fixture, not a verifier.

Needs `mosquitto` installed (`brew install mosquitto`, `apt install mosquitto`). Nothing else beyond
`requirements.txt`.

## Reproduce the stale-reply fault

```bash
./venv/bin/python tools/desk_rig.py --scenario stale-reply
```

The scenario holds one request back for longer than the reader waits for it. The reader gives up and asks for
something else; the bridge then runs both; the first reply lands inside the second request's window. The rig listens
to the reader's own `state` topic, one record per cycle, and checks the stored rows, for values no car can produce:

```
[12:34:10] holding the lbc05 request; the reader will give up on it
[12:34:29] cycles published by the reader: 252 (251 good, 1 bad); stored rows: 4 (0 bad)
[12:34:29]   on the page +12.6 s after the hold began: {'soc': 589.83, 'lv_volts': 63.999, 'pack_v': 6.46}
[12:34:29] FAULT REPRODUCED: a reply was decoded as the answer to a request it did not belong to
```

That is a group-05 reply read with group-01 offsets, the fault recorded from a real car on 2026-11-05. It takes about
half a minute, most of it the reader's own timeout.

| Option | What it does |
|---|---|
| `--expect fault` / `--expect clean` | makes the run a check: exit 1 unless that is the verdict |
| `--leg bridge` | puts the slow link between the bridge and the broker (a slow path to the car) instead of between the broker and the reader |
| `--hold-item ITEM` | which request to hold; the default is chosen from the profile (the nearest other request on the fast item's ECU) |
| `--hold-for S` | how long; the default is that item's timeout plus 2.5 s |
| `--enable TILE`, `--celllog TILE` | extra tiles to poll, and the cell log on one of them |
| `--delay-ms N` | a steady delay on the link as well, each way |
| `--scratch DIR` | where the run's files go (default: a new temporary folder, left for inspection) |

The cell-voltage form of the fault, with the cell log armed the way a long recording session has it:

```bash
./venv/bin/python tools/desk_rig.py --scenario stale-reply --hold-item lbc02 --enable cells --celllog cells
# on the page: {'soc': 1147.28, 'lv_volts': 3.927, 'pack_v': 40.2}, and one stored row with the same values
```

What it holds and which tiles it enables come from the vehicle profile (`FAST_ONLY`, `ITEMS`, `TILES`), not from a
list in the tool. A profile with a single request item has no such scenario and the rig says so.

## Bring the chain up and drive it by hand

```bash
./venv/bin/python tools/desk_rig.py
```

It prints the line that starts the dashboard against it (your own venv, `./web/app.py --adapter mqtt`, with five
environment variables), then takes commands:

```
stall 12 up            # nothing leaves in that direction for 12 s, then all of it, in order
delay 200 both         # every chunk 200 ms late
hold "data":"21 05" 12.5   # the next chunk carrying that text starts a 12.5 s stall, itself included
stats
quit
```

`up` is client to broker, `down` is broker to client. The proxy never drops, changes or reorders a byte: it is a slow
link, not a lossy one. `tools/laggy_link.py` also runs alone, in front of any TCP service on the loopback address.

## Kept apart from a real car

- **Topics:** the rig publishes under `hakake/desk` and refuses the default prefix a real car uses, so it cannot share
  a topic with a live bridge even on the same broker.
- **Data:** the application gets its own `HAKAKE_DATA_DIR` inside the scratch folder. No row reaches your database.
- **Network:** broker, link and dashboard listen on the loopback address only. The proxy refuses any other listening
  address, and refuses a target that is not loopback unless you pass `--allow-remote-target`.
- **Bus:** the CAN bus is python-can's in-process `virtual` interface. No adapter is opened.

## Tests

`tests/test_laggy_link.py` holds the proxy to its promises on loopback sockets. `tests/test_desk_rig.py` covers what
the rig decides without starting it; the one test that runs the whole chain is opt-in
(`HAKAKE_DESK_RIG=1 pytest tests/test_desk_rig.py`). `tests/test_mqtt_reply_integrity.py` reproduces the same fault
with a fake broker client and frames captured from the car, in under a second.

## Limits

- The link is delayed at the TCP level. Packet loss, MQTT-level redelivery and a broker restart are not modelled.
- The simulated ECU answers in a few milliseconds; a real battery controller takes a few hundred. Cycle times here
  say nothing about a car.
- A single bad cycle lasts tens of milliseconds at desk speed. The state file and the 5 s store are too coarse to
  promise they catch it, which is why the rig listens to the `state` topic.
