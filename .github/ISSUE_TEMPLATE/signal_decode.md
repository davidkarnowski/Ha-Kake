---
name: Signal decode
about: Report a wrong decode, or propose a newly decoded signal
title: 'signal: '
labels: signal
assignees: ''
---

`docs/SIGNALS.md` is the authority on what each byte means and how sure we are.
This template mirrors its discipline — please fill in the confidence and the
evidence honestly. "I'm not sure" is a perfectly good answer and is much more
useful than an overstated one.

## Signal

- **Name / what it is**:
- **Vehicle profile** (`leaf_ze0`, `lancer_2009`, new profile — which car):
- **Bus** (Car-CAN / EV-CAN / OBD):
- **CAN arbitration ID** (or UDS service + group/DID):
- **Byte offset(s), bit position(s), width, endianness**:
- **Scale / offset / units** (and both °C and °F if it is a temperature):
- **Sign convention** (this project uses negative = discharge for current):

## Confidence

Pick exactly one, matching the levels used in `docs/SIGNALS.md`:

- [ ] **verified** — I have observed this value change on a real car in
      response to a physical input, and the decode tracked it.
- [ ] **static** — the value is plausible and stable, but I have not seen it
      move; I cannot yet rule out coincidence.
- [ ] **tentative** — from community documentation or inference, not yet seen
      to move on my car.

## Evidence

**What did you do to the car, and what did the bytes do?**

<!-- e.g. "opened the driver's door, byte 2 bit 3 went 0 -> 1 and back on
     close, ten times". For an analog value, give at least three points across
     the range, with the reference you compared against (dash readout,
     multimeter, scan tool). -->

**Raw frames** (before/after, in a code fence):

```
```

**Reference instrument / ground truth used**:

## What could make this wrong?

<!-- See docs/reverse-engineering/06-when-youre-wrong.md and
     07-confidence-and-honesty.md. What else changed at the same time?
     Could this be a duplicate of a signal already in SIGNALS.md? -->

## If you are proposing a code change

A decoder change is only complete with all of it in one commit: fixture,
decoder, test, item, registry entry, and the `docs/SIGNALS.md` row. See
`docs/ADDING_SIGNALS.md`.

- [ ] I can attach a fixture capture for `tests/fixtures/`
- [ ] I have removed VIN / adapter UUID / personal data from the capture
