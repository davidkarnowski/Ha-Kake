# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Which bytes are the answer.

A request goes out and frames come back. On an ELM327 the adapter pairs the
two itself: it prints what arrived before its prompt and nothing else. On a
shared CAN bus, and more so across a broker, nobody does. The response id is
shared by every requester and every request, a link can deliver a reply ten
seconds late, and "whatever arrived while I was waiting" is then the answer to
something else. On 2026-11-05 that stored a state of charge of 419 %: a
group-05 reply read as group 01.

This module is the one place that decides, for every transport, and it never
throws anything away. Given a request and what came back, ``resolve()`` says
which message is the answer and hands back every other message it saw, whole,
so the caller can account for it: a late reply to an earlier request of its
own, another requester's reply, a fragment.

Two kinds of evidence, in order of strength:

  the acknowledgement   A bridge that advertises ``ack-reply`` returns the
                        reassembled reply inside the ack for that request,
                        with the request echoed. The ack topic belongs to one
                        request, so this is identity, not inference.
  the frames            Without that, the frames in the window are reassembled
                        into ISO-TP messages, strictly (first frame, sequence
                        numbers, declared length), and the answer is the last
                        complete message whose service echo matches the
                        request. Everything else in the window is a stray.

Pure functions, no I/O, no vehicle: ids and bytes in, a decision out.
"""

NEGATIVE = 0x7F
# How many bytes after the service byte a positive reply repeats from its request.
ECHO_BYTES = {0x21: 1, 0x22: 2, 0x01: 1, 0x02: 2, 0x09: 1, 0x1A: 1}


def hex_bytes(data):
    return " ".join(f"{b:02X}" for b in data)


def parse_hex(text):
    """'61 01 FF' (or '6101FF') -> bytes; None when it is not hex bytes."""
    if not isinstance(text, str):
        return None
    compact = "".join(text.split())
    if len(compact) % 2:
        return None
    try:
        return bytes.fromhex(compact)
    except ValueError:
        return None


# ── what a reply must look like ──────────────────────────────────────────

def expected_echo(request):
    """The bytes a positive reply to `request` starts with: the service plus
    0x40, then whatever of the request that service repeats."""
    request = bytes(request or b"")
    if not request:
        return b""
    n = ECHO_BYTES.get(request[0], 0)
    return bytes([(request[0] + 0x40) & 0xFF]) + request[1:1 + n]


def answers(payload, request):
    """True when `payload` is a positive reply to `request`, as far as its echo says."""
    echo = expected_echo(request)
    return bool(echo) and bytes(payload or b"")[:len(echo)] == echo


def negative_code(payload, request):
    """The response code when `payload` is the ECU refusing `request` (7F <service> <code>), else None."""
    payload, request = bytes(payload or b""), bytes(request or b"")
    if len(payload) >= 3 and request and payload[0] == NEGATIVE and payload[1] == request[0]:
        return payload[2]
    return None


def request_of(payload):
    """The request a reply answers, read back from its echo: b'\\x61\\x05...' -> b'\\x21\\x05'.
    None for a negative reply, an empty one, or a byte that cannot be a positive service echo."""
    payload = bytes(payload or b"")
    if not payload or payload[0] == NEGATIVE or payload[0] < 0x40:
        return None
    service = payload[0] - 0x40
    n = ECHO_BYTES.get(service, 0)
    if len(payload) < 1 + n:
        return None
    return bytes([service]) + payload[1:1 + n]


# ── ISO-TP, strictly ─────────────────────────────────────────────────────

class Message:
    """One ISO-TP message as it came off the bus: the payload (service echo
    included, cut to its declared length), the lines that carried it, and
    whether all of it arrived."""

    __slots__ = ("rx", "payload", "declared", "complete", "lines")

    def __init__(self, rx, payload, declared, complete, lines):
        self.rx, self.payload, self.declared = rx, bytes(payload), declared
        self.complete, self.lines = bool(complete), list(lines)

    @property
    def frames(self):
        return len(self.lines)

    def __repr__(self):
        state = "complete" if self.complete else "incomplete"
        return f"<Message {self.rx} {hex_bytes(self.payload[:4])}… {len(self.payload)}/{self.declared} B {state}>"


def _line_bytes(line, rx_id):
    parts = str(line).strip().split()
    if len(parts) < 2 or parts[0].upper() != rx_id:
        return None
    try:
        return [int(b, 16) for b in parts[1:]]
    except ValueError:
        return None


def messages(lines, rx_id):
    """Every ISO-TP message in `lines` (ELM style: ``7BB 10 29 61 01 …``) on
    `rx_id`, in order. A single frame is one message. A first frame opens one
    and consecutive frames continue it only in sequence; a frame that does not
    fit closes what was open (incomplete) and is kept as a fragment of its
    own. Nothing is dropped: every line on `rx_id` ends up in exactly one message."""
    rx_id = str(rx_id).upper()
    out, cur = [], None             # cur: [payload bytearray, declared, next sequence number, lines]

    def close(complete):
        nonlocal cur
        if cur is not None:
            payload = bytes(cur[0][:cur[1]]) if complete else bytes(cur[0])
            out.append(Message(rx_id, payload, cur[1], complete, cur[3]))
            cur = None

    for line in lines or ():
        b = _line_bytes(line, rx_id)
        if b is None:
            continue
        kind = b[0] >> 4
        if kind == 0:                                   # single frame: 0L d…
            close(False)
            n = b[0] & 0x0F
            data = b[1:1 + n]
            out.append(Message(rx_id, data, n, n > 0 and len(data) == n, [line]))
        elif kind == 1 and len(b) >= 2:                 # first frame: 1L LL d…
            close(False)
            declared = ((b[0] & 0x0F) << 8) | b[1]
            cur = [bytearray(b[2:]), declared, 1, [line]]
            if len(cur[0]) >= declared:
                close(True)
        elif kind == 2:                                 # consecutive frame: 2N d…
            if cur is not None and (b[0] & 0x0F) == cur[2]:
                cur[0].extend(b[1:])
                cur[2] = (cur[2] + 1) & 0x0F
                cur[3].append(line)
                if len(cur[0]) >= cur[1]:
                    close(True)
            else:                                       # out of sequence, or nothing open: a fragment
                close(False)
                out.append(Message(rx_id, b[1:], None, False, [line]))
        elif kind == 3:                                 # flow control belongs to the other direction
            continue
        else:
            close(False)
            out.append(Message(rx_id, b[1:], None, False, [line]))
    close(False)
    return out


def lines_from_payload(rx_id, payload, pad=b""):
    """The ELM-style lines that carry `payload` as one ISO-TP message.

    ISO-TP framing is fixed, so the only thing a reassembled reply has lost is
    what the ECU filled its last frame with. Given that (`pad`, which a bridge
    reports in its ack) the lines are the frames exactly as the bus carried
    them; without it the last line simply ends where the reply does.
    ``parse_isotp`` reads either the way it reads frames off the bus."""
    rx_id, payload = str(rx_id).upper(), bytes(payload)
    if len(payload) <= 7:
        frames = [bytes([len(payload)]) + payload]
    elif len(payload) > 0xFFF:
        raise ValueError(f"{len(payload)} bytes do not fit a classic ISO-TP message")
    else:
        frames = [bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]]
        sn, i = 1, 6
        while i < len(payload):
            frames.append(bytes([0x20 | sn]) + payload[i:i + 7])
            sn, i = (sn + 1) & 0x0F, i + 7
    frames[-1] = (frames[-1] + bytes(pad or b""))[:8]
    return [f"{rx_id} {hex_bytes(f)}" for f in frames]


# ── the decision ─────────────────────────────────────────────────────────

class Exchange:
    """What one request came to.

    lines      what to hand the decoder: the answer's lines, or None
    via        "ack" (the bridge returned the reply for this request) or
               "frames" (picked from the window by its echo); None with no answer
    payload    the proven reply, service echo included
    negative   the response code when the ECU itself refused the request. That
               is still the car's answer to this request, so `lines` carries it
               (``7BB 03 7F 21 12``), exactly as an adapter would print it
    problem    why there is no answer, when there is none: the ack's error
               ("timeout", "superseded", …), "no-reply", "incomplete",
               "echo-mismatch" or "ack-mismatch"
    strays     every other message seen in the window, whole (Message objects)
    mirror     ack path only: True when the frames mirrored on rx/<id>
               reassemble to the same reply, False when they do not, None when
               no frames were seen
    """

    __slots__ = ("lines", "via", "payload", "negative", "problem", "strays", "mirror")

    def __init__(self, lines=None, via=None, payload=None, negative=None, problem=None, strays=(), mirror=None):
        self.lines, self.via, self.payload = lines, via, payload
        self.negative, self.problem, self.strays, self.mirror = negative, problem, list(strays), mirror

    @property
    def ok(self):
        return self.lines is not None

    def __repr__(self):
        what = f"answer via {self.via}" if self.ok else f"no answer ({self.problem})"
        if self.negative is not None:
            what += f", the ECU refused (0x{self.negative:02X})"
        return f"<Exchange {what}, {len(self.strays)} stray>"


def _same_id(a, b):
    try:
        return int(str(a), 16) == int(str(b), 16)
    except (TypeError, ValueError):
        return False


def resolve(request, rx_id, ack, lines, tx_id=None):
    """Decide what `request` (bytes) to `tx_id`, answered on `rx_id`, came to,
    from the bridge's `ack` (a dict, possibly without a reply) and the frame
    `lines` captured on `rx_id` while the request was out."""
    request = bytes(request or b"")
    rx_id = str(rx_id).upper()
    ack = ack if isinstance(ack, dict) else {}
    seen = messages(lines, rx_id)

    if not ack.get("ok"):
        return Exchange(problem=str(ack.get("error") or "no-ack"), strays=seen)

    reply = parse_hex(ack.get("reply")) if "reply" in ack else None
    if reply is not None:
        # The bridge says: this is the reply to that request. Check it says so
        # about *our* request, then believe it over anything in the window.
        mirror = None
        if seen:
            mirror = any(m.complete and m.payload == reply for m in seen)
        strays = [m for m in seen if not (m.complete and m.payload == reply)]
        said = parse_hex(ack.get("request")) if "request" in ack else None
        if (said is not None and said != request) \
                or ("rx" in ack and not _same_id(ack["rx"], rx_id)) \
                or (tx_id is not None and "tx" in ack and not _same_id(ack["tx"], tx_id)):
            return Exchange(problem="ack-mismatch", payload=reply, strays=strays, mirror=mirror)
        code = negative_code(reply, request)
        if code is None and not answers(reply, request):
            return Exchange(problem="echo-mismatch", payload=reply, strays=strays, mirror=mirror)
        pad = parse_hex(ack.get("pad")) if "pad" in ack else None
        return Exchange(lines_from_payload(rx_id, reply, pad or b""), "ack", reply, negative=code,
                        strays=strays, mirror=mirror)

    # No reply in the ack (a bridge that predates ack-reply, or a local bus):
    # the window is all there is. The answer is the last complete message that
    # echoes this request; the bridge serialises requests, so anything ahead of
    # it in the window belongs to an earlier request, ours or someone else's.
    fits = [m for m in seen if m.complete and (answers(m.payload, request)
                                                or negative_code(m.payload, request) is not None)]
    if not fits:
        if not seen:
            problem = "no-reply"
        elif any(not m.complete for m in seen):
            problem = "incomplete"
        else:
            problem = "echo-mismatch"
        return Exchange(problem=problem, strays=seen)
    pick = fits[-1]
    want = ack.get("frames")
    if isinstance(want, int) and not isinstance(want, bool) and pick.frames != want:
        sized = [m for m in fits if m.frames == want]
        if sized:
            pick = sized[-1]
    strays = [m for m in seen if m is not pick]
    return Exchange(list(pick.lines), "frames", pick.payload,
                    negative=negative_code(pick.payload, request), strays=strays)
