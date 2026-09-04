# 06 — When you're wrong

**What this chapter teaches**

- Six real wrong hypotheses from this project (§§1–6): the symptom, why the wrong answer was plausible, what it cost, and what finally killed it — plus a seventh case (§7) that is not a mistake at all but a limit of the bus.
- Why a traceback tells you where a program died, not what killed it.
- How a misdiagnosis gets preserved in your own notes and outlives the bug.
- The difference between a fix that treats a symptom and a fix that addresses a cause, and why the symptom fixes were still worth keeping.
- Two failure modes that are not bugs at all: a correct decode of the wrong physical quantity, and two states that are genuinely indistinguishable on the bus.

**What this chapter assumes:** you have read [the method](02-the-method.md), and ideally [03](03-walking-discrete-inputs.md) and [04](04-analog-and-scaled.md), because several of these mistakes are the method failing.

---

Reverse engineering is mostly being wrong in an organised way. The interesting question is never whether you were wrong, it is how long the wrong answer survived and what eventually killed it. Each section below is one hypothesis from `WORKLOG.md`, told in that shape.

---

## 1. The rolling counter that looked like a gear

**Symptom.** February 2026, hunting for gear position. `gear_probe.py` scanned ten candidate CAN IDs. Byte 5 of `0x354` was changing in a small, tidy set of values: `00` → `08` → `10` → `18`.

**Hypothesis.** That's a gear indicator. Four distinct values, evenly spaced, in the right ballpark for P/R/N/D.

**Why it was plausible.** It looks exactly like an enumeration. Discrete, small, regularly spaced, no noise. If you saw that byte in a DBC file with no label you would guess "gear" too.

**Cost.** Small — one probe session, because the very next step was the right one.

**Actual cause.** It is a 2-bit rolling counter, and it cycles continuously whether or not anything in the car moves. `WORKLOG.md` records it twice, first as a discovery and then again in the CAN ID table as a warning: "Byte 5 is 2-bit counter (00→08→10→18), not gear data".

**What killed it.** Holding the input still. `gear_capture.py` captured `0x174` and `0x354` in each gear, and `0x354` byte 5 kept cycling within a single gear. A gear signal must be *stable while the gear is stable*. A counter is not.

That test — does the byte hold still while the input holds still, and does it retrace the same values on the way back down — is the one the walker scores on today. `docs/ADDING_SIGNALS.md` describes `calibrate_input.py` as ranking "the bytes that follow the input (and checks the way back down)". During the fan walk (WORKLOG entry 61) it did exactly this job: bytes 2, 7, 12 and 25 all moved, but they drifted with time rather than with the fan knob, and only byte 11 retraced.

**Generalisable.** A byte that changes is not a signal. A byte that changes *with your hand* and holds still otherwise is a signal. Counters, timestamps and odometers will all pass a naive "it changed!" filter.

---

## 2. "May be 29-bit"

**Symptom.** February 2026. Several CAN IDs printed `<DATA ERROR` instead of data: `0x002`, `0x245`, `0x292`, `0x6F6`, and `0x5B3` which was wanted for state-of-health. Others on the same bus decoded fine.

**Hypothesis.** Those frames use 29-bit extended CAN identifiers, and the adapter is configured for 11-bit.

**Why it was plausible.** `DATA ERROR` is exactly what a protocol mismatch looks like, mixed 11/29-bit traffic is a real thing, and the hypothesis explains why only *some* IDs fail. It was written into the log at the time, in the CAN ID table: `002 | 7D FF 00 07 3E | Unknown | DATA ERROR (may be 29-bit)`.

**Cost.** Effectively a whole session. `0x5B3` in particular was abandoned as unreachable, which is why dash SOH did not get decoded until August.

**Actual cause.** `ATCAF1`. Auto-formatting was left on from the UDS work. With auto-formatting enabled the adapter tries to interpret every frame as ISO-TP, and a broadcast body frame is not ISO-TP, so it reports a data error. WORKLOG entry 43, six months later: "every ID that showed `<DATA ERROR` in Feb (0x5B3, 0x292, 0x002…) decodes fine with auto-formatting OFF."

**What killed it.** Retrying an old failure with a changed setting. Nothing subtle. `elm327.passive_capture()` now sets `ATCAF0` itself so it cannot happen again, and `docs/SIGNALS.md` states the rule as a transport fact rather than a footnote.

**Generalisable, and this is the important part.** The symptom was recorded faithfully. The cause was recorded as a guess. Six months later the guess read like a finding. **That is how a misdiagnosis survives** — not because anyone lied, but because a plausible cause written next to a real symptom acquires the symptom's credibility. Write "hypothesis:" in front of your hypotheses. It costs four words.

---

## 3. The segfault blamed on Bluetooth, twice

**Symptom.** The dashboard died. Hard: SIGSEGV, after about 38 readings, in a background thread. Traceback pointed at a CoreBluetooth callback.

**Hypothesis.** BLE is crashing the process. bleak and CoreBluetooth do their work on background threads, ELM327 clones are flaky, and 38 readings is about the right timescale for something in the Bluetooth stack to go wrong.

**Why it was plausible.** The traceback said so. It genuinely, honestly pointed at a background BLE callback. This was not a lazy guess; it was the only evidence available.

**Cost.** One fix, plus a second crash, plus the time to disbelieve the traceback.

**First fix (entry 42).** Move the reader out of the Flask process entirely. `web/app.py` now runs `reader.py` as a supervised subprocess with auto-restart, so "a crash costs seconds, not the dashboard." This was a good change and it is still in the code.

**It crashed again.**

**Actual cause (entry 50).** One `sqlite3` connection shared across concurrent Flask request threads. sqlite3 objects are not thread-safe in that configuration, and the failure mode is a segfault rather than an exception. The BLE callback was simply the thread that happened to be running when the process fell over.

**Real fix.** A thread-local `Store` in `web/app.py`. Fifteen concurrent `/api/history` requests now pass. The reader stayed a supervised subprocess, because that was worth having anyway.

**Generalisable.** A traceback tells you *where it died*, not *what killed it*. Memory corruption in particular surfaces at an arbitrary later point, in whichever thread touches the wrong page first. When your crash site has no plausible mechanism for the crash, stop reading the traceback and start looking for shared mutable state.

This one earned a permanent entry in the repo's `CLAUDE.md`: "Never share a sqlite3 connection across threads — it segfaults."

---

## 4. The three-act macOS sleep saga

The longest chase in the log, and a clean demonstration that a fix can be correct, useful, and still not the cause.

**Symptom.** Close the laptop lid. Open it later. The dashboard is stale forever. It never recovers.

### Act 1 — the adapter is asleep, or the link is dead, and we cannot tell (entry 73)

The reader could not distinguish "the car has gone to sleep and stopped answering" from "the BLE link is dead". Both look like no CAN data. bleak often still reported the client as connected after a wake, so `send()` just timed out silently, and the reader fell into its 60-second "asleep" heartbeat and never tried to reconnect.

Fix: `BleELM` registers a `disconnected_callback`, bounds `connect()` with a 20 s timeout, and raises `ConnectionError` rather than hanging. The reader gained `probe_alive()`, which sends `ATI` to the adapter itself.

That probe is a nice trick worth stealing. The dongle is powered from **OBD pin 16, permanent +12 V**, so it answers `ATI` even when the car is fully asleep. Therefore:

| `ATI` | CAN data | Conclusion |
|---|---|---|
| answers | none | car is asleep — heartbeat, wait |
| times out | none | BLE link is dead — reconnect |

Result: the drop was now detected. Reconnection still failed.

### Act 2 — you cannot reconnect by address after a sleep (entry 74)

Logs from a real lid-close showed the drop *was* being caught (write TimeoutError, then a reconnect attempt), but every reconnect failed with bleak's "Device with address … was not found".

Cause: macOS and CoreBluetooth invalidate a peripheral's session UUID across sleep. A connect-by-address can never succeed again, because that address no longer names anything. You have to re-discover the peripheral by scanning.

Fix: `BleELM.connect()` now always scans first, `find_device_by_address` and falling back to `find_device_by_name`, then connects to the found `BLEDevice`. Reconnect logging became timestamped and step-by-step (detecting → scanning → found → connected → configured). Backoff cap dropped from 30 s to 15 s.

Still did not recover.

### Act 3 — the process itself is poisoned (entry 75)

Verbose logs from a second lid-close proved the reconnect was scanning and finding *nothing*, for six minutes and more. Not a timing problem. Not a backoff problem.

Actual root cause: macOS leaves the *process's* CoreBluetooth central manager stalled across sleep. No scan in that process will ever see the adapter again. Creating a fresh `BleakScanner` does not help, because the stall is at process level, not object level.

The only cure is a new process. So:

```python
MAX_DETECT_ATTEMPTS = 1
```

The reader exits after its first failed reconnect and `web/app.py`'s subprocess supervisor relaunches it. A brand-new process gets a clean CoreBluetooth and scans normally. If the adapter really is there — an awake blip, a momentary drop — the first attempt's scan finds it and reconnects in-process with no restart. Only a scan-finds-nothing failure triggers the relaunch. Scan timeouts were trimmed to 8 s and 6 s.

**Recovery went from never to about 20 seconds after wake.**

**Generalisable.** Three acts, three fixes, one cause. Acts 1 and 2 were real improvements that made the system observable enough to find act 3 — without act 1 the drop was invisible, and without act 2's step-by-step logging nobody would have known the scan was returning empty. But they did not fix the bug, and it is worth being honest with yourself about which of your fixes are which. Note also that the subprocess supervisor from mistake 3 above, built for the wrong reason, turned out to be the mechanism that made the right fix possible.

---

## 5. The spec-correct wrong measurement

**Symptom.** The Lancer's PID `0146`, "ambient air temperature", reads about 61 °C at idle.

**Hypothesis.** The decode is wrong.

**Why it was plausible.** 61 °C is not a plausible outdoor temperature anywhere people park cars, so something must be off — a scale factor, an offset, a signed/unsigned confusion.

**Actual cause.** Nothing. The decode is `raw − 40 °C`, exactly per SAE J1979, and it is right. The sensor is in the engine bay of a stationary running car and is reading heat soak. The number is a correct measurement of a quantity that is not the one the PID's name suggests.

`docs/SIGNALS.md` says so in the table rather than quietly dropping the row:

> `0146` Ambient temp — raw − 40 °C — ~61 °C at idle — **engine-bay heat soak, not weather; don't trust as outdoor temp on a stationary car**

**Generalisable.** Decoding correctly and measuring the right thing are separate problems, and passing a plausibility check does not prove you have solved either. When a correct decode gives an implausible value, the sensor's physical location is a better suspect than your arithmetic. And the honest fix is a caveat on the row, not a deletion.

---

## 6. The 2.4 % scale error

Chapter [04](04-analog-and-scaled.md) owns the details; it belongs in this chapter as a category.

Group 05's pack current was decoded as `×0.001 A`. It is `÷1024 A`. Those differ by 2.4 %, so the wrong scale produced readings that were plausible in every way: right sign, right order of magnitude, right response to load. The heater test that first confirmed the decode — 3.2 kW measured against a dash showing 3–4 kW — passes on either scale.

What killed it was cross-checking against a *second, independent* source. When the group-01 current sensors were decoded, they used `÷1024`, and they agreed with the corrected group-05 value to within 0.05 A. WORKLOG entry 35 records the change and the size of the error. `CLAUDE.md` now carries it as a standing note: "Group 05 current is ÷1024, not ×0.001 — they differ by 2.4 % and 1024 matches the group-01 sensors."

**Generalisable.** Plausibility checks catch errors of a factor of ten. They do not catch errors of two percent. Only a second measurement of the same quantity does, and a power-of-two scale is more likely than a power-of-ten one in an embedded system.

---

## 7. Fan 6 versus 7: sometimes there is no answer

Not a mistake. A limit, and how to report one.

The fan walk (entry 61) stepped the blower 1→7 and back down, watching HVAC group 10. Byte 11 tracked it cleanly: `84 85 86 88 89 8B 8B` for speeds 1 through 7 going up, and the same values coming back down. Bit 7 is "blower on"; the low bits are blower motor volts — 4, 5, 6, 8, 9, 11, 11.

Speeds 6 and 7 both read `8B`, 11 V. Every sample, both directions. Speed 7 was first sampled at 11 V while still ramping, so at first this looked like a timing artefact, but repeated walks gave the same answer: the amplifier does not distinguish them at this byte.

The honest move was not to guess. `docs/SIGNALS.md` records it explicitly, including that the decoder has a 12 V slot for speed 7 that **has never been observed on this car**, and the Climate tile displays "6–7".

**Generalisable.** "These two states are indistinguishable in the data I can see" is a finding. Displaying a confident 7 when the bus said 11 V would have been a small lie told many times per second. A range, a question mark, or a documented ambiguity costs you nothing and keeps the rest of your work credible. Chapter [07](07-confidence-and-honesty.md) is entirely about this instinct.

---

## 8. A cheerful counterpoint: the regression the tests caught

During the vehicle-profile surgery on 2026-08-28 — carving the Leaf-specific parts out of the reader into `vehicles/leaf_ze0.py` — a careless span replace ate `TILE_FIELDS`. Every reader test failed, all of them the same way. It was restored, and the log records it in one clause.

That is the entire story, and that is the point. A refactor of the load-bearing module in a project with no hardware in the room produced a total failure that was diagnosed in seconds, because the fixtures in `tests/fixtures/` are real captures and the tests assert real observed values. `pytest -q` runs the whole suite with no car, no adapter and no Bluetooth.

Every other mistake in this chapter took a session to find. This one took a test run. Write the fixtures.

---

## 9. Optional: measure before you optimise

Worth including because the numbers are documented and the shape is instructive.

The dashboard updated every **25–30 seconds**. Nobody had planned that; it accumulated. Entry 51 enumerates the causes rather than guessing at one:

| Cause | Fix |
|---|---|
| a 0.3 s post-prompt sleep on *every* `send()`, ~35 commands per cycle | `wait=0` for AT commands (they finish at the prompt) |
| `ATCAF0` + `ATCRA` re-sent for every passive ID | `ATCAF0` once per passive block |
| 12-command ECU switches | 4-command `set_uds_target()` |
| the 29-frame cell read every cycle | groups 02/06 every 2nd cycle |
| passive IDs all polled every cycle | staggered plan by period |
| `--interval` added *after* the cycle instead of being the target period | `--interval` is now the target period |

Result: **3.7–7.9 s cycles.** Then entry 53 rebuilt the scheduler around items with periods and a fast lane, and only polled items that enabled tiles actually need: **1.9–2.9 s.**

The first item on that list is the one to notice. Roughly 35 commands × 0.3 s is about 10 seconds of the original cycle, spent sleeping for no reason. Nobody would have guessed it; it fell out of counting. Entry 51 also states the remaining floor honestly: about 0.15 s per command round-trip over BLE, plus roughly 2 s for the 29-frame cell read and the passive capture windows. Knowing your floor is how you know when to stop.

---

## Apply this

A short protocol, drawn from the seven failures above.

1. **Label hypotheses as hypotheses.** In the log, in comments, in the doc. Mistake 2 cost a session because a guess sat next to a fact for six months and got promoted.

2. **Make your signal hold still.** Before believing a byte, hold the input constant and confirm the byte does too. Then walk back down and confirm it retraces. Counters die here.

3. **Distrust the crash site.** If the code at the top of your traceback has no mechanism for causing the failure, look for shared mutable state, threads, or memory being handed between them.

4. **Ask which layer your fix addressed.** Symptom, mechanism, or cause? Ship the symptom fix if it makes things observable, but keep looking. Write down which one you think it was.

5. **Cross-check with an independent source.** Plausibility catches 10×. Only a second measurement catches 2 %. A power of two beats a power of ten as a guess.

6. **Suspect the sensor's location, not just your maths.** A correct decode of the wrong physical quantity looks exactly like a broken decode.

7. **Say "indistinguishable" out loud** when two states are. A documented ambiguity is worth more than a confident guess, and it tells the next person what experiment is still open.

8. **Keep the raw capture.** Every fixture in `tests/fixtures/` is a session you never have to repeat, and the reason a mistaken decode is a five-minute correction instead of another trip to the car.
