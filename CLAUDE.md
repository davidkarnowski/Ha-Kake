# CLAUDE.md — agent working guide for the Leaf OBD Dashboard

Enough context for an AI agent (or a new human) to work here safely.
Living document — update it in the same commit as the change it describes.

## 1. What this is

A read-only telemetry dashboard, first and mainly for a 2011–2012 Nissan Leaf,
read through an ELM327 adapter. One process talks to the car (`web/reader.py`);
Flask serves the page; SQLite keeps everything forever. The owner tests every
decode in the actual car — many sessions in this log happened in a parked Leaf
with the AC running. A second profile (`lancer_2009`) and two hardware-free
harnesses (replay, simulator) came later; see §5.

## 2. Governing documents

- `docs/SIGNALS.md` — the authority on what each byte means and how sure we are.
  A decoder change without a SIGNALS.md change in the same commit is incomplete.
- `WORKLOG.md` — append-only session log. Never edit old entries.
- `docs/ROADMAP.md` — roadmap and status line.
- `SECURITY.md` — the read-only rule. Non-negotiable. Read-only is a claim
  about the *services* sent (all reads: `0x21`, modes `01`/`03`/`07`, monitor
  mode), not about staying off the bus — we transmit. Adding a write,
  control, routine or security-access service needs a documented review.

## 3. Conventions

- **Temperatures: always °C / °F together.** Decoders emit both (`*_c`, `*_f`).
- **Current sign: negative = discharge.** Power follows.
- **Passive sniffing uses `ATCAF0`; UDS uses `ATCAF1`.** Mixing them produces
  `DATA ERROR` (this cost a whole session in February).
- **Never share a sqlite3 connection across threads** — it segfaults. Flask
  uses a thread-local `Store`; the reader is a separate process.
- **`web/reader.pause`** hands the adapter to a calibration tool; remove it to resume.
- Tests run without hardware: `pytest -q` (venv active). Fixtures are
  real captures; add one when you add a decoder.
- Python 3.12 (tested; ≥3.10 required — pyobjc 12). The venv is gitignored.

## 3b. Sub-agent work must keep a progress log (non-negotiable)

Any task delegated to a sub-agent is run with **active logging by that agent
into a markdown file**, so the work can be resumed and re-contextualised after
a token limit, rate limit or disconnection. On 2026-09-03 three concurrent
agents were killed mid-flight by a session limit; recovery was possible only
because the working tree happened to be inspected first. Logs make it routine.

- **Path:** `research/agent-logs/<task-slug>-<YYYYMMDD>.md` (gitignored,
  survives the session). The orchestrator names the path in the brief.
- **Append-only, timestamped entries at every milestone**, not a narrative:
  the brief in one self-contained paragraph (so the log alone is enough to
  resume), the file-ownership list, each decision and its reason, each file
  created or edited with *done / pending* per item of the brief, test status
  (`N passed`), and a **`NEXT:` line kept current** — what the very next
  action is. Write the first entry before touching any code.
- **On resume:** read the log first, then `git diff --stat` on the owned
  files, then continue from `NEXT:`. Resume messages point at the log.
- Logs are working notes for recovery, not documentation; they never move
  into the repo. Delete or keep them at the owner's discretion.

**A brief must name the docs its change invalidates.** Narrow file ownership is
what let the docs drift on 2026-09-03: three lanes owned code, none owned
`README.md` or `docs/`, and the tests badge sat sixty-six tests behind while the
tile menu and the power model went undescribed. So every sub-agent brief lists
the living docs the change touches — README status and counts, `docs/SIGNALS.md`,
`docs/ARCHITECTURE.md`, the doc for the subsystem — and puts them in that lane's
ownership. If a lane genuinely cannot own them (two agents editing one file), the
phase ends with an explicit doc-sync step before the commit, named in the plan.
The rule in §2 does not bend: docs land in the same commit as the change.

## 4. Branching & commits

- `main` is sacred for code. Work on `feature/…`, `bug/…`, `arch/…`; tests
  green before a fast-forward merge. About to edit code on `main`? Stop and
  confirm a branch name with the operator.
- Commits: `area: plain-English subject` with a **narrative body** — the why,
  the tradeoffs, what was verified and how (bench / car). Never amend, squash,
  or force-push to tidy recent work.
- **Attribution — exactly one trailer, and no session identifiers.** When an
  assistant wrote or co-wrote the change, end the message with a single
  `Co-Authored-By:` line naming the model, e.g.

      Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>

  The work is the author's and the assistant's together, and the commit says
  so. **Never add a `Claude-Session:` URL, a conversation link, a run id or any
  other session identifier.** Those are machine- and account-specific, they
  are useless to anyone reading the repo, and §4b's sweep rejects them — a
  rule the project keeps strict rather than relaxing to fit a convention.
  If tooling injects one, strip it before committing.
- Status tables (README "Status", plan status line) update in the same commit
  as the work they describe.

## 4b. Before any push — privacy sweep (non-negotiable)

Run `python scripts/privacy_sweep.py --log 50` (the pre-push hook
does it too). It must report `privacy sweep OK`. Look for: absolute home
paths (`/Users/…`), usernames, e-mails, the BLE adapter UUID, VIN, serial
numbers, IPs, keys, Claude session URLs in files. Fix the file, move the
material to `research/` (gitignored), or — only for content meant to be
public, like the security contact — mark the line `privacy-ok`. Machine
specifics go in `config.local.json` (gitignored), never in code.

## 5. Things that are intentional

- The reader polls only what enabled tiles need. Built-in tiles list their
  items in `TILES`; user tiles resolve theirs through `signals.py`.
- Vehicle-specific code (items, tiles, signal entries, decode, sensor policy)
  lives in `vehicles/<profile>.py`; `reader.set_vehicle()` binds it and
  `--vehicle` selects it (default `leaf_ze0`). Nothing outside `vehicles/`
  and `leaf_decoders.py` may hardcode a vehicle.
- Adding an input follows `docs/ADDING_SIGNALS.md` — fixture, decoder, test,
  item, registry entry, SIGNALS.md row — in one commit.
- `STORE_PERIOD` = 5 s even though cycles are ~2 s: the state file is live,
  the database is for trends.
- Group 05 current is ÷1024, not ×0.001 — they differ by 2.4 % and 1024
  matches the group-01 sensors.
- Legacy scripts stay in `legacy/` for reference; do not resurrect their
  copy-pasted transports.
- The simulator is a fixture, not a verifier: `--adapter sim` is never
  auto-detected, its rows never reach `web/leaf_battery.db`, every load in
  `simulator/model.py` `LOADS_W` carries a MEASURED / OWNER REPORT / ASSERTED
  label, and `current_a` is *extra* current on top of the modelled loads
  (default 0; `load_kw` is the absolute override). The cockpit (`/sim`)
  generates every control from `/sim/schema`; the control API's own landing
  page (`simulator/panel.html`) is a curl-and-endpoints fallback. Neither
  names a knob — the schema is the only list, and tests enforce it.

## 6. Where to look

| Question | File |
|---|---|
| What does byte N mean? | `docs/SIGNALS.md`, then `leaf_decoders.py` |
| Why is the dashboard slow / stale? | `web/reader.py` scheduler, `item_age` in `/api/status` |
| How did we find X? | `WORKLOG.md` (search the CAN ID) |
| Adapter won't talk | `elm327.py` header comments, README "Hardware" |
| Add another vehicle? | `vehicles/__init__.py` contract docstring; `vehicles/lancer_2009.py` is the minimal example |
| Simulate the car / drive the cockpit | `docs/SIMULATOR.md`; `python web/app.py --adapter sim`, then `/sim`; the core's interface is `docs/SIMULATOR_CONTRACT.md` |
