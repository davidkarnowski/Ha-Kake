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
still no writes, but it does change an ECU's diagnostic session state, and the
session it asks for (`1002`) is the programming one. Nothing
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

**Native CAN adapter.** Ha-Kake can also read through a USB-CAN controller
(CANable 2.0 class, `--adapter can`, `cantransport.py`). What "transmit" means
does not change: on Car-CAN the adapter sends the same read services the
ELM327 sends today — UDS `0x21` and OBD modes `01`, `03`, `07`, with their
ISO-TP flow-control frames — and nothing else; the façade refuses any request
whose service byte is outside that set before it reaches a bus, and logs it.
On EV-CAN — the battery, inverter and charger network — the adapter is opened
in the controller's listen-only mode, in which it transmits nothing, not even
the acknowledgement bit other nodes see; the transport refuses to open EV-CAN
in any other mode (and refuses the bus outright when it cannot confirm the
mode from the device), and answers every request on it with `NO DATA`. The
onboard termination resistor must be disabled: both of the Leaf's buses are
already terminated, and a third terminator degrades the signal for every
controller on that bus. Reviewed 2026-09-09 as a transport, not a new service.

### Remote ingestion (MQTT bridge)

`bridge/hakake_bridge.py` puts a **second process on the bus**: a Raspberry Pi
at the OBD port that mirrors frames to an MQTT broker and runs read requests
for a reader elsewhere (`docs/MQTT.md`). "The reader must be the only process
that talks to the car" therefore reads: *the reader, or a bridge that accepts
only its read requests*. The bridge enforces the same whitelist as the reader
— a `tx/uds` request whose service byte is not `0x21`, `0x01`, `0x03` or
`0x07` is refused, logged and never transmitted; mode `0x04` never leaves;
`--listen-only` refuses everything (and the kernel interface should be
brought up `listen-only on` as well). The reader refuses the same set before
publishing. Neither end trusts the other. The whitelist does not make the
broker safe to expose: anyone who can publish to `tx/uds` can keep ECUs awake
by polling — so the broker is **LAN only, plain MQTT without credentials or
TLS by design, and reached from anywhere else over Tailscale or a private
VPN**, which authenticates and encrypts the whole path; the broker is never
exposed to the internet directly. The dashboard still binds 127.0.0.1; publishing
the decoded record to `<prefix>/state` is outbound only and carries no way
back to the car. Reviewed 2026-09-09; not yet exercised against a real
broker, Pi or car.

## In scope

- Anything that lets the web dashboard (bound to 127.0.0.1) send commands to
  the adapter — the reader must be the only process that talks to the car.
- Parsing of adapter output (`leaf_decoders.py`, `elm327.py`) on malformed or
  hostile input.
- The writing APIs (`PUT /api/tiles`, `PUT /api/sim/tiles`,
  `PUT/DELETE /api/calibration`, `PUT/DELETE/POST /api/layouts/…`,
  `PUT/DELETE /api/bookmarks`) touching
  anything other than their own JSON files (`web/tiles.json`,
  `web/sim_tiles.json`, `web/calibration.json`, `web/layouts.json`,
  `web/bookmarks.json`).
- A request reaching the API from a page on another origin, or naming a host
  other than a loopback one: the server checks `Host` on every request, and
  `Origin` (or a JSON content type) on every change.
- The simulator's control API (`hakake_sim.py`, `127.0.0.1` only, no
  authentication) reaching anything but the in-memory model — it can put a
  fault on a dashboard someone is reading, and it must never be exposed.

## Out of scope

- The Flask development server itself — the dashboard is a local tool and is
  not meant to be exposed to a network.
- The ELM327 clone firmware.
