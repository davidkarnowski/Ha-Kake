# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Link accounting: what every request came to, and what became of every reply.

A reader that reaches its car through a broker and a tunnel cannot assume
that what it asked for is what came back, or when. This module keeps the
account. For each request it records which reply was taken as the answer, on
what evidence, and where the time went (waiting on the bridge, on the CAN
bus, in the network). For every other reply that turned up it decides what
that reply is, and nothing is thrown away:

  late       the answer to a request this reader had given up on. It is a
             good reading that arrived late: it is credited to its own item
             and marked, never to the request that happened to be waiting.
  foreign    a complete reply nobody here asked for (another requester on the
             same bridge). Recorded with its bytes, decoded for no item.
  fragment   part of a reply. Recorded.

and for the request itself: a refusal by the ECU, a reply whose echo is not
the request's, an ack about some other request. Each of these, and each
value outside its physical limits, gets one line in the reply ledger
(``<data>/reply_ledger.jsonl``) with the raw bytes: the record capture, made
at the moment it happens.

No vehicle is named here and nothing is decoded here. The reader supplies
`find_item(bus, rx_id, request_bytes) -> item id or None` and does the
decoding; this module keeps the books. `Ledger` writes; `LinkAccount` counts
and classifies; both are plain objects with injectable clocks so they are
tested without a reader.
"""
import collections
import json
import os
import time

import replyproof

LEDGER_MAX_BYTES = 4 * 1024 * 1024     # the ledger rolls to <name>.1 at this size; one old file is kept
REPEAT_EVERY = 30.0                    # seconds before the same kind of entry for the same thing is written again
ABANDONED_FOR = 120.0                  # seconds a request given up on can still be credited late
QUIET = ("timeout",)                   # request outcomes that are counted but not ledgered: a sleeping car is not news
REFUSALS = ("superseded", "duplicate", "expired", "busy", "refused", "offline")


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


class Ledger:
    """An append-only JSON-lines file, capped in size. Never raises: an account
    that cannot be written must not take the reader down with it."""

    def __init__(self, path, max_bytes=LEDGER_MAX_BYTES, clock=time.time):
        self.path, self.max_bytes, self.clock = path, max_bytes, clock
        self.written = 0
        self.failed = 0

    def add(self, kind, **fields):
        """One entry. `kind` is its class (late, foreign, …); returns the entry."""
        t = self.clock()
        entry = {"t": round(t, 3), "ts": _iso(t), "class": kind}
        entry.update({k: v for k, v in fields.items() if v is not None})
        if not self.path:
            return entry
        try:
            try:
                if os.path.getsize(self.path) >= self.max_bytes:
                    os.replace(self.path, self.path + ".1")
            except OSError:
                pass
            with open(self.path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
            self.written += 1
        except OSError:
            self.failed += 1
        return entry

    def tail(self, n=100):
        """The newest `n` entries, oldest first."""
        return tail(self.path, n)


def tail(path, n=100):
    """The newest `n` entries of a ledger file, oldest first; [] when there is none."""
    n = max(0, int(n))
    if not n or not path:
        return []
    lines = collections.deque(maxlen=n)
    try:
        with open(path) as f:
            for line in f:
                lines.append(line)
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def split_rtt(rtt_ms, ack):
    """Where a round trip went, from durations alone (no clock is compared with
    another): `queue_ms` waiting on the bridge, `bus_ms` on the CAN bus, and
    `net_ms`, the rest, which is the broker and the network both ways."""
    out = {}
    for key in ("queue_ms", "bus_ms"):
        v = ack.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[key] = v
    if len(out) == 2 and isinstance(rtt_ms, (int, float)):
        out["net_ms"] = round(max(0.0, rtt_ms - out["queue_ms"] - out["bus_ms"]), 1)
    return out


class LinkAccount:
    """The books for one reader. Call `begin_cycle()`, then `exchange()` for
    every request record a transport kept (`CanFacade.take_exchanges()`) and
    `late_ack()` for every late one (`take_late()`); `take_credits()` hands
    back the late replies to decode, and `block()` is what goes in the record."""

    def __init__(self, find_item, ledger=None, mono=time.monotonic):
        self.find_item = find_item
        self.ledger = ledger or Ledger(None)
        self.mono = mono
        self.totals = collections.Counter()
        self.cycle = collections.Counter()
        self.items = {}                    # item → this cycle's entry
        self.via = None                    # how the last answer was proven: "ack" | "frames"
        self.caps = []
        self._abandoned = {}               # (bus, item) → monotonic time it was given up on
        self._asked = {}                   # (bus, item) → monotonic time that request was sent
        self._credits = []
        self._said = {}                    # (kind, thing) → monotonic time of the last ledger line
        self.last_stall = None             # {"t", "item", "late_s"}: the newest late reply

    # ── bookkeeping ──────────────────────────────────────────────────────

    def begin_cycle(self):
        self.cycle = collections.Counter()
        self.items = {}

    def _count(self, what, n=1):
        self.cycle[what] += n
        self.totals[what] += n

    def _note(self, kind, thing, **fields):
        """A ledger line, at most once per REPEAT_EVERY for the same kind and thing."""
        now = self.mono()
        last = self._said.get((kind, thing))
        if last is not None and now - last < REPEAT_EVERY:
            return None
        self._said[(kind, thing)] = now
        if len(self._said) > 512:
            for key in [k for k, t in self._said.items() if now - t >= REPEAT_EVERY]:
                del self._said[key]
        return self.ledger.add(kind, **fields)

    @staticmethod
    def _ack_summary(ack):
        return {k: ack[k] for k in ("req", "ok", "error", "reason", "frames", "bytes", "src", "seq", "request",
                                    "queue_ms", "bus_ms", "t_in", "t_done") if k in ack}

    def given_up_on(self, bus, item):
        """Monotonic time `item` was last given up on, if that is recent enough to credit a late reply."""
        t = self._abandoned.get((bus, item))
        if t is None or self.mono() - t > ABANDONED_FOR:
            self._abandoned.pop((bus, item), None)
            self._asked.pop((bus, item), None)
            return None
        return t

    # ── one request ──────────────────────────────────────────────────────

    def exchange(self, bus, item, rec, caps=()):
        """Account for one request record; returns this cycle's entry for `item`."""
        ack = rec.get("ack") if isinstance(rec.get("ack"), dict) else {}
        self.caps = list(caps or [])
        self._count("sent")
        entry = {"rtt_ms": rec.get("rtt_ms")}
        if ack.get("req"):
            entry["req"] = ack["req"]
        entry.update(split_rtt(rec.get("rtt_ms"), ack))
        if isinstance(ack.get("t_done"), (int, float)) and not isinstance(ack.get("t_done"), bool):
            entry["t_bus"] = ack["t_done"]
        request = replyproof.hex_bytes(rec.get("request") or b"")
        payload = rec.get("payload")
        raw = replyproof.hex_bytes(payload) if payload else None

        if rec.get("ok"):
            entry["via"] = self.via = rec.get("via")
            if rec.get("negative") is not None:
                entry["state"], entry["nrc"] = "negative", rec["negative"]
                self._count("negative")
                self._note("negative", (bus, item, rec["negative"]), bus=bus, item=item, request=request, reply=raw,
                           nrc=f"0x{rec['negative']:02X}", ack=self._ack_summary(ack),
                           action="the ECU refused the request; handed on as its answer")
            else:
                entry["state"] = "ok"
                self._count("ok")
                # (bus, item) stays in _abandoned: if the reply to the request
                # given up on still turns up it is ours and late, not foreign.
                # Whether it is still worth anything is the reader's call.
        else:
            problem = rec.get("problem") or "no-answer"
            entry["state"] = problem
            if problem == "timeout":
                self._count("timeouts")
                now = self.mono()
                waited = rec.get("rtt_ms")
                self._abandoned[(bus, item)] = now
                self._asked[(bus, item)] = now - (waited / 1000.0 if isinstance(waited, (int, float)) else 0.0)
            elif problem in REFUSALS:
                self._count(problem)
            else:
                self._count("unproven")
                self._note(problem, (bus, item), bus=bus, item=item, request=request, reply=raw,
                           window=[replyproof.hex_bytes(m.payload) for m in rec.get("strays") or ()][:8],
                           ack=self._ack_summary(ack),
                           action="not decoded: nothing in hand is provably the answer to this request")

        if rec.get("mirror") is False:
            self._count("mirror_mismatch")
            self._note("mirror", (bus, item), bus=bus, item=item, request=request, reply=raw, ack=self._ack_summary(ack),
                       action="the ack's reply was used; the frames mirrored on the response id did not match it")

        # Everything else that arrived while this request was out. With a
        # bridge that returns replies in its acks these are only a mirror of
        # other traffic (our own late replies come back through late acks), so
        # they are counted. Without one they are all the evidence there is.
        strays = rec.get("strays") or ()
        if "reply" in ack or "ack-reply" in self.caps:
            if strays:
                self._count("mirrored_other", len(strays))
        else:
            for m in strays:
                self._stray(bus, rec.get("rx", ""), m)
        self.items[item] = entry
        return entry

    def _stray(self, bus, rx, m):
        raw = replyproof.hex_bytes(m.payload)
        if not m.complete:
            self._count("fragments")
            self._note("fragment", (bus, rx), bus=bus, rx=rx, reply=raw, frames=m.frames, declared=m.declared,
                       action="recorded: part of a reply, not enough to say whose")
            return
        asked = replyproof.request_of(m.payload)
        item = self.find_item(bus, rx, asked) if asked else None
        given_up = self.given_up_on(bus, item) if item else None
        if item and given_up is not None:
            self._abandoned.pop((bus, item), None)
            sent = self._asked.pop((bus, item), given_up)
            # how late: from when it was asked for to now, which is what the car's answer took to get here
            self._credit(bus, item, m.lines, m.payload, late_s=self.mono() - sent, given_up=given_up,
                         t_src=None, via="frames", request=replyproof.hex_bytes(asked))
        else:
            self._count("foreign")
            self._note("foreign", (bus, rx, raw[:5]), bus=bus, rx=rx, item=item,
                       request=replyproof.hex_bytes(asked) if asked else None, reply=raw, frames=m.frames,
                       action="recorded, decoded for no item: a reply this reader did not ask for")

    # ── an ack that came after its request was given up on ───────────────

    def late_ack(self, bus, rec):
        ack = rec.get("ack") if isinstance(rec.get("ack"), dict) else {}
        request = rec.get("request") or b""
        item = self.find_item(bus, str(rec.get("rx", "")).upper(), request)
        if not ack.get("ok"):
            error = str(ack.get("error") or "error")
            self._count("late_" + error if error in REFUSALS else "late_failed")
            return
        if not rec.get("lines") or item is None:
            self._count("late_acks")          # an older bridge: the reply itself is in the frames
            return
        given_up = self._abandoned.pop((bus, item), None)
        self._asked.pop((bus, item), None)
        waited = rec.get("waited_s")
        waited = waited if isinstance(waited, (int, float)) else 0.0
        took = rec.get("t", 0) - rec.get("t_asked", 0) if isinstance(rec.get("t_asked"), (int, float)) \
            and isinstance(rec.get("t"), (int, float)) else waited
        self._credit(bus, item, rec["lines"], rec.get("payload"),
                     late_s=max(took, waited),
                     given_up=given_up if given_up is not None else self.mono() - waited,
                     t_src=ack.get("t_done"), via="ack", request=replyproof.hex_bytes(request), ack=ack)

    def _credit(self, bus, item, lines, payload, late_s, given_up, t_src, via, request, ack=None):
        self._count("late")
        self._credits.append({"bus": bus, "item": item, "lines": list(lines), "payload": payload,
                              "late_s": round(float(late_s), 3), "given_up": given_up, "t_src": t_src, "via": via,
                              "request": request, "ack": self._ack_summary(ack or {})})

    def take_credits(self):
        """Late replies to decode, each for its own item, oldest first."""
        out, self._credits = self._credits, []
        return out

    def credited(self, credit, applied, wall):
        """Say what became of a late reply (`applied`: its values went into the record)."""
        self.last_stall = {"t": round(wall, 3), "item": credit["item"], "late_s": credit["late_s"]}
        if applied:
            self._count("late_credited")
            entry = self.items.setdefault(credit["item"], {})
            entry["late_s"] = credit["late_s"]
            entry.setdefault("state", "late")       # an item not asked this cycle: all it has is the late reply
        self.ledger.add("late", bus=credit["bus"], item=credit["item"], request=credit["request"],
                        reply=replyproof.hex_bytes(credit["payload"]) if credit.get("payload") else None,
                        late_s=credit["late_s"], t_src=credit.get("t_src"), via=credit["via"],
                        ack=credit.get("ack") or None,
                        action=(f"credited to {credit['item']}, {credit['late_s']:.1f} s late, and marked late" if applied
                                else f"kept here only: a newer reading of {credit['item']} was already in"))

    def flagged(self, bus, key, value, limits, item=None, lines=None):
        """A value outside its physical limits: stored, marked, and its bytes kept here."""
        self._count("flagged")
        self._note("out-of-limits", (bus, key), bus=bus, item=item, key=key, value=value, limits=list(limits),
                   frames=list(lines)[:40] if lines else None,
                   action="stored with a quality flag, not dropped")

    # ── what goes in the record ──────────────────────────────────────────

    def block(self):
        out = {"via": self.via, "items": dict(self.items),
               "cycle": {k: v for k, v in sorted(self.cycle.items()) if v},
               "totals": {k: v for k, v in sorted(self.totals.items()) if v}}
        if self.caps:
            out["caps"] = list(self.caps)
        if self.last_stall:
            out["last_stall"] = dict(self.last_stall)
        return out
