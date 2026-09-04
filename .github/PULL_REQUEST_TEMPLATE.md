## What this changes, and why

<!-- The narrative belongs in the commit message; a couple of sentences here
     is enough. Say what you verified and how — on the bench, on the car, or
     both. -->

## How it was verified

- [ ] On the bench / from fixtures only
- [ ] On a real car (which one, and what did you watch to confirm it?)

## Checklist

- [ ] **Tests pass** — `pytest -q` green with the venv active.
- [ ] **Fixture added** if a decoder changed — every decoder is exercised
      against real captured frames in `tests/fixtures/`; no test touches a car.
- [ ] **`docs/SIGNALS.md` updated in this same commit** if a CAN ID, byte
      offset, scaling or confidence level changed. A decoder change without a
      SIGNALS.md change is incomplete. Confidence marked **verified** only with
      a capture that shows the value moving.
- [ ] **Privacy sweep run** — `python scripts/privacy_sweep.py --log 50`
      reports `privacy sweep OK`. No home paths, usernames, e-mails, adapter
      UUIDs, VINs, serials, IPs, keys or session URLs.
- [ ] **DCO sign-off present** — every commit made with `git commit -s`, so it
      carries a `Signed-off-by:` line. Contributions are offered under
      Apache-2.0; see `CONTRIBUTING.md`.
- [ ] **Read-only rule respected** — this PR adds **no** control, routine,
      write or security-access service (UDS `0x2E`, `0x2F`, `0x31`, `0x3D`,
      `0x27`) and nothing that actuates the vehicle. See `SECURITY.md`; PRs
      that add them will not be merged.
- [ ] **Vehicle seam respected** — no vehicle-specific values hardcoded
      outside `vehicles/` and `leaf_decoders.py`.
- [ ] **Temperatures emit both** `*_c` and `*_f`, if this touches temperatures.
- [ ] **Docs and status tables** updated in the same commit as the work
      (README "Status", `docs/ROADMAP.md` status line) if they are now stale.
- [ ] `WORKLOG.md` entry appended if this involved a session with the car
      (append-only — never edit an old entry).

## Anything you are unsure about

<!-- Honest uncertainty is welcome and speeds up review. -->
