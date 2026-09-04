# Security Policy

## Reporting a vulnerability

Email **kn6irv@gmail.com** with "Leaf OBD security" in the subject. Expect an <!-- privacy-ok -->
acknowledgment within a few days. Please do not open a public issue for
anything you believe is exploitable before we have had a chance to respond.

## Scope — and a safety note first

This software **transmits on your car's diagnostic bus** — asking a question
still puts frames on the wire, unlike passive listening. It sends no write,
control, routine or security-access service anywhere. The dashboard's reader
sends UDS service `0x21` read requests (the Leaf's controllers), OBD-II modes
`01`, `03` and `07` (the Lancer's live data and trouble codes — mode `04`,
which clears codes, is never sent), and ELM327 monitor mode. The
console probe tools additionally send read-identification services (`0x22`,
`0x1A`, OBD mode `09`), and one legacy script sends `0x10` session control —
still no writes, but it does change an ECU's diagnostic session state. Nothing
in this repository sends control, routine, write, or security-access services.
Even so:

- **Polling can keep modules awake.** A parked car that would otherwise sleep
  may not, and a first-generation Leaf's 12 V battery is its most common
  failure. Do not leave the reader running for days on a parked vehicle.
- **It adds traffic to a safety-critical network.** Small, but not zero.
- Use it only on a vehicle you own or are authorised to work on.
- Never run it while driving unless the laptop is secured and someone else is
  driving. The dashboard is a passenger's tool.
- Do not add write/actuation commands (UDS `0x2E`, `0x2F`, `0x31`, `0x3D`,
  `0x27` security access) without an explicit, documented safety review. Pull
  requests that add them will not be merged.

## In scope

- Anything that lets the web dashboard (bound to 127.0.0.1) send commands to
  the adapter — the reader must be the only process that talks to the car.
- Parsing of adapter output (`leaf_decoders.py`, `elm327.py`) on malformed or
  hostile input.
- The writing APIs (`PUT /api/tiles`, `PUT /api/sim/tiles`,
  `PUT/DELETE /api/calibration`, `PUT/DELETE/POST /api/layouts/…`) touching
  anything other than their own JSON files (`web/tiles.json`,
  `web/sim_tiles.json`, `web/calibration.json`, `web/layouts.json`).
- The simulator's control API (`hakake_sim.py`, `127.0.0.1` only, no
  authentication) reaching anything but the in-memory model — it can put a
  fault on a dashboard someone is reading, and it must never be exposed.

## Out of scope

- The Flask development server itself — the dashboard is a local tool and is
  not meant to be exposed to a network.
- The ELM327 clone firmware.
