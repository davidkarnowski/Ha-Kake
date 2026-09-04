# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Frame encoder — the inverse of leaf_decoders.py.

Every offset, scale and sign here is taken from docs/SIGNALS.md, which is the
byte-level authority. Nothing in this file may be "fixed" to make a test pass:
if the encoder and the decoder disagree, one of them disagrees with SIGNALS.md
and that is the thing to settle first.

ISO-TP framing follows what the real LBC and HVAC amp put on the wire, which
`parse_isotp()` was written against:

    declared length byte = 2 (the `61 NN` positive-response echo) + D data bytes
    first frame  : "<rx> 1L LL 61 NN d0 d1 d2 d3"   (4 data bytes)
    consecutive  : "<rx> 2N d0..d6"                 (7 data bytes, last padded)
    single frame : "<rx> 0L 61 NN d0.."             (D <= 5)

So the caller sees 4 + 7*ceil((D-4)/7) bytes — more than D when the last frame
is padded. That is exactly why group 02 (D=196) parses as 200 bytes and HVAC
group 10 (D=41) parses as 46: the decoders are written to tolerate it.
"""

from .model import NUM_CELLS, FAN_VOLTS, DOOR_BITS   # noqa: F401  (FAN_VOLTS re-exported)

FF_PAD = 0xFF          # the LBC pads consecutive frames with FF
ZERO_PAD = 0x00        # the Lancer's ECUs pad with 00


# ── byte helpers ─────────────────────────────────────────────────────────

def u16(v):
    v = int(round(v)) & 0xFFFF
    return [v >> 8, v & 0xFF]


def s16(v):
    return u16(int(round(v)) & 0xFFFF)


def u24(v):
    v = int(round(v)) & 0xFFFFFF
    return [v >> 16, (v >> 8) & 0xFF, v & 0xFF]


def s32(v):
    v = int(round(v)) & 0xFFFFFFFF
    return [v >> 24, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]


def s8(v):
    return int(round(v)) & 0xFF


def line(can_id, data):
    return f"{can_id.upper()} " + " ".join(f"{b & 0xFF:02X}" for b in data)


# ── ISO-TP ───────────────────────────────────────────────────────────────

def isotp(rx, echo, payload, pad=FF_PAD):
    """ELM327 response lines for one positive response.

    `echo` is the 2-byte positive-response header the ECU repeats (e.g.
    [0x61, 0x01] for service 21 group 01, or [0x43, count] for mode 03).
    """
    echo = list(echo)
    payload = list(payload)
    declared = len(echo) + len(payload)
    body = echo + payload
    if declared <= 7:                       # single frame: 1 PCI + up to 7 bytes
        return [line(rx, [declared] + body)]
    out = [line(rx, [0x10 | ((declared >> 8) & 0x0F), declared & 0xFF] + body[:6])]
    rest = body[6:]
    seq = 1
    for i in range(0, len(rest), 7):
        chunk = rest[i:i + 7]
        chunk += [pad] * (7 - len(chunk))
        out.append(line(rx, [0x20 | (seq & 0x0F)] + chunk))
        seq += 1
    return out


def negative(rx, service, nrc):
    """A negative response: '764 03 7F 21 12'."""
    return [line(rx, [0x03, 0x7F, service & 0xFF, nrc & 0xFF])]


# ── LBC / BMS payloads (service 21, 0x79B -> 0x7BB) ──────────────────────

def temp_raw(c):
    """The u16 thermistor raw that accompanies each s8 °C in groups 04/05.

    Fitted to the real car (36 °C -> 363, 35 -> 370, 34 -> 378): raw falls
    about 7.5 counts per °C. The decoders read the °C from the s8 byte; the
    raw is kept only because the project keeps it for future calibration.
    """
    return max(0, min(0xFFFF, int(round(633.0 - 7.5 * c))))


def group01(st, sensor_dropout=False):
    """39 bytes. docs/SIGNALS.md 'Group 01 — battery state'."""
    d = [0] * 39
    i = st["current_a"]
    # sensor 1 is the coarse one on this car (±2 A steps); sensor 2 is the
    # canonical reading the reader fuses from
    d[0:4] = s32(0x7FFFFFFF if sensor_dropout else round(i * 2.0) / 2.0 * 1024)
    d[4:6] = u16(0x0287)                     # unknown, static on this car
    d[6:10] = s32(i * 1024)
    d[10:14] = [0xFF, 0xFF, 0xFF, 0xFF]      # unknown, static
    d[14:18] = [0x03, 0x60, 0x2A, 0xF8]      # unknown, static
    d[18:20] = u16(st["pack_v"] * 100)
    d[20:22] = u16(st["lv_volts"] * 1024)
    d[22:24] = u16(st["insulation_kohm"])
    d[24:26] = u16(0x00F2)                   # unknown, static
    d[26:28] = u16(st["hx"] * 100)
    d[28] = 0x00
    d[29:32] = u24(st["soc"] * 10000)
    d[32] = 0x00
    d[33:36] = u24(st["capacity_ah"] * 10000)
    d[36:39] = [0x80, 0x00, 0x05]            # unknown, static
    return d


def group02(st):
    """196 bytes: 96 u16 cell-pair millivolts, then the pack voltage twice.

    decode_group02() stops at the first word >= 5000, so anything after the
    real cells has to look like padding to it — the two pack-voltage words at
    the end of the real frame do (38 387 >= 5000)."""
    d = []
    for mv in st["cells"][:NUM_CELLS]:
        d += u16(mv)
    d += [FF_PAD, FF_PAD] * max(0, NUM_CELLS - len(st["cells"]))
    d += u16(st["pack_v"] * 100)
    d += u16(st["pack_v"] * 100)
    return d[:196]


def group03(st):
    """26 bytes — tentative; only bytes 10-13 are decoded."""
    d = [0x00] * 26
    d[0:2] = u16(st["cell_min"])
    d[2:6] = [0x02, 0x87, 0x01, 0x68]
    d[6:10] = [0x00, 0x07, 0x5A, 0x53]
    d[10:12] = u16(st["cell_max"])
    d[12:14] = u16(st["cell_min"])
    d[14:20] = [0x00, 0x03, 0xB4, 0x00, 0x04, 0x80]
    d[25] = 0x0B
    return d


def group04(st):
    """14 bytes: 4 x (u16 raw, s8 °C), then the mean °C."""
    d = []
    for c in st["temps_c"][:4]:
        d += u16(temp_raw(c)) + [s8(c)]
    d += [s8(int(sum(st["temps_c"]) / len(st["temps_c"]))), 0x00]
    return d


def group05(st):
    """69 bytes. docs/SIGNALS.md 'Group 05 — extended state'."""
    d = [0x00] * 69
    d[0:2] = u16(0x02D0)                     # unknown, static
    d[2:4] = u16(0x0200)
    d[4:6] = u16(0x0199)
    d[6:8] = u16(st["cell_max"])
    d[8:10] = u16(st["cell_min"])
    for n, c in enumerate(st["temps_c"][:4]):
        d[10 + 2 * n:12 + 2 * n] = u16(temp_raw(c))
    d[18:20] = u16(0x0286)                   # unknown, static
    d[20:22] = u16(0xFFFF if st["discharging"] else 0x0000)
    # Bytes 22-23 are documented as s16 / 1024 A, which tops out at ±32 A —
    # far short of a Leaf's real driving current. The reader treats group-01
    # sensor 2 as canonical and uses this only for direction and for the
    # learned offset at idle, so saturating here is the honest encoding of an
    # unresolved scale rather than a silent wrap-around.
    d[22:24] = s16(max(-32768, min(32767, int(round(st["current_a"] * 1024)))))
    d[24:26] = u16(st["insulation_kohm"])
    for n, v in enumerate(st["segment_deltas"][:10]):
        d[26 + 2 * n:28 + 2 * n] = u16(v)
    for n, v in enumerate(st["cell_groups"][:10]):
        d[46 + 2 * n:48 + 2 * n] = u16(v)
    d[66:68] = u16(0xAAAA)                   # unknown, static
    d[68] = 0x00
    return d


def group06(st):
    """24 bytes = 192 bits = 2 bits per cell pair, MSB first."""
    bits = 0
    for n, f in enumerate(st["balancing"][:NUM_CELLS]):
        bits |= (int(f) & 0b11) << (190 - 2 * n)
    return list(bits.to_bytes(24, "big"))


LBC_GROUPS = {"2101": (0x01, group01), "2102": (0x02, group02), "2103": (0x03, group03),
              "2104": (0x04, group04), "2105": (0x05, group05), "2106": (0x06, group06)}


def lbc_response(cmd, st, rx="7BB", sensor_dropout=False):
    ent = LBC_GROUPS.get(cmd.upper())
    if not ent:
        return None
    num, fn = ent
    payload = fn(st, sensor_dropout) if fn is group01 else fn(st)
    return isotp(rx, [0x61, num], payload, FF_PAD)


# ── HVAC amplifier (service 21, 0x744 -> 0x764) ──────────────────────────

# FAN_VOLTS (blower volts per fan speed) lives in model.py now — the load
# table needs it too — and is imported above.


def setpoint_raw(f):
    """Byte 12: °F ≈ 60 + (raw − 111) × 30/62, so raw = 111 + (°F − 60) × 62/30."""
    return max(0, min(255, int(round(111.0 + (f - 60.0) * 62.0 / 30.0))))


def hvac_group10(st):
    """41 bytes; the ISO-TP padding takes it to the 46 the decoder sees."""
    d = [0x00] * 41
    on, ac = st["hvac_on"], st["hvac_ac_on"]
    d[0] = (st["ambient_c"] + 40) & 0xFF
    d[1] = (st["cabin_temp_c"] + 40) & 0xFF
    d[2] = (st["evap_c"] + 40) & 0xFF
    d[3] = st["sunload"] & 0xFF
    d[4] = d[0]
    d[5] = d[1]
    d[7] = 0x2B
    d[10] = 0x80 if (on and ac) else 0x00
    d[11] = (0x80 | FAN_VOLTS[int(st["hvac_fan_speed"])]) if on else 0x00
    d[12] = setpoint_raw(st["hvac_setpoint_f"])
    rpm = st["compressor_rpm"]
    d[21:23] = u16(rpm)
    d[23] = min(255, rpm // 100)
    d[24] = min(255, rpm // 150)
    if on:
        d[25], d[26] = 0x54, 0x8F
        d[32], d[33] = 0x40, (0xA0 if ac else 0x00)
        d[34], d[35] = 0x10, 0x90
        d[37], d[38], d[39] = 0x53, 0x36, 0x36
    heat = int(st["heater_level"])
    d[29] = min(255, heat * 6)
    d[31] = min(255, heat * 3)
    d[36] = min(255, heat)
    return d


HVAC_STATIC = {
    "2100": [0x80, 0x01, 0x80, 0x00],
    "2101": [0xFD, 0xAE, 0x00, 0x00, 0xFB, 0xFF, 0xF8, 0xFF, 0xF1, 0xBF],
    "2111": [0x06, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0A, 0x0A],
}


def hvac_response(cmd, st, rx="764"):
    """None means 'this ECU does not implement that group' — the real amp
    answers NRC 0x12 (subFunctionNotSupported) for everything but 00/01/10/11/82/83."""
    c = cmd.upper()
    if c == "2110":
        return isotp(rx, [0x61, 0x10], hvac_group10(st), FF_PAD)
    if c in HVAC_STATIC:
        return isotp(rx, [0x61, int(c[2:], 16)], HVAC_STATIC[c], FF_PAD)
    return None


# ── Car-CAN passive frames ───────────────────────────────────────────────

GEAR_CODES = {"P": 0x08, "R": 0x10, "N": 0x18, "D": 0x20, "Eco": 0x38}
GEAR_174 = {"P": 0xAA, "N": 0xAA, "R": 0x99, "D": 0xBB, "Eco": 0xBB}
TURN_CODES = {"off": 0x80, "left": 0x82, "right": 0x84, "hazards": 0x86}
# DOOR_BITS is imported from model.py (the record builder shares it)
START_CODES = {"off": 0, "acc": 1, "on": 2, "ready": 3}

# broadcast period in seconds, used to decide how many lines a capture window
# of N seconds should contain
FRAME_PERIOD = {"421": 0.010, "174": 0.010, "284": 0.010, "180": 0.010,
                "292": 0.020, "358": 0.100, "60D": 0.100, "5C5": 0.100,
                "5A9": 0.100, "355": 0.100, "5B3": 0.100, "625": 0.100,
                "385": 1.000}
MAX_FRAMES = 24        # unfiltered ATMA overflows around here on a real adapter

# how many data bytes each frame actually carries on this car, from the
# captures in tests/fixtures/ — some are short, and the decoders' min_len
# checks in last_complete_frame() exist because of it
FRAME_DLC = {"421": 3, "355": 7, "625": 6}


def frame_bytes(can_id, st):
    """The 8 data bytes of one broadcast frame, or None if we do not model it."""
    cid = can_id.upper()
    k = st

    if cid == "421":
        return [GEAR_CODES.get(k["gear"], 0x08), 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

    if cid == "174":
        return [0x00, 0x00, 0x00, GEAR_174.get(k["gear"], 0xAA), 0x02, 0x00, 0x00, 0x00]

    if cid == "358":
        return [0x00, 0x00, TURN_CODES.get(k["turn_signal"], 0x80), 0x00, 0x00, 0x00, 0x00, 0x00]

    if cid == "385":
        psi = k["tpms_psi"]
        return [0x00, 0x00] + [max(0, min(255, int(round(p * 4)))) for p in psi] + [0x00, 0xF0]

    if cid == "5C5":
        odo = int(k["odometer_mi"]) if k["units_miles"] else int(round(k["odometer_mi"] / 0.621371))
        return [(0x40 | (0x04 if k["handbrake"] else 0x00)),
                (odo >> 16) & 0xFF, (odo >> 8) & 0xFF, odo & 0xFF, 0x00, 0x0C, 0x00, 0x00]

    if cid == "5A9":
        raw12 = max(0, min(0xFFF, int(round(k["range_km"] * 5))))
        return [0x77, (raw12 >> 4) & 0xFF, (raw12 & 0x0F) << 4, 0x0B, 0x42, 0x20, 0x00, 0x00]

    if cid == "355":
        return [0x00, 0x00, 0x00, 0x00, 0x20, 0x00, (0x60 if k["units_miles"] else 0x40), 0x00]

    if cid == "5B3":
        soh = max(0, min(127, int(round(k["soh"]))))
        return [0x94, (soh << 1) & 0xFE, 0x05, 0x01, 0xE0, 0x4D, 0x31, 0x0A]

    if cid == "284":
        raw = max(0, min(0xFFFF, int(round(k["speed_kmh"] * 100))))
        return [0x00, 0x00, 0x00, 0x00, (raw >> 8) & 0xFF, raw & 0xFF, 0x9A, 0x20]

    if cid == "292":
        return [0x7E, 0xC8, 0x28, 0x80, 0x20, 0x00,
                max(0, min(255, int(round(k["brake_pct"] * 1.39)))), 0x00]

    if cid == "180":
        return [0x00, 0x00, 0x00, 0x00, 0x00,
                max(0, min(255, int(round(k["accel_pedal_pct"] * 2)))), 0x21, 0x00]

    if cid == "60D":
        b0 = 0x00
        for d in k["doors_open"]:
            b0 |= DOOR_BITS.get(d, 0)
        if k["parking_lights"]:
            b0 |= 0x04
        if k["headlights"]:
            b0 |= 0x02
        b1 = (START_CODES.get(k["start_state"], 0) & 0x03) << 1
        if k["high_beam"]:
            b1 |= 0x08
        if k["fog_lights"]:
            b1 |= 0x01
        return [b0, b1, 0x18 if k["locked"] else 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

    if cid == "625":
        b1 = 0x00
        if k["parking_lights"]:
            b1 |= 0x40
        if k["headlights"]:
            b1 |= 0x20
        if k["high_beam"]:
            b1 |= 0x10
        if k["fog_lights"]:
            b1 |= 0x08
        return [0x02, b1, 0xFF, 0x1D, 0x20, 0x00, 0x00, 0x00]

    return None


def frame_line(can_id, st):
    b = frame_bytes(can_id, st)
    if b is None:
        return None
    return line(can_id, b[:FRAME_DLC.get(can_id.upper(), 8)])


def frame_count(can_id, secs):
    per = FRAME_PERIOD.get(can_id.upper(), 0.1)
    return max(1, min(MAX_FRAMES, int(round(float(secs) / per))))
