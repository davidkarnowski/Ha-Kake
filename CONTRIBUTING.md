# Contributing

Thanks for your interest. This is a small, hardware-in-the-loop project;
here is what makes a contribution easy to merge.

## Practical rules

- **Branches:** `main` is sacred for code. Work on `feature/…`, `bug/…`, or
  `arch/…` branches; tests green before a fast-forward merge.
- **Tests:** `pytest -q` (venv active). No test touches the car — every
  decoder is exercised against real frames captured into `tests/fixtures/`.
  New decoding needs a fixture and a test; contracts other code relies on get
  pinned by one.
- **Commits:** `area: plain-English subject` with a narrative body — the why,
  the tradeoffs, what was verified (on the bench, on the car, or both). The
  log is documentation. Never amend, squash, or force-push to tidy recent
  work; the messiness is the record.
- **Work log:** `WORKLOG.md` is append-only and never
  retroactively edited. Every session that touches the car gets an entry with
  what was tried, what the bytes said, and what was concluded.
- **Signals:** a new CAN ID or byte offset goes into `docs/SIGNALS.md` in the
  same commit as the decoder, marked **verified** (observed changing with the
  physical input) or **tentative** (from community documentation, not yet
  seen to move on this car). Do not promote tentative to verified without a
  capture that shows it.
- **Before every push:** `python scripts/privacy_sweep.py --log 50`
  must pass (the `.githooks/pre-push` hook runs it). Home paths, adapter UUIDs,
  VINs, keys and session URLs never go public; personal captures live in
  `research/`, which is gitignored. Mark a deliberately public line (like the
  security contact) with `privacy-ok`.
- **Temperatures** are always presented as °C / °F together.
- **Safety:** read-only. See SECURITY.md before adding any UDS service other
  than `0x21`.

## Licensing — please read before you send a patch

Ha-Kake ships under the **GNU Affero General Public License, version 3 or
later** (AGPL-3.0-or-later); see `LICENSE`. The documentation under `docs/`
ships under **CC BY-SA 4.0**; see `LICENSE-DOCS`.

**Contributions are offered to this project under the Apache License 2.0.** By
submitting a pull request you are licensing your contribution under Apache-2.0,
which permits the maintainer to include it in the AGPL-licensed project and in
separately licensed builds. Documentation contributions are offered under
CC BY-SA 4.0, matching the docs license.

Why the inbound license differs from the outbound one, plainly: the maintainer
owns the copyright in the existing code and sells proprietary licenses to
companies that cannot ship under the AGPL (see `COMMERCIAL.md`). That is how
this project gets funded. Apache-2.0 inbound keeps that possible — your patch
can be folded into a commercially licensed build — without asking you to sign
and return a CLA. You keep your copyright; you are granting a license, not
assigning ownership. That is the whole trade, and you should know about it
before you send code rather than after.

If you are not comfortable with that, say so in the issue before writing the
patch — a bug report, a fixture capture, or a clear description of the fix is
still enormously useful and carries none of this.

### Sign your commits off (DCO)

Instead of a CLA this project uses the **Developer Certificate of Origin 1.1**
— the same mechanism the Linux kernel uses. Commit with `-s`:

```
git commit -s -m "decoders: fix HX scaling on AZE0 captures"
```

which appends a line to your commit message:

```
Signed-off-by: Jane Hacker <jane@example.com>
```

Use your real name and an address that reaches you. That line is your
certification of the DCO, reproduced in full below (the canonical text lives at
<https://developercertificate.org/>):

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Do not add SPDX headers or copyright lines to files in a feature PR; header
hygiene is handled in its own pass.

## Bringing a different Leaf or adapter

(For a car that is not a Leaf at all, see "Bringing a different vehicle" just
below — that path exists and is welcome.)

The decoders are for the 2011–2012 ZE0. AZE0 (2013–2017) and ZE1 (2018+)
move several offsets and change scalings; if you have one of those, the most
useful first contribution is a fixture capture (`probe_hvac_carcan.py`,
`battery_read.py --raw`) with the model year noted, before any decoder change.

## Bringing a different vehicle

**Non-Leaf vehicles are welcome.** The reader is vehicle-agnostic: everything
car-specific — items, tiles, signal-registry entries, decode functions and the
sensor policy — lives in a profile module under `vehicles/`, selected with
`--vehicle`. Nothing outside `vehicles/` and `leaf_decoders.py` may hardcode a
vehicle, and a PR that breaks that seam will be sent back.

Where to start:

- `vehicles/__init__.py` — the profile contract, documented in its docstring.
  This is the authority on what a profile must provide.
- `vehicles/lancer_2009.py` — the minimal worked example. A 2009 Mitsubishi
  Lancer that answers standard OBD-II and reads DTCs; roughly the smallest
  useful profile, and a good shape to copy.
- `docs/reverse-engineering/` — the guide to getting on the bus, walking
  discrete inputs, scaled analog values, UDS/ISO-TP, and (importantly) how to
  tell when you are wrong. Read `07-confidence-and-honesty.md` before you mark
  anything **verified**.
- `docs/ADDING_SIGNALS.md` — the per-signal routine: fixture, decoder, test,
  item, registry entry, `docs/SIGNALS.md` row, all in one commit.

**Expect rough edges, and talk to the maintainer first.** Open a
"New vehicle profile" issue before you write much code. What exists and what
does not:

- **The replay harness exists.** `python web/app.py --adapter replay --vehicle
  <profile>` drives the whole stack — reader, scheduler, transport, decoders,
  store, API, page — from a recorded session fixture in `tests/fixtures/`,
  made by `record_session.py`; `tests/test_replay_e2e.py` runs both shipped
  profiles end to end. So a profile can be developed and reviewed without the
  car, once someone has captured a session from it. See `docs/REPLAY.md`.
  There is a simulator too (`--adapter sim`, `docs/SIMULATOR.md`), but it is a
  fixture for the *dashboard*, not evidence about any real vehicle.
- `docs/ADDING_A_VEHICLE.md` — the profile-level walkthrough: the decision
  tree, every required and optional attribute, the traps, and a worked example
  following the Lancer from first capture to a working tile.

The seam itself is young — it was cut when the Lancer became car number two, so
it has been exercised exactly twice. If your car needs something the contract
cannot express, that is a finding about the contract, not a failure on your
part; say so in the issue and we will move the seam.

The most useful first contribution for any new vehicle is a **fixture capture**
with the model year and adapter noted — before any decoder change. Raw frames
are worth more than a guess at what they mean.

## AI involvement

Much of this code was written with an AI assistant working from live
captures; the human operator was in the car for every calibration. Commits
carry a `Co-Authored-By` trailer when that is the case. Review what you
merge — the assistant is confident, not infallible.
