# 3. Walking discrete inputs

**What this chapter teaches**

- Five worked examples, byte-exact, all verified on a 2012 Leaf: doors, locks,
  exterior lights, turn signals, gear.
- The **physical** procedure for each walk, which is the part nobody writes down.
- Why a redundant mirror signal (`0x625`) was found, recorded, and deliberately
  not decoded into the product.
- How a capture can settle a *negative* question: `0x174` proven unable to
  separate P from N.
- Two honest gaps: the hazards value that was demoted from verified, and the
  pedals walk that was never run.

**What this chapter assumes:** [the method](02-the-method.md) — differential
capture, walking back down, and the scoring flags.

---

Every signal in this chapter lives on **`0x60D`**, **`0x358`**, **`0x421`** or
`0x174` on Car-CAN, captured passively with `ATCAF0` and a per-ID `ATCRA`
filter. None of it needs UDS. All of it is reachable from a stock OBD-II plug.

`0x60D` in particular is a busy little frame. Doors, locks, lights and the
ignition state all live in its first three bytes, which is convenient once you
know it and confusing before you do — a walk that changes doors will show byte 0
moving, and so will a walk that changes lights, and they are different bits of
the same byte.

---

## Doors — `0x60D` byte 0

**Verified, door walk 2026-08-25.** Fixture:
`tests/fixtures/walk_doors_20260825_110050.json`.

| Bit | Value | Door |
|---|---|---|
| 3 | `0x08` | Driver |
| 4 | `0x10` | Front passenger |
| 5 | `0x20` | Rear left |
| 6 | `0x40` | Rear right |
| 7 | `0x80` | Hatch / tailgate |

Baseline with everything shut is `00` in those bits. One bit per door, no
overlap in either direction.

### The physical procedure

The whole design of this walk is "only one bit can possibly move at a time".
Twelve steps:

```
 1  all shut       All doors + hatch shut, car unlocked (baseline)
 2  driver open    Open the DRIVER door
 3  driver shut    Shut the DRIVER door
 4  pass open      Open the FRONT PASSENGER door
 5  pass shut      Shut the FRONT PASSENGER door
 6  rl open        Open the REAR LEFT door
 7  rl shut        Shut the REAR LEFT door
 8  rr open        Open the REAR RIGHT door
 9  rr shut        Shut the REAR RIGHT door
10  hatch open     Open the HATCH / trunk
11  hatch shut     Shut the HATCH
12  all shut       Everything shut again (confirm we are back to baseline)
```

```bash
./venv/bin/python calibrate_input.py doors
```

Notice the shape. Every open is immediately followed by a shut, so the byte
returns to baseline five times during the walk. That is the `consistent` term
doing its work: the label "shut" appears six times and must give the same value
every time. And step 12 is a deliberate duplicate of step 1, so the walk proves
it ended where it started.

The result was described in the log as a "clean single-bit result", which is
what you hope for and do not always get.

Practical notes from doing it:

- Doing this parked with the car in READY makes the byte-1 start state stable.
  During the walk byte 1 read `06` throughout, which decodes as start state
  READY. It briefly dropped when the car was accidentally taken out of READY and
  had to be restored, which is exactly the kind of thing a baseline-last step
  catches.
- The passive target for this walk captures three IDs, `60D`, `5C5` and `625`.
  Neither `0x5C5` nor `0x625` moved for doors or locks. Recorded as a negative.
- Interior lights and door chimes make it easy to lose track of which door you
  actually opened. Say the step out loud before pressing Enter. This is not a
  joke; the walker prints the instruction for a reason.

The decoder now emits `door_driver`, `door_pass`, `door_rl`, `door_rr`,
`door_hatch` and a `door_any` roll-up, and the dashboard's Body tile swings open
the specific corner that is open.

Before this walk, the project had a *tentative* `0x60D` door map that grouped
front, rear and hatch rather than splitting per corner. The walk replaced a
plausible grouping with the actual bits. Plausible is not verified.

---

## Locks — `0x60D` byte 2

**Verified, lock walk 2026-08-25.** Fixture:
`tests/fixtures/walk_locks_20260825_110510.json`.

| Value | State |
|---|---|
| `0x18` | Locked |
| `0x00` | Unlocked |

Five steps, doors shut throughout, locking and unlocking with the key fob or the
door button:

```
1  unlocked   UNLOCKED (baseline)
2  locked     LOCK the car
3  unlocked   UNLOCK
4  locked     LOCK again
5  unlocked   UNLOCK — leave it unlocked
```

```bash
./venv/bin/python calibrate_input.py locks
```

Two presses in each direction is the minimum that is worth doing. One press
tells you a byte changed when you pressed a button; two tells you it changes
*with the state* rather than pulsing on the event. `0x18` is not obviously a
flag — it is two bits — and only seeing it return to exactly `0x18` on the
second lock makes it safe to treat as a state value rather than a coincidence.

---

## Exterior lights — `0x60D` bytes 0 and 1

**Verified, lights walk 2026-08-25.** Fixture:
`tests/fixtures/walk_lights_20260825_112526.json`.

| Byte | Bit | Lamp |
|---|---|---|
| 0 | `0x04` | Parking / position lights |
| 0 | `0x02` | Low beam (headlights) |
| 1 | `0x08` | High beam |
| 1 | `0x01` | Fog |

Byte 1 bits 1–2 remain the start state; the light bits sit around them without
disturbing it, which the walk explicitly checked.

### The physical procedure, rebuilt to match the car

The first version of this walk had a step list invented at a desk. It was wrong,
because a ZE0's headlight switch is not a set of independent buttons — it is a
rotating stalk with a fixed click sequence, plus a separate push for high beam,
plus a separate switch entirely for fog. You cannot go from PARKING to FOG
without passing through the positions in between, so a step list that asks you
to is a step list you cannot execute.

The rebuilt sequence follows the real hardware:

```
 1  off          Headlight switch OFF
 2  auto         First click: AUTO
 3  parking      Second click: PARKING / position lights
 4  headlights   Third click: HEADLIGHTS (low beam)
 5  high beam    Push the stalk forward: HIGH BEAM
 6  headlights   Release to LOW beam
 7  off          Headlight switch OFF
 8  fog on       Separate FOG switch ON (with headlights if required)
 9  fog off      FOG switch OFF
10  off          Everything OFF
```

```bash
./venv/bin/python calibrate_input.py lights
```

Hold each position about 3 seconds. The walk captures `60D`, `625`, `358` and
`5C5` at every step.

### AUTO reads as `00`, and that is correct

Step 2 (AUTO) produced the same bytes as step 1 (OFF). The walk was done in
daylight, the light sensor saw no reason to switch the lamps on, and so nothing
lit.

The instinct is to call this a failed step. It is not. **The walk is reading
switch state, not lamp output.** In daylight, AUTO and OFF genuinely are the
same state as far as these bytes are concerned, because these bytes report what
the lamps are doing and the lamps are doing nothing.

The walker's own intro text says so up front:

> In daylight AUTO may leave the lamps off — that is fine, we are reading the
> switch state.

The honest conclusion recorded in `docs/SIGNALS.md` is that AUTO mode is *not
distinguishable from OFF* on these bytes. If you want AUTO-versus-OFF you need a
different signal, or you need to run the walk after dark and accept that you are
then measuring the light sensor as well as the switch.

This walk also promoted an earlier guess. A tentative "`0x60D` b0 `0x02` might
be headlights" had been sitting in the notes since the first passive probe. The
walk confirmed it. Guesses are allowed; they just have to be labelled as guesses
until something like this happens to them.

---

## The `0x625` mirror, and why it stays undecoded

The lights walk captured `0x625` alongside `0x60D`, and `0x625` byte 1 turned
out to be a clean bitfield carrying the same information:

| Value | Lamp |
|---|---|
| `0x40` | Parking |
| `0x20` | Low beam |
| `0x10` | High beam |
| `0x08` | Fog |

Arguably it is a *nicer* encoding than `0x60D`'s, since all four lamps live in
one byte with no start-state bits mixed in.

It is recorded in `docs/SIGNALS.md` as **"observed in walks; not decoded — the
dashboard uses `0x60D`"**, and that is where it stops. No decoder, no test, no
registry entry, no tile.

`SIGNALS.md` records the choice in four words: "observed in walks; not
decoded". Here is the case for it, because the instinct is to decode everything
you find.

- The dashboard already polls `0x60D` for doors, locks and start state. Decoding
  the lights from `0x625` would add a second passive capture to every cycle to
  learn something the reader already knows. Over BLE each extra passive ID costs
  real time in a cycle budget measured in seconds.
- Two sources for one fact means two things that can disagree, and then a
  question about which one wins, and then a fusion rule, and then a bug in the
  fusion rule. The project already carries one such rule for battery current
  because there was no alternative; adding another voluntarily is not free.
- The evidence is not wasted. It is written down. If `0x60D` ever proves
  unreliable on a different Leaf, the mirror is documented and someone can
  switch to it in an afternoon.

**Recording an observation and declining to productise it is a legitimate,
completed piece of work.** The failure mode is not "we didn't use it" — the
failure mode would have been not writing it down.

---

## Turn signals — `0x358` byte 2

| Value | State | Confidence |
|---|---|---|
| `0x80` | Off | **Verified** (2026-02 capture) |
| `0x82` | Left | **Verified** |
| `0x84` | Right | **Verified** |
| `0x86` | Hazards | **Tentative** — community value, never captured on this car |

This one predates the walker. It was found with a plain filtered stream: 60
seconds on `0x358` alone while the stalk was toggled left, right and off,
yielding **605 frames with zero errors at about 10 frames per second**. The
transitions are visible in the raw stream without any analysis at all, which is
what a ~10 Hz body frame gives you.

```bash
./venv/bin/python gear_hvac_live.py gear 120     # same idea, prints only transitions
```

### The demotion, which is the point of including this

`0x86` for hazards carried a **verified** label in `docs/SIGNALS.md` for a
while. It was removed during a documentation-truth audit on 2026-08-26:

> And 0x358's `86` hazards value wore a "verified" label though the only capture
> holds only off/left/right — relabelled tentative (community value).

Nobody faked anything. `0x86` is almost certainly right: it is the bitwise OR of
left and right, it is what the community DBCs say, and the pattern is obvious.
The problem is narrower and more important than that: **the capture that
justified the row does not contain the value.** The stalk was toggled left and
right; the hazard button was never pressed.

"Almost certainly right" and "verified" are different claims, and the whole
grading scheme in `docs/SIGNALS.md` is worthless if the difference is allowed to
blur. The fix costs one word in a table. Not fixing it costs the credibility of
every other row.

Pressing the hazard button for ten seconds would settle it. Until someone does,
it says tentative. See [chapter 7](07-confidence-and-honesty.md).

---

## Gear — `0x421` byte 0

**Verified, all five positions, 2026-08-24.**

| Value | Gear |
|---|---|
| `0x08` | P |
| `0x10` | R |
| `0x18` | N |
| `0x20` | D |
| `0x38` | Eco |

This took two live captures rather than a walker preset, because shifting is
something you do with a foot on the brake and a hand on the shifter, which does
not leave a hand for the Enter key.

```bash
./venv/bin/python gear_hvac_live.py gear 120
```

**First capture (19:03).** Confirmed `08` = P, `10` = R, `20` = D by shifting
them live. `18` = N and `38` = Eco were at that point still *expected from a
community DBC and not yet observed*, and were labelled that way.

**Second capture (19:12–19:13).** A full sweep, P → R → N → D → Eco → P, in one
pass. All five values observed on this car. The table above became verified in
its entirety.

The gap between those two captures is nine minutes and the difference in claim
strength is large. It is worth noticing how cheap the second capture was
relative to how much it bought.

### The capture that closed a negative

`0x174` byte 3 was captured simultaneously in both sessions. It had been the
project's original gear signal, found back in February:

| `0x174` b3 | Gear |
|---|---|
| `0xAA` | P **or** N |
| `0x99` | R |
| `0xBB` | D **or** Eco |

February could not tell whether that ambiguity was real or an artefact of not
having captured carefully enough. A whole session had gone into it: a
Drive-versus-Eco diff tool compared 12 CAN IDs between the two states and found
no stable differentiator, with every apparent change turning out to be a rolling
counter or an odometer. The open question was left as "D/Eco distinction may
require EV-CAN bus access".

The second gear sweep answered it. Across P → R → N → D → Eco → P, `0x174`
byte 3 read:

```
AA   99   AA   BB   BB   AA
 P    R    N    D   Eco   P
```

**In lockstep with `0x421`, and with P and N producing the identical byte, and D
and Eco producing the identical byte.** That is not a failure to capture. That
is a positive demonstration that the information is not present in that byte,
under exactly the conditions where it would have to be.

The February question closed as a definite no, and the reader now polls `0x421`
for gear. `0x174` remains in `docs/SIGNALS.md` marked "verified — cannot split
P/N or D/Eco; not polled".

This is the shape of a good negative result: a specific claim about a specific
byte, backed by a capture that contains every state the claim covers.

---

## Side observation: start state on `0x60D` byte 1

Bits 1–2 of `0x60D` byte 1 appear to encode the ignition state:

| Value | State |
|---|---|
| 0 | Off |
| 1 | Accessory |
| 2 | On |
| 3 | Ready |

`06` was observed as READY, repeatedly and stably, across several walks. It is
labelled **tentative** in `docs/SIGNALS.md`, and it should be, because it came
from a passive probe and only one of the four states has ever actually been seen
on the bus. The doors, locks and lights walks all happened with the car in READY
throughout, so they confirm it is stable — not that the other three values are
right.

Walking it properly is easy and nobody has done it: park, then step off →
accessory → on → ready → off with the byte on screen. Five steps.

---

## The gap: pedals

`calibrate_input.py` has a `pedals` preset. It has never been run.

```
rest           Both pedals up (baseline)
throttle 25    Accelerator ~1/4
throttle 50    Accelerator ~1/2
throttle 100   Accelerator to the floor
throttle 50    Ease back to ~1/2
rest           Accelerator up
brake soft     Press the brake gently
brake hard     Press the brake firmly
rest           Both pedals up
```

It captures `180`, `292` and `1D5`, and it is designed to be safe: READY, gear
in Park, parking brake set, so pressing the accelerator moves nothing.

Because it has not been run, both signals it would settle remain **tentative**:
`0x180` byte 5 as throttle ÷ 2 %, and `0x292` byte 6 as the brake pedal. The
throttle byte is decoded but not even polled by the reader. The Body tile does
light its brake lamps from `0x292`, and it does so from a tentative decode,
which `docs/SIGNALS.md` states outright.

The preset is written. The car exists. This is simply not done, and the docs say
so rather than implying otherwise.

---

## Try it on your own car

Doors are the best first target on any vehicle, for three reasons: the state is
unambiguous, you can change it from outside the car with the laptop on the seat,
and it is almost always a clean bitfield.

1. **Find the body frame.** Filter each candidate ID in turn and open one door
   while watching. `door_watch.py` in this repo does exactly this — it streams
   several IDs and prints a line only when a byte changes:

   ```bash
   ./venv/bin/python door_watch.py 120 --ids 60D,5C5,625
   ```

   Adapt the ID list to your car. Watching a live diff for thirty seconds while
   you wiggle a door is often faster than any structured walk for finding
   *which* ID to walk.

2. **Then walk it properly.** Baseline, one door at a time, each open
   immediately followed by a shut, baseline again. Do not open two doors at
   once, however tempting it is to save time.

3. **Expect a bitfield, and check for overlap.** If two doors set the same bit,
   your car groups them and you should say so rather than inventing per-corner
   detail the bus does not have.

4. **Locks next**, both directions at least twice. Watch for the value being a
   multi-bit pattern like this car's `0x18` rather than a single flag.

5. **Lights: get the switch sequence from the car, not from your head.** Sit in
   the driver's seat and write down the actual click order before you write the
   step list. Then accept that AUTO in daylight may be indistinguishable from
   OFF, and say so in your notes instead of retrying it five times.

6. **Gear needs a second person or a long capture window.** Use a
   transitions-only live stream rather than a step-and-Enter walk, and sweep
   *every* position in one pass so no state is left "expected from a DBC".

7. **When you find a second ID mirroring the first, write both down and pick
   one.** Note in your docs which one you picked and why.

8. **Anything you did not physically actuate is tentative**, no matter how
   obvious the value looks. Hazards is `0x86`. Probably. Nobody pressed the
   button.

Next: [analog and scaled values](04-analog-and-scaled.md), where the failure
mode changes from "wrong byte" to "nearly right scale".
