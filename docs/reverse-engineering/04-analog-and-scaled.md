# 4. Analog and scaled values

**What this chapter teaches**

- Continuous values fail differently from discrete ones: the trap is a decode
  that is *nearly* right.
- Fan speed turned out to be blower **volts**, and two of the seven speeds are
  physically indistinguishable. The dashboard says so.
- The setpoint byte lags on the way down, which reveals it is an air-mix target
  rather than the number you dialled.
- The ÷1024 story: a scale error of **2.4 %** that passed every plausibility
  check and was only caught by a second, independent sensor.
- How to cross-check a scaled value: an independent measurement of the same
  quantity, or a known external reference.

**What this chapter assumes:** [the method](02-the-method.md), and that you have
seen a walk produce a ranked byte in
[chapter 3](03-walking-discrete-inputs.md).

---

## Why continuous values are harder

A discrete signal is either right or obviously wrong. If you claim `0x20` is
Drive and it turns out to be Neutral, the car tells you the moment you shift.

A scaled value has a much worse failure mode. You pick a byte, you pick a
divisor, and the number that comes out **looks completely reasonable**. It sits
in the right range. It moves the right direction. It responds to load. It has
the right sign. And it is wrong by a few percent, forever, in a way that nothing
in your own system can detect, because your only reference is the number you
just computed.

The rest of this chapter is examples of that, and of the two things that
actually catch it.

---

## Fan speed is not fan speed

**Group 10 byte 11, HVAC amplifier (`0x744 → 0x764`, service `21 10`).
Verified by the fan walk 2026-08-24 and the HVAC on/off walk.** Fixture:
`tests/fixtures/walk_fan_20260824_221450.json`.

The walk stepped the fan 1 → 7 → 1. Byte 11 read:

| Fan speed | Byte 11 | Bit 7 | Low bits |
|---|---|---|---|
| 1 | `84` | 1 | 4 |
| 2 | `85` | 1 | 5 |
| 3 | `86` | 1 | 6 |
| 4 | `88` | 1 | 8 |
| 5 | `89` | 1 | 9 |
| 6 | `8B` | 1 | 11 |
| 7 | `8B` | 1 | 11 |
| HVAC off | `00` | 0 | 0 |

The same values came back on the way down, which is what made this a decode
rather than a coincidence.

Now look at the low bits: 4, 5, 6, 8, 9, 11, 11. That is not a speed index. A
speed index for a seven-position knob would be 1–7, or 0–6, or some scaled
version of them. The gap between 6 and 8, and the repeat at 11, say something
else is being reported.

**It is the voltage the amplifier sends the blower motor.** 4 V at speed 1, up
to about 11 V at the top, with bit 7 as a separate "blower on" flag that goes to
zero when the HVAC system is switched off entirely.

That reframing is the whole finding. The ECU was never asked "what speed is the
fan on". It was asked for its diagnostic state, and its diagnostic state is the
thing it actually controls: a motor drive voltage. Speed is a human abstraction
layered on top by the control panel.

Once you know that, the decode writes itself as a nearest-volts lookup, and the
dashboard's fan rotor spins at a rate proportional to volts rather than to a
speed number, which is both easier and more truthful.

### Speeds 6 and 7 are indistinguishable, and the UI admits it

Both read `8B` — 11 V — in **every sample of the walk**.

The first pass at this, written the same evening, hedged: it recorded 6 as 11 V
and 7 as "~12 V, sampled at 11 V while still ramping", and marked 6-vs-7
tentative. That was a reasonable guess about a transient. It was checked, and
the correction is recorded the same day:

> Fan 6 vs 7: both read `8B` (11 V) in every sample of the walk — the amp does
> not distinguish them.

There are three things you can do here. You can pick one and pretend. You can
show the raw voltage and make the user do the mapping. Or you can say what you
know. The dashboard **shows "6–7"** at 11 V, and `docs/SIGNALS.md` explains why.

One further piece of honesty in the same row: the decoder's nearest-volts table
has a 12 V entry that maps to speed 7, inherited from the original guess. **That
slot has never been observed on this car.** It is documented as such rather than
quietly removed, because it is still in the code and a reader deserves to know
which branches have been exercised.

---

## Setpoint — group 10 byte 12, and the lag that gives it away

**Verified proxy, setpoint walk 2026-08-24 (60 → 90 → 60 °F, all 13 steps).**
Fixture: `tests/fixtures/walk_setpoint_20260824_224401.json`.

Byte 12 ran from **111 to 173** across 60 to 90 °F, roughly 11 counts per 5 °F.
That gives:

```
°F ≈ 60 + (raw − 111) × 30 / 62
```

Decoded as `hvac_target_f`, accurate to about ±2 °F against the dial.

The interesting part is not the fit. It is the residual.

**Coming back down, the byte lagged the dial by 1 to 3 counts.** Set 90, walk
down to 85, and the byte does not land on exactly the value it had on the way
up. Dial 80, and it is a count or two off. Small, consistent, and only on the
descending half.

A pure setpoint register would not do that. A setpoint is a number you wrote; it
reads back as what you wrote, in either direction. A value that overshoots on
one side and takes a moment to settle is a **target that something is
controlling toward** — here, the air-mix door position the amplifier is aiming
for, which depends on the cabin's current thermal state as well as your dial.

So the honest label is a *proxy*: it tracks the setpoint closely, it is derived
from a real physical target, and it is not literally the number on the panel.
The dashboard writes it with a "≈" for exactly that reason.

Two general lessons:

- **The disagreement between the up-walk and the down-walk is data.** The walk
  is designed to catch drift, but when the same label gives *nearly* the same
  value rather than exactly the same value, that near-miss tells you something
  about the physics behind the byte. Do not average it away.
- **Say what a value is, not what you wish it were.** "Setpoint" and "air-mix
  target that follows the setpoint" behave identically 95 % of the time and
  differently in exactly the cases a user would notice.

---

## A/C compressor — group 10 byte 10, bit 7

**Verified, A/C walk 2026-08-24** (off, on, off, on, off — five steps).
Fixture: `tests/fixtures/walk_ac_20260824_222922.json`.

| Value | State |
|---|---|
| `0x80` | Compressor on |
| `0x00` | Compressor off |

Clean, consistent across all five toggles. This one was easy, and it was easy
because of an accident: during the **fan** walk, byte 10 was noticed sitting at
`80` earlier in the evening with the A/C on and `00` later with it off. That
made it the first candidate for the A/C walk, which then confirmed it in five
steps.

Incidental observations from unrelated walks are a real source of leads. They
are not evidence — a byte that differed between two moments hours apart could be
anything — but they are an excellent way to choose what to walk next.

Bytes 21–22 read **1600–2425 as a u16 with the A/C on, and 0 with it off**,
which is a very plausible compressor rpm. It is labelled **tentative**, and it
should be: nothing external confirmed those numbers are rpm rather than some
other scaled quantity. Bytes 23–26 hold two more words that scale with it, and
are marked unresolved rather than guessed at.

---

## Heater demand — group 10 byte 36

**Tentative.** From the setpoint walk: byte 36 reads **0 at 60–65 °F** and rises
from **3 to 40** as the PTC heater works at higher setpoints. Bytes 29 and 31
light up alongside it (`18 00 03` at 90 °F) and are unresolved — plausible
current or kW figures, never checked against anything.

Why is a byte that tracks the heater so cleanly still tentative? Because
"correlates with heating demand" is not a decode. The units are unknown, the
range is unknown, and 40 might be the maximum or might be a third of it. It is
useful enough to display as a level and not solid enough to call a measurement.

The same walk incidentally explained a puzzle: bytes 38/39 jumped at the
lower+defrost vent position and stayed jumped. That is not a vent-mode signal —
it is **defrost forcing the A/C compressor on**, which the compressor bit
confirmed. A byte that changes when you change a control is not necessarily a
byte about that control.

---

## The ÷1024 story

This is the most important example in this guide, and the reason for the whole
chapter.

### February

Pack current was decoded from the battery controller's group 05, bytes 22–23, as
a signed 16-bit value scaled **× 0.001 A**.

It was checked. Not carelessly — a deliberate load-step test was run. The cabin
heater was switched on and off while the decoded power was watched, and:

> Confirmed by heater ON/OFF test: 3.2 kW draw matches dash display of 3–4 kW.

That is a real external cross-check against an independent instrument (the car's
own dash energy display), and it passed. The value tracked load, appeared and
vanished with the heater, had the right sign, and landed inside the range the
car itself reported. On that basis it went into the dashboard and stayed there
for six months.

### August

While decoding group 01, two more current sensors turned up: bytes 0–3 and 6–9,
each a signed 32-bit value, scaled **÷ 1024 A**. Now there were two independent
measurements of the same physical quantity, from two different diagnostic groups
in the same ECU.

They disagreed. And when group 05's bytes 22–23 were re-scaled ÷ 1024 instead of
× 0.001, the disagreement went away: the three sources then **agreed within
0.05 A under load**.

The correction is recorded plainly:

> Group 05 current scale changed from ×0.001 to **÷1024** (matches group-01
> sensors within 0.05 A; 2.4 % difference from the old scale).

### The size of the error

`1/1024 = 0.0009766`. Against `0.001`, that is a **2.4 % difference**.

Sit with that for a moment.

- At 3.2 kW, the old scale was off by about 75 W. The dash reads in whole kW.
- The sign was right.
- The magnitude was right.
- The response to load was right, proportionally.
- Every plausibility bound you could construct was satisfied.
- The heater test passed. It would still pass. It would pass at any scale
  between about 0.0009 and 0.0011.

**A 2.4 % error is undetectable by plausibility.** Not "hard to detect" —
undetectable, because the check you would run has a resolution far coarser than
the error. And nothing in the system was wrong in any visible way for six
months.

What caught it was not thinking harder. What caught it was **a second sensor
measuring the same thing**. That is the whole mechanism.

### The general rule

> For scaled values, **plausibility is not verification**. You need either an
> independent measurement of the same quantity, or a known external reference.

Corollaries worth internalising:

- **Prefer powers of two.** Automotive ECUs deal in fixed-point binary. `÷1024`,
  `÷256`, `÷100`, `÷10`, `×0.5` are all common; `×0.001` on a raw ADC-derived
  register is comparatively unusual. If your fitted scale is suspiciously close
  to a power of two, it probably *is* that power of two, and the difference is
  your error rather than the ECU's. This is a heuristic that generates
  hypotheses, not a proof.
- **A cross-check that passes tells you the error is smaller than the check's
  resolution, and nothing more.** Write down the resolution. "Matches the dash,
  which reads in whole kW" is an honest statement of a ±0.5 kW bound. "Confirmed
  against the dash" is not.
- **Two decodes of one quantity are worth far more than two decodes of two
  quantities**, when you are establishing scales. Go looking for redundancy on
  purpose.

---

## Cross-checking, done properly

Three techniques the project actually used.

### 1. Sum the parts and compare to the whole

Group 01 bytes 18–19 are a u16 that, divided by 100, gives **384.26 V**.

Group 02 returns all 96 cell pair voltages individually, in millivolts. Summing
them gives **384.3 V**.

Two entirely different diagnostic groups, two entirely different encodings, one
physical quantity, agreeing to within 0.1 V — about 0.01 %. That is a real
verification of the ÷100 scale, and it is why `docs/SIGNALS.md` marks the pack
voltage row verified while marking neighbouring bytes in the same group
"unknown, static".

This pattern generalises well: whenever an ECU reports both a total and its
components, you have a free cross-check waiting.

### 2. Apply a known load and look for it

The Session 4 method. Switch on something whose power draw you roughly know, and
check that the decoded value moves by roughly that much:

- Heater on → about **3.2 kW** appears in the decoded power.
- The car's own dash energy display reads **3–4 kW**.
- Heater off → it goes away. Idle "other systems" load sits around **200 W**.

Good for catching order-of-magnitude and sign errors. Useless for catching 2.4 %,
as established above. Both facts are true at once and you should know which one
your test is buying you.

### 3. Know your instrument's noise floor before you trust a small number

With the car in READY, A/C off, all three current sources were logged at what
should have been zero:

- Group 01 sensor 1: jumped −2.0 / +0.2 / +1.4 A (coarse, roughly ±2 A steps).
- Group 01 sensor 2: sat between −0.15 and +0.36 A.
- Group 05: read −0.96 A, then +0.31 A, with its own discharge flag flipping to
  "not discharging" as the sign changed.

**All three wander about ±0.5 A around zero, and the BMS's own discharge flag
follows the noise.** Under a real load, sensor 2 and group 05 agree within
0.05 A.

The consequences shaped the product rather than being hidden inside it: the
reader fuses sensor 2 with a learned offset to group 05, there is a per-car
zero-current calibration you run with the car on but not READY, positive
readings are clamped while the BMS reports discharging, and the dashboard paints
anything under 0.6 A or 250 W as plain grey IDLE rather than pretending to
resolve it.

Displaying "−0.31 kW" when your noise floor is ±0.5 A is not precision. It is
decoration.

---

## Temperatures: emit both units, always

Group 04 gives four pack temperatures as (u16 raw, s8 °C) pairs. Group 10's
first four bytes give ambient, cabin, evaporator and sunload as `raw − 40 °C`
(tentative; the sample read 33 °C ambient, 21 °C cabin, 1 °C evaporator on a
warm evening with the A/C running, which is coherent but has not been walked).

The project convention, adopted 2026-08-24 and enforced by a test per vehicle
profile:

> **Every temperature decoder emits both `*_c` and `*_f`.**

Not a formatting preference. Storing one unit and converting at the display
layer means the conversion lives in several places and one of them will
eventually be wrong. Emitting both from the decoder makes the decoder the single
place a temperature is ever computed.

The Lancer profile carries a matching warning worth repeating here, because it
is the same class of error as the fan-speed one: its standard `0146` ambient PID
reads about **61 °C at idle**. That is engine-bay heat soak, not weather. The
PID is correctly decoded per SAE J1979 and the number is still not the thing its
name suggests. **A correct decode of a badly named sensor is still a wrong
answer to the user's question.**

---

## Try it on your own car

For any continuous or scaled value:

1. **Walk it like a discrete input first.** Even a continuous control has
   positions you can hold. Get the byte identified before you worry about the
   scale.

2. **Plot raw against physical.** Seven fan positions, seven setpoint steps,
   whatever you have. Look at the shape before fitting anything: is it linear,
   does it saturate, does it repeat? The fan's `4 5 6 8 9 11 11` said "this is
   not an index" at a glance.

3. **Check the down-walk residual.** If the same physical setting gives a
   slightly different raw value depending on direction, you are looking at a
   controlled target, not a stored setting. Say so.

4. **Look for a power of two before you look for a round decimal.** Fit your
   scale, then ask whether 1/1024, 1/512, 1/256, 1/100 or 1/2 is within a few
   percent. If one is, prefer it and go find evidence.

5. **Find a second source for the same quantity, and make that your real test.**
   Another diagnostic group, another ECU, a total-versus-sum relationship, a
   standard OBD PID if your car has one. This is the only thing that catches
   small scale errors. Budget time for it.

6. **Write down the resolution of every cross-check you run.** "Agrees with the
   dash, which displays whole kW" bounds your error at ±0.5 kW and no tighter.
   Then decide whether that is good enough for what you are going to claim.

7. **Measure your noise floor at a known zero** before you display small values.
   Park it, switch the load off, and log for a few minutes. Whatever spread you
   see is the smallest number you are entitled to show.

8. **Grade what you end up with**, using [chapter
   7](07-confidence-and-honesty.md)'s tiers. "Correlates cleanly with the
   control" is genuinely useful and is genuinely not the same as "verified".

Next: [UDS and ISO-TP](05-uds-and-isotp.md), which is how you get at the
multi-frame diagnostic data this chapter has been quietly assuming you have.
