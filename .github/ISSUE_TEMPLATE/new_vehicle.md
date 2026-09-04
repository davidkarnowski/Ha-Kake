---
name: New vehicle profile
about: You want Ha-Kake to support a car it doesn't know yet
title: 'vehicle: '
labels: vehicle-profile
assignees: ''
---

Non-Leaf vehicles are welcome. Please open this issue **before** writing much
code — read `CONTRIBUTING.md` ("Bringing a different vehicle") first. The
profile contract is documented in `vehicles/__init__.py`, and
`vehicles/lancer_2009.py` is the minimal worked example.

## The car

- **Year, make, model, trim**:
- **Market** (US / EU / JP / other — trims and buses differ):
- **Powertrain** (ICE / hybrid / EV):

## What does it answer?

- [ ] It responds to **standard OBD-II mode 01** (live PIDs)
- [ ] It responds to **mode 03 / 07** (stored / pending DTCs)
- [ ] It responds to **mode 09** (VIN, calibration IDs)
- [ ] It answers **UDS** (`0x22` read-by-identifier, `0x21` read-by-local-ID)
- [ ] I don't know yet

**Protocol / bus** (CAN 11-bit 500k, CAN 29-bit 250k, ISO 9141, KWP, unknown):

**Which PIDs / IDs have you actually seen respond?**

```
```

## Fixtures

Raw captures are worth more than guesses about meaning — a profile can't be
merged without them, since no test may touch a real car.

- [ ] I can capture raw frames from this car and attach them
- [ ] I have an adapter that works with Ha-Kake (which one? )
- [ ] I can re-test on the car when asked to verify a decode
- [ ] I can't capture — I'm reporting from documentation only

## Anything already known publicly

<!-- DBC files, forum threads, other projects that decoded this car.
     Link them; we record provenance in NOTICE. -->

## Expectations

Be aware: `docs/ADDING_A_VEHICLE.md`, a profile-level walkthrough, is planned
but not written yet, so early vehicle work has rough edges and will involve
some back-and-forth with the maintainer. The no-hardware replay harness does
exist (`--adapter replay`, `docs/REPLAY.md`) — once you have captured a session
from the car with `record_session.py`, the profile can be developed and
reviewed without it.
