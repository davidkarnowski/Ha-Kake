# Reverse-engineering your car

How the signals in this project were found: doors, locks, every exterior lamp,
gear position, fan speed, climate setpoint, pack current, 96 cell voltages.
None of it came from a service manual. All of it came from a $20 adapter, a
parked car, and a method you can repeat on a vehicle nobody has touched yet.

**This guide teaches the method, not just the answers.** The byte tables for a
2012 Leaf are in [`../SIGNALS.md`](../SIGNALS.md) and they are useful to maybe
a few thousand people. The method works on anything with a CAN bus.

## Who this is for

- You own a car and want to see what it already knows about itself.
- You have an OBD-II adapter and a laptop, and no idea what to do next.
- You want to add a [vehicle profile](../../vehicles/__init__.py) to this
  project so the dashboard works on your car.
- You are an AI agent building context on how this project decodes signals.

No prior CAN experience is assumed. You will need patience and a willingness to
write down the things that did not work.

## The method, in one page

Everything in this guide is a variation on one loop.

**1. Get on the bus.** Make the adapter talk, and make it show you frames.
Most of the pain here is adapter quirks rather than the car. See
[chapter 01](01-getting-on-the-bus.md).

**2. Narrow to one ID.** A car broadcasts dozens of messages continuously. An
unfiltered capture overflows a cheap clone's buffer in seconds and tells you
nothing. Filter to a single CAN ID and watch just that.

**3. Change exactly one physical thing.** Open one door. Click the stalk one
position. This is the entire trick. A capture where nothing changed proves
nothing, and a capture where three things changed proves less than nothing,
because it will convince you of something false.

**4. Walk it up, then walk it back down.** The single most valuable discipline
in this guide. On the way up, several bytes will appear to follow your input.
On the way back down, most of them keep going the same direction, because they
were temperatures drifting or counters counting, not responses. Only a value
that returns when you return is actually tracking the thing you touched.

**5. Score what moved.** Four questions, and this project's walker
([`calibrate_input.py`](../../calibrate_input.py)) computes all of them: Is the
byte *stable* while you hold a position? Is it *consistent* when you come back
to a position you already visited? Is it *monotonic* across an ordered control?
How many *distinct* values does it take? Stability kills rolling counters;
consistency kills drift. See [chapter 02](02-the-method.md).

**6. Verify against something independent.** A decode that looks plausible is
not verified. This project once had a current scale that was wrong by 2.4 % and
it survived every sanity check anyone applied, because 2.4 % looks exactly like
correct. It was caught only by a second sensor measuring the same current a
different way. See [chapter 04](04-analog-and-scaled.md).

**7. Label how sure you are, honestly.** Verified, static, or tentative, and
never quietly upgrade one. See [chapter 07](07-confidence-and-honesty.md).

**8. Write down what did not work.** A documented negative is a real result.
This project walked the climate system's vent mode, AUTO button, and
fresh/recirc control, and nothing moved anywhere. That took an evening, and it
is recorded so that nobody, including its author, ever spends that evening
again.

## The chapters

| # | Chapter | What you get |
|---|---|---|
| 01 | [Getting on the bus](01-getting-on-the-bus.md) | Adapters, BLE quirks, filtering, and the one configuration mistake that costs whole sessions |
| 02 | [The method](02-the-method.md) | Differential capture, walking an input, and how the scoring actually works |
| 03 | [Walking discrete inputs](03-walking-discrete-inputs.md) | Doors, locks, lamps, turn signals, gear. Byte-exact, with the physical procedure for each |
| 04 | [Analog and scaled values](04-analog-and-scaled.md) | Fan speed, setpoints, temperatures, currents, and why nearly-right is the dangerous case |
| 05 | [UDS and ISO-TP](05-uds-and-isotp.md) | Asking an ECU directly instead of listening: flow control, multi-frame reads, negative response codes |
| 06 | [When you're wrong](06-when-youre-wrong.md) | Every significant wrong hypothesis in this project, how long it survived, and what killed it |
| 07 | [Confidence and honesty](07-confidence-and-honesty.md) | The verified/static/tentative discipline, and why demoting your own claims is healthy |
| 08 | [From signal to tile](08-from-signal-to-tile.md) | Turning a decoded byte into something on the dashboard |

Read 01 and 02 first. After that, 03 if your target is a switch, 04 if it is a
measurement, 05 if the car will not broadcast it and you have to ask.

Chapter 06 is worth reading even if you never touch this project's code. It is
the honest accounting of a rolling counter mistaken for a gear indicator, a
segfault blamed on Bluetooth twice before anyone looked at the database, and a
sleep-recovery bug that took three fixes before one of them addressed the
cause.

## What you need

- **An ELM327 adapter.** This project is tested with a LELink BLE clone and an
  obdiisoft USB CH340, both reporting ELM327 v1.5. Cheap clones are fine and
  are what most people have. They also lie in interesting ways, which
  [chapter 01](01-getting-on-the-bus.md) covers.
- **A car you own**, or have explicit permission to work on.
- **A laptop.** macOS and Linux are both fine for USB; BLE is developed and
  tested on macOS.
- **Time in a parked car.** Most of the discoveries behind this guide were made
  sitting in a driveway with the air conditioning running.

## Before you start: this is read-only

Everything in this guide reads. Nothing writes.

The dashboard's reader sends UDS service `0x21` read requests and ELM327
monitor mode. The console probe tools additionally send read-identification
services. Nothing anywhere in this project sends control, routine, write, or
security-access services, and on a car speaking standard OBD-II, mode `04`
(clear trouble codes) is never sent.

That boundary is not decoration. A diagnostic bus will accept commands that
change how your car behaves, and the gap between "read a value" and "write a
value" is one hex digit. Stay on the reading side of it, work on a parked car,
and read [`../../SECURITY.md`](../../SECURITY.md) before you decide to explore
further than this guide goes.

## Where the answers live

- [`../SIGNALS.md`](../SIGNALS.md) is the byte-level authority: every decoded
  CAN ID and offset, per vehicle, with its confidence label.
- [`../ADDING_SIGNALS.md`](../ADDING_SIGNALS.md) is the routine for wiring a new
  decode into the dashboard once you have found it.
- [`../../vehicles/`](../../vehicles/) holds one module per supported vehicle.
  [`lancer_2009.py`](../../vehicles/lancer_2009.py) is the minimal example, and
  a useful reminder that not every car needs reverse engineering at all. It
  speaks standard SAE J1979, so its entire profile is a page of public spec.

**Start by checking whether your car answers standard OBD-II PIDs.** If it
does, you may not need most of this guide. Reverse engineering is what you do
for the signals the standard does not cover, which on the Leaf turned out to be
almost everything interesting.

## Credit where it is due

Community documentation gave this project many of its starting hypotheses,
in particular the Open Vehicle Monitoring System's Nissan Leaf module and
dalathegreat's `leaf_can_bus_messages` DBC collection. Those are credited in
[`../SIGNALS.md`](../SIGNALS.md).

A hypothesis is not a fact, and this guide is careful about the difference. The
gear values for `0x421` came from a community DBC and stayed marked tentative
until someone sat in the car and shifted through all five positions. That is
the relationship to aim for with other people's work: take their map
gratefully, then go and check the territory.
