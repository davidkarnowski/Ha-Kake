# 2. The method

**What this chapter teaches**

- Differential capture: change exactly one physical thing and diff the bytes.
- Why you must walk the input **back down**, and the fan walk that proves it.
- The scoring heuristic in `calibrate_input.py`, term by term, and what each
  term buys you.
- The mechanics of a walk: pausing the dashboard, settling, sampling, writing
  partial results after every step.
- Why a documented negative result is real output and belongs in the docs.

**What this chapter assumes:** you can get filtered frames off the bus and you
know the `ATCAF0` rule from [chapter 1](01-getting-on-the-bus.md).

---

## The core idea

You have a CAN ID. It is broadcasting eight bytes, several times a second,
forever. You have no documentation. Most of those bytes are counters, checksums,
or signals for systems you do not care about.

The only reliable way to find out which byte means what is this:

> Capture the ID while you change **exactly one** physical thing, and diff.

Everything else in this chapter is a refinement of that sentence, mostly aimed
at the ways it quietly fails.

Two consequences follow immediately, and both are stated in
`docs/ADDING_SIGNALS.md`:

- **A capture where nothing changed proves nothing.** You cannot decode a signal
  from a static dump. You can guess, and the guess may even be right, but you
  have not verified anything and you must not label it as though you had.
- **One thing at a time.** If you open the driver door and the passenger door
  together and two bits change, you have learned that two bits are involved in
  doors. You have not learned which is which, and you will have to do it again.

---

## Walk it up, and walk it back down

This is the discipline that separates a decode from a coincidence, and the
reason for it is best told through the walk that discovered it.

On 2026-08-24 at 22:14 the fan on the Leaf's climate control was stepped from
speed 1 up to 7 and back down to 1, while the HVAC amplifier's diagnostic group
10 was polled at every step. Thirteen steps, four samples each.

Byte 11 followed the fan exactly. But it was not the only byte that appeared to.
**Bytes 2, 7, 12 and 25 also "moved with" the fan on the way up.** Every one of
them rose as the fan speed rose. On a rising-only walk, all five bytes are
candidates and you have no way to rank them.

On the way back down, bytes 2, 7, 12 and 25 kept going the same direction. They
were temperatures, drifting upward with time because the A/C was off. Byte 11
retraced its values exactly:
`84 85 86 88 89 8B 8B` going up, and the same values coming down.

That is the whole argument. **A rising walk cannot distinguish a response from a
drift, because on a rising walk they look identical.** Coming back down
separates them: the response returns, the drift does not.

Every preset in `calibrate_input.py` walks up and back down, or toggles a switch
at least twice in each direction, for exactly this reason. Look at the shapes:

- `fan` — 1 → 7 → 1 (13 steps)
- `setpoint` — 60 °F → 90 °F → 60 °F in 5-degree steps (13 steps)
- `ac` — off, on, off, on, off (5 steps)
- `doors` — baseline, then open-and-shut each door alone, then baseline again
- `mode` — the four vent positions, twice round

The `mode` preset is a cycle rather than a ramp, so it cannot be walked
backwards; it goes round twice instead, which gives the same property: the same
label appears more than once at different points in time.

---

## The scoring heuristic

After a walk, `analyse()` in `calibrate_input.py` scores every byte that changed
at all and ranks them. The formula is four terms:

```python
score = (3 if consistent else 0) \
      + (2 if stable else 0) \
      + (2 if mono else 0) \
      + min(distinct, 8) / 8.0
```

Bytes that never changed across the whole walk are dropped before scoring. The
top 14 are printed with flag letters `C`, `S`, `M` and a count of distinct
values.

The weights are deliberate, and each term kills a specific kind of false
positive.

### `consistent` (3 points) — kills time drift

For each step label, collect the value seen at that label. If a label appears
more than once and the values differ, the byte is not consistent.

```python
by_label = {}
consistent = True
for lab, v in zip(labels, vals):
    if lab in by_label and by_label[lab] != v:
        consistent = False
    by_label[lab] = v
```

This term is only meaningful **because the walk comes back down**. On a walk
where every label appears once, every byte is trivially consistent and the term
is worth 3 points to everybody, which is to say worth nothing. On the fan walk,
"fan 3" appears twice, twenty-odd seconds apart, and the drifting temperature
bytes had moved in between. Byte 11 had not. It scores the highest weight
because it is the strongest evidence available.

### `stable` (2 points) — kills rolling counters

Within a single step, four samples are taken about 0.4 s apart. If a byte holds
the same value across all four, it is stable.

```python
col = [s[i] for s in samples if len(s) > i]
if len(set(col)) > 1:
    stable = False
```

This is the counter filter, and the project has a scar to show for it. In
February, `0x354` byte 5 was scanned as a gear-position candidate and *initially
looked like gear data*. It changed when you shifted, it took a small number of
discrete values, and it looked exactly like the thing being hunted.

It was a **2-bit rolling counter cycling 00 → 08 → 10 → 18**, continuously,
whether or not anything was shifted. It changed when you shifted because it
changed all the time.

A single sample per step cannot tell those apart. Four samples in a two-second
window can: a counter cycling several times a second will show at least two
distinct values inside one step, and lose the stability points immediately.
`0x174` byte 4 is another one — a rolling counter sitting right next to the
genuine gear byte.

### `mono` (2 points) — rewards ordered controls

Take the rising half of the walk and ask whether the values are non-decreasing
or non-increasing.

```python
half = vals[: max(2, len(vals) // 2 + 1)]
mono = all(a <= b for a, b in zip(half, half[1:])) \
    or all(a >= b for a, b in zip(half, half[1:]))
```

Fan speed, temperature setpoint and pedal position are ordered controls, and the
byte that encodes them is very likely to be ordered too. This term surfaces
them. It is worth noting what it does *not* do: a drifting temperature is also
monotonic on the rising half, so this term alone is worthless. It only earns its
keep alongside `consistent`.

For an unordered control (vent mode, gear) monotonicity is meaningless and the
correct byte simply forfeits these 2 points. That is fine — it still beats a
noisy byte on the other three terms.

### `distinct` (up to 1 point) — a tiebreaker

`min(distinct, 8) / 8.0`, so it can never outweigh a structural term. Its job is
to sort the bytes that tie on the flags. A byte that takes seven distinct values
across a seven-position fan control is more interesting than one that flips
between two.

### Reading the output

```
step:      fan 1   fan 2   fan 3   fan 4   fan 5   fan 6   fan 7   fan 6 ...
2110 b11     132     133     134     136     137     139     139     139 ...   score 7.8 [CSM] 6 values
```

`[CSM]` is the byte you want: consistent, stable, monotonic. `[-S-]` is
suspicious. `[--M]` on its own is very often a drift.

And if the report is empty, the tool says so plainly:

```
nothing changed across the walk — check the target / that the control really moved
```

which is a real outcome and is discussed below.

---

## The mechanics of a walk

The loop in `calibrate_input.py` is not complicated, but several of its details
were added after something went wrong, and they are worth copying.

**Pause the dashboard first.** The reader process owns the adapter. The walker
writes `web/reader.pause`, waits up to 15 s for the reader's state file to
report `paused`, and only then connects. On exit — including Ctrl-C, including
an exception — it removes the pause file so the dashboard comes back. If you are
doing this by hand rather than with the walker:

```bash
touch web/reader.pause      # wait a few seconds for the reader to let go
# ... capture ...
rm web/reader.pause
```

**Print the instruction and wait for a human.** Each step prints what to do and
blocks on Enter. There is an `--auto N` mode that advances every N seconds
instead, for controls you cannot operate and press Enter for at the same time.

**Settle before sampling.** `--settle`, default 1.5 s. A blower motor takes a
moment to reach its new speed, and an ECU takes a moment to report it. Sampling
into the transient gives you a value that belongs to no step.

**Take several samples per step.** `--samples`, default 4, `--gap` 0.4 s apart.
This is what makes the `stable` term possible. Fewer than three and you cannot
see a counter; many more and the walk becomes tedious enough that you stop doing
it properly.

**Diff live against the previous step.** After each step the tool prints the
current bytes and what changed:

```
    2110: 49 3D 29 02 49 3D 00 29 02 00 80 85 61 …   changed: b11:84→85
```

This is not decoration. Seeing `changed: nothing` on the step where you flipped
the switch tells you immediately that you are on the wrong ECU, or that the
control is not exposed there, and you can abandon a twelve-step walk after step
two instead of after step twelve.

**Write partial results after every step.** `write(False)` is called at the
bottom of every iteration, via a temp file and `os.replace` so the JSON is never
half-written. If the adapter drops, or the car goes to sleep, or you fumble the
Ctrl-C, the steps you already did survive:

```
aborted after 7 of 13 steps — partial capture kept: tests/fixtures/walk_fan_20260824_221450.json
```

Re-walking is not just slow, it is
physically annoying — you are sitting in a hot car pressing buttons — and
anything that makes you reluctant to re-walk makes you likely to accept weaker
evidence.

**Name fixtures deterministically.** Output lands in
`tests/fixtures/walk_<preset>_<YYYYMMDD>_<HHMMSS>.json`, carrying the preset,
the target, the step labels, every sample, and a `complete` flag. The existing
walks are all there:

```
tests/fixtures/walk_fan_20260824_221450.json
tests/fixtures/walk_ac_20260824_222922.json
tests/fixtures/walk_setpoint_20260824_224401.json
tests/fixtures/walk_doors_20260825_110050.json
tests/fixtures/walk_locks_20260825_110510.json
tests/fixtures/walk_lights_20260825_112526.json
```

Those files are then fed to the test suite, so every decode is re-checked
against the actual capture on every commit. That is [chapter
8](08-from-signal-to-tile.md).

### Running one

```bash
./venv/bin/python calibrate_input.py fan
./venv/bin/python calibrate_input.py doors locks
./venv/bin/python calibrate_input.py custom --steps "off,low,high,low,off" --target lbc
./venv/bin/python calibrate_input.py setpoint --from auto     # resume a chain after an abort
```

Presets chain: `all` runs the HVAC sequence, `body` runs `doors` then `locks`,
and `--from` restarts a chain partway through.

---

## Negatives are results

Three separate walks on this car found nothing at all, and all three are
documented.

The **vent mode** walk stepped the four-position mode control twice round.
The **AUTO** walk toggled AUTO on and off five times. The **fresh/recirc**
intake walks ran twice each, once from each starting state. In all of them,
nothing moved anywhere in HVAC groups 10, 11 or 01. A full service-21 sweep
established that only groups 00, 01, 10, 11, 82 and 83 answer at all, so group
00 was added to the walker as the last candidate. **It stayed `80 01 80 00`
through every step of every walk.**

The conclusion is not "we failed to decode vent mode". The conclusion is:

> **Not readable from this ECU (walked, nothing moved in 00/01/10/11):** vent
> mode (4-position cycle, twice), AUTO, fresh/recirc. OVMS reads those from
> EV-CAN `0x54B` (fan, vent mode, intake) — needs the re-pinned cable.

That is in `docs/SIGNALS.md`, and the group-00 constant is **pinned by a test**,
so if someone later claims to read the intake door from the HVAC amplifier, the
suite disagrees with them.

A documented negative is worth writing down because it is expensive to produce
and it stops the next person spending the same evening. It has to be specific to
be worth anything: "we walked the four-position mode control twice round and
groups 00/01/10/11 did not move" is useful. "Vent mode doesn't work" is not.

One honest caveat on the negatives above: they establish that the signal is not
in the groups that were walked, on this ECU, on this car. They do not establish
that it is nowhere. The SIGNALS.md wording says exactly that much and no more.

---

## A note on the tool's portability

`calibrate_input.py` is currently Leaf-shaped in two places: the `TARGETS` dict
hardcodes the Leaf's UDS addresses (`744`/`764` for the HVAC amp, `79B`/`7BB`
for the battery controller) and the passive ID groups, and `PRESETS` describes
this car's specific button set including its four-position vent control.

The walk loop, the sampling, the live diff, the partial writes and `analyse()`
have no vehicle knowledge at all. Generalising the targets and presets the way
`vehicles/` generalised the reader is planned work, not done work. Until then,
adapting it to another car means editing two dicts at the top of the file,
which is a smaller job than it sounds.

---

## Try it on your own car

Pick any control with discrete positions. A fan knob, a headlight stalk, a
window switch, a gear selector.

1. **Choose your search space.** From your unfiltered sweep, pick a handful of
   CAN IDs that plausibly relate. Body controls tend to cluster; on this car
   `0x60D` carries doors, locks and lights together. If you have no idea, start
   with the IDs that broadcast at a low rate — 10 Hz status frames are more
   likely to be body state than a 100 Hz frame, which is more likely to be
   powertrain.

2. **Write down the step list before you start**, and make it a palindrome.
   `off, low, med, high, med, low, off`. If the control is a cycle rather than a
   ramp, go round it twice.

3. **Baseline first and baseline last.** The first and last steps should be the
   same state. If the last capture does not match the first, something drifted
   and you should be suspicious of everything in between.

4. **Four samples per step, roughly half a second apart, after a second or two
   of settling.** Nothing here is magic; the point is more than one sample so
   you can see counters, and a pause so you are not sampling a transient.

5. **Score the bytes.** Even by hand this is quick: drop bytes that never
   changed, drop bytes that varied within a step, then look for ones where the
   same label gives the same value. What is left is usually one byte.

6. **Look at the runners-up too.** On the lights walk, the byte that won was on
   `0x60D` — but `0x625` byte 1 also tracked the switch perfectly, as a
   different encoding of the same information. Knowing a signal is mirrored
   elsewhere is useful even when you do not use the mirror. See
   [chapter 3](03-walking-discrete-inputs.md).

7. **If nothing moved, write that down with the exact scope of what you tried**,
   and go looking on a different bus or a different ECU before you conclude the
   signal does not exist.

Next: [walking discrete inputs](03-walking-discrete-inputs.md), where this
method is applied to doors, locks, lights, turn signals and gear, byte by byte.
