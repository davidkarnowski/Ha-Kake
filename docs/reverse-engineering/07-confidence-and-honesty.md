# 07 — Confidence and honesty

**What this chapter teaches**

- The three confidence tiers this project grades every signal with, and the evidence each one demands.
- Why demoting a label is a healthy, normal event rather than an embarrassment.
- That negative results are results, and how to keep one from silently rotting.
- How to use community data without laundering a hypothesis into a fact.
- Why documenting what you *cannot* reach is part of the map.
- The mechanical habits that keep the labels honest when nobody is watching.

**What this chapter assumes:** you have decoded at least one thing and are about to write it down.

---

## Three tiers

`docs/SIGNALS.md` opens with its own definitions, and they are short enough to quote whole:

> **Verified** = observed changing with the physical input on this car.
> **Static** = decoded from a static capture whose value passed an external check (matches the dash, the LBC, or a plausibility bound) but has not been observed changing.
> **Tentative** = from community documentation or an unchecked sample.

That is the entire epistemology. What matters is that the three tiers are distinguished by **what evidence exists**, not by how confident the author feels.

| Tier | Evidence required | Example from this project |
|---|---|---|
| Verified | You changed the physical input and watched the bytes follow, ideally in both directions | `0x421` byte 0 gear: all five of P/R/N/D/Eco observed live while shifting, 2026-08-24 |
| Static | One capture, plus an independent check that the value is right | `0x5C5` odometer: matches the dash at 65,545 mi. Nobody has driven the car far enough to watch it increment |
| Tentative | Somebody else's document, or a sample nobody stressed | `0x5A9` range: `(u16 >> 4) ÷ 5` km, from OVMS, never checked against the dash |

The "Static" tier did not exist at first. It was created during a pre-release audit, when it became clear that TPMS, odometer, handbrake, units and dash-SOH were all wearing "verified" labels on the strength of a single plausible-looking capture. They are almost certainly right. They were not *verified*, because nothing had been observed changing. Inventing a tier was cheaper than either lying or discarding good work.

**If your two labels do not fit your evidence, you need a third label, not a rounder number.**

---

## Demotion is normal and healthy

The turn-signal decode on `0x358` byte 2 is the model case. It was recorded as:

```
80 = off   82 = left   84 = right   86 = hazards
```

Off, left and right were verified in February 2026 by toggling the stalk and watching the byte change in a 605-frame capture. `86` for hazards came from community documentation. The row wore a single "verified" label covering all four values.

A pre-release truth audit in August caught it: the only capture on this car holds off, left and right. Nobody had ever pressed the hazard button while recording. The row now reads:

> left/right **verified** (2026-02 capture); `86` hazards tentative — community value, never captured on this car

The value did not change. The claim about the value did. That took one line of editing and it means the next person knows exactly which experiment is still open — and it is an easy one, five seconds with a thumb on the triangle button.

That audit was not an isolated act of conscience. It was two deliberate passes over the whole documentation set before going public, recorded in `WORKLOG.md`:

- **Part 1** compared README, ARCHITECTURE, ADDING_SIGNALS and the legacy README against the code. Seventeen drifts, six of them blockers. A whole dashboard tile (Body) was missing from two documents. A test count was stale. A refresh interval said 10 s where the code said 3 s. A signal count said 38 where the registry had 57.
- **Part 2** went through the byte tables in `docs/SIGNALS.md`, decoding them through the real decoders and fixtures, and through the policy documents. Two blockers. `SECURITY.md` and README both claimed *every* command the project sends is a service-`0x21` read, which is true of the reader but not of the probe tools (`0x22`, `0x1A`, mode `09`) or one legacy script (`0x10` session control). Both documents now say exactly that. The hazards demotion above came out of this pass. So did the new Static tier, a demotion of `0x625` to "observed, not decoded", and the renaming of a table headed "ECUs that do not answer" which contained ECUs that answer.

Note the direction of every single correction: toward less confidence, more specificity, fewer implied capabilities. Audits that only ever find things to boast about are not audits.

---

## Negatives are documented results

The HVAC walks produced several signals and several nothings. The nothings are in the documentation with equal weight:

> **Not readable from this ECU (walked, nothing moved in 00/01/10/11):** vent mode (4-position cycle, twice), AUTO, fresh/recirc. OVMS reads those from EV-CAN `0x54B` (fan, vent mode, intake) — needs the re-pinned cable.

Behind that sentence: the fresh/recirc walk moved nothing anywhere in groups 10, 11 and 01. The vent mode walk cycled the four positions twice and moved nothing. The AUTO walk moved nothing. When a full service-21 sweep turned up group `00` as a previously-unknown answering group, it was added to the walker as the last remaining candidate for door and mode flags — and it stayed at `80 01 80 00` through everything.

That negative is now **pinned by a test** (WORKLOG entry 63). Group 00's constancy across those walks is asserted against the fixtures, so if someone later writes a decoder that claims to read vent mode out of group 00, the suite objects.

This is the part most projects skip. A negative result that lives only in someone's memory decays into "I think we tried that once". A negative result with a fixture and a test is a fact with a maintenance contract. It also saves the next person from the identical afternoon: if you have walked vent mode twice and it moved nothing, say so, so nobody walks it a third time.

---

## Community values are hypotheses, not evidence

This project owes real debts, and `docs/SIGNALS.md` credits them:

- Open Vehicle Monitoring System, `vehicle_nissanleaf.cpp` — CAN ID map and scalings
- dalathegreat, `leaf_can_bus_messages` — a DBC collection

Those sources are good. They are also somebody else's car, somebody else's model year, and in the DBC case a file with no attached provenance for any individual row. Used correctly they are enormously valuable: they turn "which of 2048 CAN IDs should I look at" into "check these six". Used incorrectly they launder a stranger's guess into your fact.

The rule this project follows: **a community value enters as tentative and is promoted only by observation on this car.**

Gear on `0x421` is the model case, and it is worth following end to end:

1. `0x421` byte 0 came from a community DBC with a full value map. WORKLOG entry 44 records it as `0x08` = P confirmed by observation, and `10/18/20/38` = R/N/D/Eco "from community DBC", marked **⚠ verify by shifting**.
2. Entry 49, later the same evening: P = `08`, R = `10`, D = `20` observed live, with `0x174` byte 3 moving in lockstep. N = `18` and Eco = `38` still "expected from the DBC, not yet observed".
3. Entry 52: a second capture walked P→R→N→D→Eco→P and confirmed all five. Only then did `docs/SIGNALS.md` get to say **verified (all five, 2026-08-24)**.

Three log entries, one row promoted. The DBC was right the whole time. That is not the point. The point is that at step 1 nobody *knew* it was right, and the label said so.

The counter-example is in the same document. Range on `0x5A9` uses `÷5` for kilometres, taken from OVMS, and is still marked **tentative — scale unconfirmed**. It has never been checked against the dash's own range display, which would take about ten seconds. Until somebody does it, it stays tentative. That row is a small standing invitation.

---

## Say what you cannot reach

A map that omits the edges is not honest about being a map.

`docs/SIGNALS.md` carries an entire section, "Not reachable without EV-CAN", explaining that the Leaf's EV-CAN is on OBD pins 13/12 rather than the 6/14 every ELM327 uses, confirmed against the OVMS ZE0 cable pinout and the sethfischer Leaf OBD manual. It then lists, by CAN ID, everything that a re-pinned cable would expose and this project currently cannot see: `0x1DB` pack voltage and current at 10 ms, `0x1DA` motor torque and RPM, `0x55B` SOC, `0x5BC` GIDs, `0x54C` ambient, `0x54F` cabin, and more.

Similarly, `docs/ADDING_SIGNALS.md` puts the boundary in its very first decision table, as a legitimate outcome:

> only on EV-CAN (`0x1DB`, `0x54F` …) → **not reachable** without a re-pinned cable — stop here

And the "Other ECU probes" table records that the VCM returns NRC `0x80` to everything and is **parked**, that the inverter and steering ECUs gave no response at all, and that ABS, BCM and EPS answer group 01 but have not been decoded.

Every one of those lines does work. It tells a reader which questions are answered, which are open, and which need different hardware before they can even be asked. "We don't know" is information. "We tried and here is precisely how far we got" is better information.

---

## The discipline, in practice

Labels stay honest because of habits, not intentions. This project's are small and mechanical:

| Habit | Where it is written down |
|---|---|
| A decoder change without a `docs/SIGNALS.md` change in the same commit is incomplete | `CLAUDE.md` |
| A new decode needs a fixture captured *while the input was changing*, plus a test asserting the value you saw with your own eyes | `docs/ADDING_SIGNALS.md`, steps 1 and 3 |
| "A capture where nothing changed proves nothing" | `docs/ADDING_SIGNALS.md`, step 1 |
| The signal row records the tier, the sample you saw, and the date | `docs/ADDING_SIGNALS.md`, step 6 |
| `WORKLOG.md` is append-only. Never edit old entries | `CLAUDE.md` |

The append-only rule is the one that does the most quiet work, because it makes being wrong survivable. The February "may be 29-bit" guess in chapter [06](06-when-youre-wrong.md) is still sitting there in the log, unedited, with entry 43 six months later explaining that it was `ATCAF1` all along. If the log were editable, the tidy thing to do would be to go back and fix it, and the record of *how the mistake happened* would be gone.

The log even carries a correction about itself. The last line of `WORKLOG.md`:

> Correction to the entry above: the suite is 89 tests, not 90 (87 + the two DTC tests). The commit message for f85bad5 repeats the miscount; the code and fixtures are as described.

A miscounted test total is not important. Publishing a correction to it, in a document nobody was auditing, over a number nobody would ever have checked, is the habit that makes the rest of the labels believable. Honesty is not something you switch on for the important claims.

---

## The general principle

Your confidence label is a promise. It is a promise to whoever reads your work, and it is a promise to yourself in six months, when you have forgotten everything except what you wrote down.

Break it once — mark something verified because it looked obvious, or because the community file said so, or because you did not want the table to have a weak row in it — and you have not just made one row unreliable. You have made every row require independent re-checking, because a reader can no longer tell your verified rows from your hopeful ones. One laundered guess devalues the whole document.

Keep it honest and the opposite happens. The tiers compound. A reader who trusts your Verified rows can build on them, and can spend their own limited time on the Tentative ones, which is exactly where you wanted their help.

**Two rules that cover most of it:**

1. Label by the evidence you have, never by how sure you feel.
2. Make demotion cheap and unembarrassing, so it actually happens.

---

## Apply this

- **Write the tier next to every claim**, in the same commit as the code that acts on the claim. Not later, not in a separate pass.
- **Record the sample and the date.** "33 °C / 91 °F, 2026-08-24" is checkable. "looks right" is not.
- **Log negatives with the same ceremony as positives.** Which input you moved, how many times, and where you looked. Then pin it with a test if you can.
- **Keep community data in its own tier** until you have moved the physical thing yourself. Credit the source either way.
- **Publish your boundary.** What you cannot reach, what refused to answer, and what hardware would change that.
- **Never edit the log.** Append the correction underneath, and leave the mistake where the next person can learn from it.
- **Audit before you publish.** Read every claim against the code, and expect the corrections to run toward less confidence. If none of them do, you did not audit.

Next: [08 — from signal to tile](08-from-signal-to-tile.md), where a labelled, tested decode becomes something on a screen.
