# 05 — UDS and ISO-TP: asking an ECU directly

**What this chapter teaches**

- The difference between sniffing broadcasts and asking a specific ECU a question.
- The exact ELM327 command sequence that gets a 2012 Leaf's battery controller talking, and what every one of those commands does.
- How a multi-frame ISO-TP answer is reassembled, why the reassembled buffer is longer than the data, and the one guard that stops a truncated line becoming a fake reading.
- Why functional addressing (`7DF`) silently fails on long answers, and what to use instead.
- How to map an unknown ECU by sweeping groups and reading its refusals.
- The read-only boundary this project does not cross.

**What this chapter assumes:** you can get an ELM327 on the bus and capture frames ([01](01-getting-on-the-bus.md)), and you know the differential method ([02](02-the-method.md)).

---

## Two different conversations

Everything in chapters [01](01-getting-on-the-bus.md) through [04](04-analog-and-scaled.md) is eavesdropping. The car is talking to itself, you set a filter, and you write down what goes past. In ELM327 terms that is `ATCAF0` (auto-formatting off, give me raw bytes), `ATCRA <id>` (only show me this ID) and `ATMA` (monitor all, which with a filter set means monitor that one).

This chapter is the other conversation: you send a request to a named ECU and it sends an answer back. That is UDS (ISO 14229) carried over ISO-TP (ISO 15765-2) on CAN. It gets you data that is never broadcast at all — on the Leaf, the entire battery controller dataset.

The two modes do not mix. From `docs/SIGNALS.md`:

> Passive sniffing **must use `ATCAF0`**; with `ATCAF1` most raw frames print as `<DATA ERROR`

and conversely, the UDS setup below needs `ATCAF1`. Getting this backwards cost a whole session in February 2026; chapter [06](06-when-youre-wrong.md) tells that story properly.

| | Passive sniffing | UDS request/response |
|---|---|---|
| Who starts it | the car | you |
| ELM327 formatting | `ATCAF0` | `ATCAF1` |
| Addressing | `ATCRA <id>` filter | `ATSH <tx>` + `ATCRA <rx>` |
| Flow control | not involved | required for anything over 7 bytes |
| Typical Leaf use | gear, doors, lights, TPMS | pack SOC, 96 cell voltages, temperatures |

---

## Before you reverse anything: try the standard PIDs

The Leaf needed all of this because it does not implement standard OBD-II. `docs/SIGNALS.md` records the result plainly: `0100` returns `NO DATA`, and everything useful is Nissan-proprietary.

The 2009 Lancer in the same repo is the opposite case. It answers plain SAE J1979 mode 01, 39 supported PIDs, and the first console read took minutes rather than sessions: coolant 203 °F, 13.84 V charging, 719 rpm idle (WORKLOG, 2026-08-28). No reverse engineering happened at all. `vehicles/lancer_2009.py` is one page of PID numbers and lambdas.

So: ask your car for `0100` first. If it answers, read the J1979 PID list and go straight to a dashboard. Spend your reverse-engineering budget on the car that refuses.

---

## The Leaf LBC recipe, command by command

The Leaf's Lithium Battery Controller lives on EV-CAN, which is not on OBD pins 6/14. The VCM bridges Car-CAN to EV-CAN for diagnostic requests, so a bog-standard ELM327 on 6/14 can still reach it. This is the sequence, from `elm327.configure_leaf_bms()`:

```
ATZ                 reset the adapter
ATE0                echo off
ATL1                linefeeds on
ATH1                headers on  (you need the responding CAN ID)
ATS1                spaces on   (readable hex)
ATSP6               protocol 6: ISO 15765-4 CAN, 11-bit, 500 kbit/s
ATSH 79B            set header: requests go out as CAN ID 0x79B
ATCRA 7BB           receive filter: only accept 0x7BB
ATCAF1              auto-formatting ON (the adapter does ISO-TP for you)
ATFCSH 79B          flow-control frames are sent with header 0x79B
ATFCSD 30 00 20     flow-control payload: ContinueToSend, BS 0, STmin 0x20
ATFCSM1             flow-control mode 1: use the header and data I just gave you
```

Then the actual question is a UDS service `0x21` (read data by local identifier) with a group number:

```
2101    battery state   -> 6 frames
2102    cell voltages   -> 29 frames, 96 cell pairs
2104    temperatures    -> 3 frames
```

### The flow-control trio

This is the part people get stuck on, so it is worth spelling out.

ISO-TP splits a long answer into 8-byte CAN frames. The sender transmits one **first frame**, then stops and waits for the receiver (you) to send a **flow-control frame** saying "go ahead". Only then do the **consecutive frames** arrive.

An ELM327 will do this for you, but it has to know what to put in that flow-control frame:

| Command | Meaning |
|---|---|
| `ATFCSH 79B` | Send the flow-control frame with CAN ID `0x79B` — the same ID the LBC is expecting to hear from. |
| `ATFCSD 30 00 20` | The three payload bytes. `0x30` = ContinueToSend. `0x00` = BlockSize 0, meaning "send the whole rest, do not wait for me again". `0x20` = STmin, the minimum separation between consecutive frames; per ISO 15765-2 values `0x00`–`0x7F` are milliseconds, so this is 32 ms. |
| `ATFCSM1` | Flow-control mode 1: actually use the header and data above, rather than the adapter's own guess. |

Leave any of these out and you get exactly one frame back and nothing else. That is the failure mode to recognise: **a short answer where you expected a long one usually means flow control, not a missing feature.**

The reader does not repeat all eleven commands every time it changes ECU. `elm327.set_uds_target()` sends only four, because `ATFCSD` and `ATFCSM` persist across header changes:

```python
await elm.send("ATCAF1", wait=0)
await elm.send(f"ATSH {tx}", wait=0)
await elm.send(f"ATCRA {rx}", wait=0)
await elm.send(f"ATFCSH {tx}", wait=0)
```

Cutting the ECU switch from twelve commands to four was one of the changes that took the dashboard's cycle time from about 28 seconds to under 3 (WORKLOG entries 51 and 53). Over BLE, each command round-trip costs roughly 0.15 s, so command count is latency.

---

## Reassembling the answer

With `ATCAF1` the adapter hands you the raw ISO-TP frames, one per line, each prefixed with the responding CAN ID. `parse_isotp()` in `leaf_decoders.py` is 20 lines and does the whole job:

```python
pci = hx[0]
if (pci & 0xF0) == 0x10:        # first frame: 10 LL 61 NN d0 d1 d2 d3
    data.extend(hx[4:])
elif (pci & 0xF0) == 0x20:      # consecutive frame: 2N d0..d6
    data.extend(hx[1:])
elif (pci & 0xF0) == 0x00:      # single frame: 0L 61 NN d..
    data.extend(hx[3:1 + pci])
```

Three frame types, distinguished by the top nibble of the first byte:

| Top nibble | Type | Layout in an 8-byte frame | Data bytes |
|---|---|---|---|
| `0x0` | Single frame | `0L` + payload | `L` − 2 after the `61 NN` echo |
| `0x1` | First frame | `10 LL` + `61 NN` + data | 4 |
| `0x2` | Consecutive frame | `2N` + data | 7 |

`N` in a consecutive frame is a sequence number that wraps 0–F. `parse_isotp` deliberately does not check it: it trusts the adapter to deliver frames in order and concatenates. That is a simplification, not a claim of correctness under packet loss.

Note what gets stripped: the CAN ID, the PCI byte(s), **and the two-byte positive-response echo `61 NN`**. So by the time a `decode_groupNN()` function sees the buffer, byte 0 is the first real data byte. Every offset in `docs/SIGNALS.md` is 0-based into that stripped payload. If you are reading the doc against a raw capture, remember to shift.

### Why the buffer is longer than the data

`docs/SIGNALS.md` records group 02 as "192 B of cell data; the ISO-TP parse pads to 200 B", and HVAC group 10 as "41 B declared; the parse pads to 46 B". Both are the same arithmetic. ISO-TP frames are fixed size; the last one is padded out and `parse_isotp` keeps the padding.

- Group 02: 29 frames = 1 first frame (4 data bytes) + 28 consecutive frames (7 each) = 4 + 196 = **200**. 96 cell pairs at 2 bytes each is 192. The remaining 8 bytes are padding, and `decode_group02()` stops at the first value ≥ 5000 mV, which is the `0xFFFF` fill.
- Group 10: 4 + 6 × 7 = **46**, against 41 declared.

The lesson generalises. **Do not use the reassembled length as the payload length.** Take the declared length from `LL` in the first frame if you need it, and otherwise decode by offset and stop at a padding sentinel.

### The truncation guard

BLE makes this worse than serial. A notification can arrive with a line cut mid-byte, and a half-line of hex parses perfectly well into a shorter list of bytes. If you take "the last line" you will occasionally decode garbage and publish it as a reading.

`leaf_decoders.last_complete_frame()` is the fix, and `docs/ADDING_SIGNALS.md` tells decoder authors to use it by name:

```python
def last_complete_frame(lines, min_len):
    """Most recent frame with at least min_len data bytes (BLE can truncate lines)."""
    for line in reversed(lines):
        b = _frame_bytes(line)
        if b and len(b) >= min_len:
            return b
    return None
```

You say how many bytes you need, and you get the newest frame that actually has them, or nothing. Every passive decode in `decode_carcan()` goes through it. The gear decode adds a second guard on top, because a frame of `00` is well-formed but is never a valid gear:

```python
b = last_complete_frame(captures.get("421", []), 1)
if b and b[0] == 0x00:
    nz = [f for f in (_frame_bytes(l) for l in captures.get("421", [])) if f and f[0]]
    b = nz[-1] if nz else None
```

Two habits worth stealing: **require a minimum length before you index**, and **know which values your signal can never legally take**, so you can reject them instead of displaying them.

---

## Functional vs physical addressing: the Lancer DTC story

This is the sharpest lesson in the chapter, and it cost a re-read to learn.

Reading the Lancer's trouble codes on 2026-08-28 started the obvious way, with the functional broadcast address `7DF`. Any ECU that implements the requested service answers a `7DF` request. It worked, in the sense that answers came back. But only single-frame answers ever arrived, and the car turned out to have 12 stored engine codes plus a CVT code — far more than fits in one frame.

From `docs/SIGNALS.md`:

> functional addressing (`7DF`) cannot do ISO-TP flow control, so a 12-code mode-03 answer needs physically-addressed requests (`7E0/7E8` + `ATFCSH`/`ATFCSD`/`ATFCSM1`)

The reason is structural. Flow control is a point-to-point conversation: the receiver has to send a flow-control frame addressed to one specific sender. A functional request may be answered by several ECUs at once, and there is no single peer to negotiate with. So a multi-frame answer to a functional request cannot complete. It does not error. It just stops after the first frame, and if you are not counting frames you will conclude the car has one trouble code.

The re-read used physical addresses, with flow control configured:

| Target | Request header | Response filter | Reads |
|---|---|---|---|
| Engine ECU | `7E0` | `7E8` | mode `01` (MIL + count), `03` (stored), `07` (pending) |
| Transmission ECU | `7E1` | `7E9` | mode `03` (stored) |

The whole raw exchange is checked in as `tests/fixtures/lancer_dtc_raw_20260828.json`, and the profile's `configure()` sets flow control once at startup precisely so long answers reassemble:

```python
for cmd in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6",
            "ATFCSH 7E0", "ATFCSD 30 00 20", "ATFCSM1"):
    await elm.send(cmd, wait=0)
```

**Rule of thumb: `7DF` is fine for probing "does anything answer this service?", and wrong for anything that might return more than seven bytes.**

### The second bug in the same session: "empty" vs "absent"

`parse_isotp` strips the response echo. For a mode-03 answer, the bytes it strips are the service byte and the code count. That is what every decoder wants — except that it makes an empty result ambiguous. A car with no stored codes and a car that did not answer at all both produce an empty byte list.

The fix, in `vehicles/lancer_2009.py`, is to look at the raw frames for the positive-response byte *before* reassembling:

```python
resp, seen = svc + 0x40, False          # 0x43 for mode 03, 0x47 for mode 07
...
    top = hx[0] & 0xF0
    if (top == 0x00 and len(hx) > 1 and hx[1] == resp) or \
       (top == 0x10 and len(hx) > 2 and hx[2] == resp):
        seen = True
if not seen:
    return None                          # no answer
...
rec[key] = " ".join(codes) if codes else "none"
```

`None` means the ECU said nothing. `"none"` means the ECU said, clearly, that it has no codes. Those display differently and they should.

This is a general decoder trap and it is worth naming: **an empty result and a missing result are not the same value, and a helper that normalises both to "empty" has destroyed information you needed.** Any time your parser strips a header, ask what it just made indistinguishable.

---

## Discovery by sweeping, and reading refusals

You will meet ECUs with no documentation at all. The Leaf's HVAC amplifier (`0x744` → `0x764`) was one. The approach was brute and effective: point the adapter at it and ask for every service-21 group in turn.

The result, from `docs/SIGNALS.md` and WORKLOG entries 45 and 62, captured as `tests/fixtures/hvac_group_sweep.json`:

| Group | Result |
|---|---|
| `00` | answers, 4 B: `80 01 80 00` |
| `01` | answers, 11 B |
| `10` | answers, 41 B declared — the useful one (cabin/ambient/evaporator temps, blower, A/C) |
| `11` | answers, 11 B |
| `82` | answers, DTC-style, mostly `FF` |
| `83` | answers, ASCII part number |
| everything else | NRC `0x12` |
| service `0x22` (any identifier) | NRC `0x11` |

A negative response is not a failure. It is a fact about the ECU, delivered for free:

| NRC | Standard meaning | What it tells you |
|---|---|---|
| `0x11` | serviceNotSupported | Stop trying this whole service on this ECU. `0x22` is a dead end on the HVAC amp. |
| `0x12` | subFunctionNotSupported | The service works; this particular group number does not exist. Keep sweeping. |
| `0x80` | outside the common ISO 14229 set; manufacturer-specific | Something is being refused for a reason the standard does not name. On the Leaf's VCM this is what every `0x21`/`0x22` returns. |

The `0x11`/`0x12` distinction is the whole value of a sweep: it lets you separate "wrong service" from "wrong group", which turns a two-dimensional search into two one-dimensional ones.

`docs/SIGNALS.md` also keeps an "Other ECU probes" table, and note that its honest title is *probes*, not "ECUs that do not answer" — an earlier version had that title and contained ECUs that do answer. Chapter [07](07-confidence-and-honesty.md) has more on that.

| ECU | Address | Result |
|---|---|---|
| VCM | `0x797 → 0x79A` | NRC `0x80` for every `0x21`/`0x22`; `0x1A`/`0x09` → NRC `0x11`. Needs a CONSULT session or security access. Parked. |
| Inverter | `0x793 → 0x7BD` | no response |
| Steering | `0x746 → 0x766` | no response |
| ABS, BCM, EPS | `0x743`, `0x745`, `0x784` | respond to group 01; not yet decoded |

"No response" and "parked" are results. Write them down. The next person to probe the inverter deserves to know that somebody already tried.

---

## The read-only rule

State it once, clearly, and then live by it.

This project sends UDS service `0x21` reads and ELM327 monitor mode from the dashboard reader. The console probe tools additionally send read-identification services `0x22`, `0x1A` and OBD mode `09`, and one legacy script sends `0x10` session control. On the Lancer it reads modes `01`, `03` and `07`. **Mode `04`, which clears trouble codes, is never sent.** Nothing anywhere in the repository sends a control, routine, write, or security-access service. `SECURITY.md` is the policy document and says pull requests adding UDS `0x2E`, `0x2F`, `0x31`, `0x3D` or `0x27` will not be merged without an explicit documented safety review.

The reasoning is not squeamishness. A read cannot brick a module or clear a fault history somebody needed. A write can. Programmatic control was explicitly moved out of this project to a future sibling gated behind native CAN hardware (WORKLOG entry 65), and the decision was recorded rather than left implicit.

If you are following this guide on your own car: reads are cheap and reversible, mode `04` throws away evidence, and security access exists to keep you out of things that were not designed for you to be in.

---

## Try it on your own car

1. **Check for the easy path.** Configure `ATSP6`, then send `0100`. If you get data back, you have standard OBD-II and this chapter is optional.

2. **Find who answers.** Probe candidate ECU addresses with a short request and note the reply for each. Silence, a positive answer, and a negative response code are three different outcomes; record which you got.

   ```bash
   # in this repo, against the Leaf's HVAC amp
   python probe_hvac_carcan.py --phase B
   ```

3. **Sweep one ECU's groups.** For an ECU that answers, walk service `21` across `00`–`FF` and tabulate: answered, NRC `0x12`, NRC `0x11`, silence. You now have its map.

4. **Count your frames.** If an answer looks suspiciously short, check whether you configured `ATFCSH` / `ATFCSD` / `ATFCSM1`, and whether you used a functional address for something that needs a physical one.

5. **Guard your decoder before you trust it.** Use a minimum-length check on every frame, know your signal's impossible values, and make sure "no answer" and "empty answer" produce different results.

6. **Keep the raw exchange.** Every discovery in this repo has a JSON fixture in `tests/fixtures/` with the date in the filename. That is what makes chapter [08](08-from-signal-to-tile.md)'s test step possible, and chapter [06](06-when-youre-wrong.md)'s corrections cheap.
