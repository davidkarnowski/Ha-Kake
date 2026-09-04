# 1. Getting on the bus

**What this chapter teaches**

- How to get a cheap ELM327 clone to speak at all, over BLE and over USB serial.
- The three adapter quirks that will otherwise cost you a weekend each:
  `response=True` on BLE writes, `ATCRA` filtering, and `ATCAF0` vs `ATCAF1`.
- Why a working transport can still hand you `DATA ERROR` on perfectly good frames.
- What is and is not reachable from a standard OBD-II plug on a 2011–2012 Leaf.
- Where the per-machine adapter settings live so you never commit them.

**What this chapter assumes:** nothing. This is the first chapter. If you already
have frames on screen, skip to [the method](02-the-method.md).

---

## The two adapters

Everything in this guide was captured with two sub-$30 ELM327 clones. Both
report firmware "ELM327 v1.5" and both identify as "OBDII to RS232 Interpreter",
which is what a clone says whether or not it is one.

| Adapter | Link | Notes |
|---|---|---|
| LELink, advertises as `OBDBLE` | BLE (GATT) | Needs `response=True` on every write |
| obdiisoft.com USB dongle | USB serial, CH340 chip, 38400 baud | Has an HS/MS switch; leave it on **HS** |

Both speak the same AT command set. In this repo the difference is hidden behind
one file, `elm327.py`, which exposes `BleELM` and `SerialELM` with the same
`send()` and a `detect_adapter()` that tries USB first and falls back to BLE.

The USB adapter produced byte-identical BMS data to the BLE one, so neither is
"more correct" — but the USB one is faster per round trip and has none of the
BLE quirks. If you are starting from zero, start on USB.

### The HS/MS switch is not what you want it to be

The USB adapter has a two-position switch labelled HS and MS. The hopeful reading
is "MS gets me the other bus". It does not.

In the **MS** position every command returns `CAN ERROR`. All four protocol
combinations were tried (500k and 250k, 11-bit and 29-bit) and no traffic
appeared on any of them. On this dongle the MS position selects OBD pins 3/11,
which on a Leaf is AV-CAN, not EV-CAN. Leave the switch on **HS**, which is
pins 6/14, the same bus the BLE adapter sits on.

---

## Finding a BLE adapter

Step one is a passive scan. `scan_ble.py` lists every nearby BLE device with
its RSSI and flags the likely OBD ones by name.

```bash
./venv/bin/python scan_ble.py
```

On the first session this found the adapter as `OBDBLE` at −52 dBm.

Step two is GATT enumeration. A BLE ELM327 clone does not use a standard
serial-over-BLE profile; it exposes a vendor characteristic and expects you to
write ASCII AT commands into it and read the replies back as notifications.
Enumerating the services on this adapter turned up:

| Service | Characteristic | Properties |
|---|---|---|
| `0x180A` Device Information | — | generic placeholders |
| `0xFFE0` vendor specific | `0xFFE1` | notify / write / read |
| `0xFFE0` vendor specific | `0xFFEE` | write / read (config) |

`0xFFE0` / `0xFFE1` is the pairing you want: subscribe to notifications on
`0xFFE1`, write your command to `0xFFE1`, read the answer off the notification
stream. That combination is common across a whole family of cheap BLE serial
bridges, so if your adapter is not a LELink there is a good chance it is still
`FFE0`/`FFE1`.

Put your adapter's address in `config.local.json` (copy
`config.local.example.json`), which is gitignored precisely so nobody publishes
their own hardware address:

```json
{
  "ble_addr": "",
  "ble_name": "OBDBLE"
}
```

Leave `ble_addr` empty and the transport finds the adapter by name at connect
time, which is the more portable option anyway.

---

## Quirk one: `response=True`, or the transport that lies

The first CAN monitor written for this project produced **zero output**. Not an
error, not a timeout with a message, not a partial frame. The writes returned
success. The notification callback never fired once.

The temptation at this point is to assume the car is asleep, or the protocol is
wrong, or the CAN ID is wrong. All three are wrong guesses, and all three cost
time, because you cannot distinguish "no data on the bus" from "the transport is
silently dropping my commands" by staring at an empty screen.

The fix was to stop guessing and write a diagnostic whose only job was to answer
one question. `diag_notify.py` sent the same trivial command two ways, with
write-with-response and write-without-response, and counted notification
callbacks in each case. Test B answered it:

> This adapter **only triggers BLE notifications when writes use
> `response=True`**. `response=False` (write-without-response) silently succeeds
> but produces zero notification callbacks.

Write-without-response is faster and most BLE serial bridges accept it, so it is
a natural default and several BLE libraries make it easy to reach for. On this
adapter it is a black hole.

The general lesson is worth more than the specific fix. **When a tool produces
nothing at all, suspect the instrument before you suspect the subject.** Build
the smallest possible experiment that distinguishes "the car said nothing" from
"my code never asked". A capture that produces nothing looks the same in both
cases, and only a deliberate test tells them apart.

In `elm327.py` the write is now unconditional:

```python
await self.client.write_gatt_char(
    self.characteristic, (cmd + "\r").encode(), response=True)
```

The USB serial transport has no equivalent quirk.

---

## Quirk two: filter, or drown

The ELM327's `ATMA` command ("monitor all") dumps every frame on the bus. On a
running Leaf that is far more than a clone's buffer can hold.

Measured: an unfiltered `ATMA` produced **24 frames across 16-plus CAN IDs
before `BUFFER FULL`**. That is under a second of bus time, and worse, the
frames you do get are an arbitrary slice of whatever arrived first.

The cure is `ATCRA <id>`, which sets a receive-address filter so the adapter
only passes frames matching one ID. Set the filter, then monitor:

```
ATCAF0
ATCRA 60D
ATMA
```

Filtered captures on a single ID have run for 60 seconds with **605 frames and
zero errors** at roughly 10 frames per second. Same adapter, same bus.

So an unfiltered sweep is only useful for one thing: discovering *which* IDs
exist. Do it once, write down the ID list, and never do it again. Everything
after that is one ID at a time.

`elm327.passive_capture()` wraps this pattern, and takes a `set_caf` flag so a
block of consecutive captures only pays for the `ATCAF0` round trip once.

---

## Quirk three: `ATCAF0` versus `ATCAF1`

This is the one that cost a whole session, and it is the reason the February
notes are full of a hypothesis that turned out to be wrong.

`ATCAF` is "CAN auto-formatting". With it **on** (`ATCAF1`), the ELM327 tries to
interpret frames as ISO-TP: it reads the first byte as a PCI (protocol control
information) byte, strips it, and reassembles multi-frame messages for you. That
is exactly what you want for UDS diagnostic responses, which really are ISO-TP.

With it **off** (`ATCAF0`), the adapter hands you the raw bytes and stays out of
your way. That is exactly what you want for passive sniffing, where the frames
on the bus are plain broadcast data with no ISO-TP structure at all.

Mix them up and the adapter tells you `DATA ERROR`, because it tried to
reassemble a message that was never a message.

### The wrong hypothesis, preserved

The February session log records the symptom faithfully and the cause
incorrectly. In the table of observed IDs, several rows carry this note:

| CAN ID | Data sample | Note as written in February |
|---|---|---|
| `002` | `7D FF 00 07 3E` | DATA ERROR (**may be 29-bit**) |
| `245` | `7F E8 02 18 3A 00 7F E2` | DATA ERROR |
| `292` | `80 08 28 80 30 00 00 02` | DATA ERROR |
| `6F6` | `81 00 00` | DATA ERROR |

and elsewhere, `0x5B3` producing "6 frames with DATA ERROR (protocol mismatch)".

"May be 29-bit" is a reasonable guess. It is also completely wrong, and it is
wrong in a way that is hard to disprove from inside the same session, because
"try 29-bit addressing" is a change that produces *different* nothing rather
than data. Note also that the raw bytes were sitting right there in the sample
column the whole time. The adapter was receiving the frames correctly and then
refusing to print them cleanly.

The correction, six months later:

> **`ATCAF0` is the key for passive Car-CAN** — every ID that showed
> `<DATA ERROR` in Feb (0x5B3, 0x292, 0x002…) decodes fine with auto-formatting
> OFF. `ATCAF1` is only needed for ISO-TP (UDS) responses.

Every one of those IDs decoded on the first try once auto-formatting was off.
`0x5B3` in particular turned out to carry the dash's own state-of-health
percentage, which had been sitting behind a `DATA ERROR` since the first day.

The rule, which now lives in the project's `CLAUDE.md` because it is that easy
to forget:

> **Passive sniffing uses `ATCAF0`; UDS uses `ATCAF1`.**

[Chapter 6](06-when-youre-wrong.md) goes further into how a wrong hypothesis
survives in a log, and [chapter 5](05-uds-and-isotp.md) covers the ISO-TP side
in detail, including flow control.

---

## The configuration sequence

For UDS work against the Leaf's battery controller, `configure_leaf_bms()` sends
this after a reset:

```
ATZ
ATE0     echo off
ATL1     linefeeds on
ATH1     headers on — you want to see which ID a line came from
ATS1     spaces on
ATSP6    ISO 15765-4 CAN, 11-bit, 500 kbit/s
ATSH 79B set the request header (the LBC)
ATCRA 7BB accept only its answers
ATCAF1   auto-formatting on — this is a UDS target
ATFCSH 79B / ATFCSD 30 00 20 / ATFCSM1   flow control
```

`ATSP6` is the protocol that works on this car: ISO 15765-4 CAN, 11-bit
identifiers, 500 kbit/s. You can let the ELM327 auto-detect with `ATSP0`, but
pinning it saves several seconds per connect and removes a variable.

`ATH1` is not optional in practice. With headers off you get bytes with no
indication of which CAN ID produced them, which makes an unfiltered sweep
useless and a filtered one merely fragile.

For passive work, the only changes are `ATCAF0` and a per-ID `ATCRA`.

---

## What you can actually reach on a ZE0 Leaf

This is the part that saves a stranger weeks, so it gets said plainly.

The 2011–2012 Leaf has **no CAN gateway** on the OBD-II port. Raw bus traffic is
directly available. That is the good news. The bad news is that the bus you get
from a normal ELM327 is not the interesting one.

| OBD-II pin | Signal |
|---|---|
| 6 / 14 | **Car-CAN** H / L — what every ELM327 plugs into |
| **13 / 12** | **EV-CAN** H / L |
| 11 / 3 | AV-CAN H / L |
| 4, 5 | chassis / signal ground |
| 8 | +12 V only when the vehicle is powered on |
| 16 | permanent +12 V |

This pinout was confirmed on 2026-08-25 against two independent sources: the
OVMS ZE0 cable pinout (SKU 1779000, which wires OBD 13 → DB9 CAN-H and OBD 12 →
DB9 CAN-L as its *primary* bus, with 6/14 as the alternate) and the
sethfischer Leaf OBD manual.

So, from a stock OBD plug:

**Reachable on Car-CAN, passively:** gear (`0x421`), doors and locks and lights
(`0x60D`), turn signals (`0x358`), TPMS (`0x385`), odometer and parking brake
(`0x5C5`), dash state-of-health (`0x5B3`), and more. These are covered in
[chapter 3](03-walking-discrete-inputs.md).

**Reachable on Car-CAN, by asking:** the battery controller and the HVAC
amplifier answer UDS requests, and the VCM bridges Car-CAN to EV-CAN for
diagnostic traffic even though it does not bridge broadcast frames. That is how
the full 96-cell battery read works from pins 6/14. See
[chapter 5](05-uds-and-isotp.md).

**Not reachable without re-pinning:** the EV-CAN broadcast frames.
`0x1DB` (pack volts and amps at 10 ms), `0x1DA` (motor torque and RPM),
`0x55B` (SOC), `0x5BC` (GIDs), `0x54C` (ambient), `0x54F` (cabin),
`0x11A` (gear and eco). None of these appear on Car-CAN. A probe specifically
looked for `0x1DB`, `0x1DA`, `0x55B` and `0x5BC` bridged to Car-CAN and found
none of them.

The workaround is mechanical, not clever: a re-pinned OBD-II extension cable
with female pin 6 wired to plug pin 13 and female pin 14 wired to plug pin 12.
That puts a standard ELM327 on EV-CAN. Only the 2018-and-later ZE1 has a gateway
that would block this.

The Leaf also does **not implement standard OBD-II service modes**. `0100`
returns `NO DATA`. Everything useful is Nissan-proprietary. (By contrast, the
2009 Lancer that became this project's second vehicle profile answers 39
standard mode-01 PIDs and gave up coolant temperature, RPM and battery voltage
within minutes of first contact. Not every car makes you work.)

---

## Try it on your own car

A first-contact procedure that works on an unknown vehicle:

1. **Identify the adapter.** USB: find the serial port (`ls /dev/tty.*` on
   macOS, `/dev/ttyUSB*` on Linux) and try 38400 baud first, then 115200 and
   9600. BLE: run a passive scan and look for a device named like an OBD
   adapter, then enumerate its GATT services and look for a characteristic with
   both notify and write.

2. **Prove the transport before you blame the car.** Send `ATI` and require an
   identity string back. If you get nothing, do not proceed to CAN commands —
   fix the transport. On BLE, the first thing to try is toggling
   write-with-response. Write the smallest test that distinguishes the two
   failure modes; do not eyeball it.

3. **Ask for standard PIDs first.** `ATSP0` then `0100`. If you get a supported-
   PID bitmask back, your car implements SAE J1979 and a large amount of this
   guide is optional for you: you can read documented PIDs directly. If you get
   `NO DATA`, you are in proprietary territory and the rest of this guide is for
   you.

4. **Do exactly one unfiltered sweep.** `ATCAF0`, then `ATMA`, and let it hit
   `BUFFER FULL`. Record every CAN ID you saw. That list is your search space.

5. **Then filter, always.** `ATCAF0`, `ATCRA <id>`, `ATMA` for a few seconds per
   ID. Sweep your ID list one at a time and save the raw lines with the ID and a
   timestamp.

6. **If frames print as `DATA ERROR`, check auto-formatting before anything
   else.** It is a one-command test and it is the single most likely cause.

7. **Find your bus pinout before you conclude a signal does not exist.** Search
   for your model plus "OBD pinout" and cross-check at least two sources; for
   EVs and hybrids in particular the drivetrain bus is very often not on pins
   6/14. Absence of a signal on the bus you happen to be plugged into is not
   evidence of absence on the car.

Next: [the method](02-the-method.md), which is how you turn a list of CAN IDs
into a decoded signal.
