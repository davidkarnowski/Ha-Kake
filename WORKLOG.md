# Work log

Significant milestones only, newest last. What each signal means and how sure we
are of it is in [`docs/SIGNALS.md`](docs/SIGNALS.md); the current state and plans
are in the README "Status" section and [`docs/ROADMAP.md`](docs/ROADMAP.md).
Day-to-day development notes — sessions, drives, probes, measurements — are kept
privately by the maintainer and are not part of this repository.

- **2026-02-15** — First contact with a 2012 Leaf through a Bluetooth ELM327 on
  Car-CAN: gear position decoded, battery state and cell voltages read from the
  battery controller (UDS group reads, read-only).
- **2026-02-19** — USB ELM327 support; energy and power from battery group 05.
- **2026-08-24** — The web dashboard: Flask and SQLite, the reader in its own
  process, tile-driven polling (~2 s cycle); Car-CAN body signals (doors, locks,
  lights, gear) and the HVAC amplifier decoded.
- **2026-08-25** — Logging: key signals as columns, an event log of state changes.
- **2026-08-28** — Vehicle profiles: a second car (2009 Lancer) through standard
  OBD-II PIDs, and a read-only trouble-code readout.
- **2026-09-02** — Replay: the whole stack runs from a recorded session, no car.
- **2026-09-03** — The vehicle simulator and its cockpit; group-05 current resolved.
- **2026-09-07** — Audible threshold alerts on any tile value.
- **2026-09-08** — The 3D battery pack tile, playback of recorded sessions, and
  the cell log.
- **2026-09-09** — Native CAN transport (CANable 2.0 class) and MQTT ingestion
  with a public JSON protocol; acquisition timing, several adapters at once,
  per-value provenance; a simulated CAN bus for testing at real frame rates.
- **2026-09-10** — A fixed-range colour scale for the cell grid and 3D pack.
- **2026-09-11** — Trouble-code dictionary format; the raw output console.
- **2026-09-12** — Fix: the dashboard no longer goes offline on a profile with no
  battery pack.
- **2026-10-03** — The CANable runs on the Leaf's Car-CAN: a full read cycle in
  ~0.6 s against ~2 s over Bluetooth (ISO-TP separation time 5 ms for the HVAC
  amplifier; listen-only mode no longer reads as a sleeping car). The live page is
  pushed over Server-Sent Events with millisecond read timings — every cell read
  drawn, ~2.8 per second. CI runs locally before each push; no hosted workflows.
