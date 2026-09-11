#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The raw output console — the ring, the decimation and the file (docs/CONSOLE.md).

What this is
------------
A **window** on what the transport is saying, not a capture tool. `record_session.py`
records a session properly and the MQTT bridge logs frames losslessly; this drops
frames on purpose and says how many. Nothing here is evidence.

The three pieces, none of which know what a vehicle is:

  `Ring`      a fixed-capacity, thread-safe ring of entries with a monotonically
              increasing sequence number. The sequence number is the API's cursor:
              two frames can share a millisecond, so a timestamp cannot be one.
              Decimation happens here — on the way *in*, in the reader's process —
              because at 1,700 frames a second nothing useful reaches a browser.
  `ConsoleLog` appends kept entries to a rolling JSON-lines file beside the state
              file, size-capped, and reads a filtered tail back out of it. That is
              the state file's pattern (one writer process, one reader process, a
              file between them) and it needs no new IPC.
  `Console`   the two together plus the options the tile asked for, which is what
              the reader holds and the taps call.

An entry
--------
    {"seq": 41, "t": 1234.5, "wall": 1789..., "bus": "car",
     "kind": "frame", "id": "421", "text": "421 08 00 00"}

`t` is monotonic (ordering, ages), `wall` is epoch seconds (what a human reads).
`kind` is one of KINDS; `id` is the CAN id where one is meaningful and "" where it
is not (a reader event has no id). `text` is the line as the transport gave it —
for a frame, the exact `ID B0 B1 …` shape the decoders and the fixtures use, so a
line copied out of the pane can be pasted into a fixture unchanged.

The one rule that outranks the rest: the console is read-only. Nothing in this
module sends anything, and nothing in it can be made to — there is no transport
handle here at all, only text that has already arrived.
"""

import json
import os
import threading
import time

# The kinds an entry can have. `frame` is the firehose; the other four are rare
# enough that they are never decimated (a kind filter in the tile is what makes
# the rate problem go away for most users — see docs/CONSOLE.md).
KINDS = ("frame", "uds", "adapter", "text", "event")

CAPACITY = 4000            # entries held in memory; a few thousand, per the plan
RATE_CAP = 10              # frames of one id kept per second, before dropping
FILE_MAX = 2_000_000       # bytes; the JSONL file is truncated to the newest half
TEXT_MAX = 240             # one entry's text, clipped (a BUFFER FULL dump is long)
LIMIT_DEFAULT = 200
LIMIT_MAX = 2000


def clip(text):
    t = " ".join(str(text or "").split())
    return t[:TEXT_MAX]


class Ring:
    """A bounded ring of console entries, with the decimation on the way in.

    Thread-safe: frames arrive on a python-can notifier thread while the
    reader's own loop adds UDS answers and events.

    Decimation (`ids` = the ids the enabled tiles poll, `everything` = the lossy
    mode that says it is lossy):

      * every kind but `frame` is always kept — they are answers and events, not
        a stream, and dropping one loses the thing a person was watching for;
      * a `frame` whose id is not in `ids` is dropped unless `everything`;
      * a `frame` of an id already seen `rate_cap` times in this second is
        dropped.

    Every drop is counted, per id, and the counts are published: a pane that
    silently thinned its own data would be worse than no pane.
    """

    def __init__(self, capacity=CAPACITY, rate_cap=RATE_CAP, ids=None, everything=False):
        self.capacity = max(1, int(capacity))
        self.rate_cap = max(1, int(rate_cap))
        self.ids = {str(i).upper() for i in (ids or ())}
        self.everything = bool(everything)
        self.lock = threading.Lock()
        self._buf = []             # oldest first; len <= capacity
        self.seq = 0               # last sequence number handed out (0 = nothing yet)
        self.kept = 0
        self.dropped = 0
        self.dropped_by_id = {}
        self.evicted = 0           # entries pushed out of the ring by newer ones
        self._sec = None           # the second the per-id counters belong to
        self._sec_counts = {}
        self._drained = 0          # the last seq handed to the file writer

    # ── the way in ───────────────────────────────────────────────────────

    def wants(self, kind, cid):
        """Would `add` keep this? Cheap enough for the per-frame hot path, and
        it does not touch the rate counters — `add` is still the decider."""
        if kind != "frame":
            return True
        return self.everything or str(cid or "").upper() in self.ids

    def add(self, kind, text, cid="", bus="", t=None, wall=None):
        """Append an entry, or drop it and count the drop. Returns the entry
        (a copy the caller may keep) or None."""
        kind = kind if kind in KINDS else "event"
        cid = str(cid or "").upper()
        now = time.monotonic() if t is None else float(t)
        with self.lock:
            if kind == "frame" and not self._allow_frame(cid, now):
                self.dropped += 1
                self.dropped_by_id[cid] = self.dropped_by_id.get(cid, 0) + 1
                return None
            self.seq += 1
            e = {"seq": self.seq, "t": round(now, 4),
                 "wall": round(time.time() if wall is None else float(wall), 3),
                 "bus": str(bus or ""), "kind": kind, "id": cid, "text": clip(text)}
            self._buf.append(e)
            self.kept += 1
            if len(self._buf) > self.capacity:
                del self._buf[:len(self._buf) - self.capacity]
                self.evicted += 1
            return dict(e)

    def _allow_frame(self, cid, now):
        """The id filter and the per-id, per-second cap. Called under the lock."""
        if not self.everything and cid not in self.ids:
            return False
        sec = int(now)
        if sec != self._sec:
            self._sec = sec
            self._sec_counts = {}
        n = self._sec_counts.get(cid, 0)
        if n >= self.rate_cap:
            return False
        self._sec_counts[cid] = n + 1
        return True

    # ── the way out ──────────────────────────────────────────────────────

    def entries(self, since=0, kind=None, ids=None, limit=LIMIT_DEFAULT):
        """Entries after the cursor `since`, oldest first. `kind` and `ids` are
        iterables of the values to keep (None = all); `limit` caps the answer to
        the *newest* matches, because a pane that fell behind wants the end of
        the stream, not the start of a backlog."""
        with self.lock:
            rows = list(self._buf)
        return select(rows, since=since, kind=kind, ids=ids, limit=limit)

    def drain(self):
        """Entries added since the last drain, for the writer. An entry the ring
        evicted before a drain is gone — the file is the ring's tail, not a
        second, longer memory of it (docs/CONSOLE.md: this is a window)."""
        with self.lock:
            out = [e for e in self._buf if e["seq"] > self._drained]
            self._drained = self.seq
        return out

    def stats(self):
        """What the pane must be told: how much was kept, how much was thrown
        away and per which id, and under which rules."""
        with self.lock:
            worst = sorted(self.dropped_by_id.items(), key=lambda kv: -kv[1])[:8]
            return {"cursor": str(self.seq), "kept": self.kept, "dropped": self.dropped,
                    "dropped_by_id": dict(worst), "held": len(self._buf),
                    "capacity": self.capacity, "evicted": self.evicted,
                    "rate_cap": self.rate_cap, "everything": self.everything,
                    "ids": sorted(self.ids)}


def select(rows, since=0, kind=None, ids=None, limit=LIMIT_DEFAULT):
    """The filter both the ring and the file answer with — pure, so the endpoint
    and the ring cannot disagree about what `since` means."""
    try:
        since = int(since or 0)
    except (TypeError, ValueError):
        since = 0
    limit = max(1, min(int(limit or LIMIT_DEFAULT), LIMIT_MAX))
    kinds = {str(k) for k in kind} if kind else None
    want = {str(i).upper() for i in ids} if ids else None
    out = []
    for e in rows:
        if e.get("seq", 0) <= since:
            continue
        if kinds is not None and e.get("kind") not in kinds:
            continue
        if want is not None and str(e.get("id", "")).upper() not in want:
            continue
        out.append(e)
    return out[-limit:]


# ── the file between the two processes ───────────────────────────────────

class ConsoleLog:
    """A rolling JSON-lines file: the reader appends, Flask reads the tail.

    Size-capped and truncated to its newest half rather than rotated to a
    second file — nobody is meant to keep this, and one gitignored file is one
    thing to explain.
    """

    def __init__(self, path, max_bytes=FILE_MAX):
        self.path = path
        self.max_bytes = int(max_bytes)

    def append(self, entries):
        if not entries:
            return 0
        body = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries)
        with open(self.path, "a") as f:
            f.write(body)
        try:
            if os.path.getsize(self.path) > self.max_bytes:
                self.truncate()
        except OSError:
            pass
        return len(entries)

    def truncate(self):
        """Keep the newest half of the file. Written to a temp file and moved
        into place, so a reader never sees a half-rewritten log."""
        try:
            with open(self.path) as f:
                lines = f.readlines()
        except OSError:
            return
        keep = lines[len(lines) // 2:]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(keep)
        os.replace(tmp, self.path)

    def clear(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def read(self, since=0, kind=None, ids=None, limit=LIMIT_DEFAULT):
        """The filtered tail. A line that does not parse is skipped, not fatal:
        the writer may be mid-append, and one torn line is not worth a 500."""
        rows = []
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and "seq" in e:
                        rows.append(e)
        except OSError:
            return []
        return select(rows, since=since, kind=kind, ids=ids, limit=limit)


# ── what the reader holds ────────────────────────────────────────────────

class Console:
    """Ring + file + the options the tile asked for.

    Created by the reader when the console tile is enabled and dropped when it
    is not, so when the tile is off there is no object, no tap and no file.
    """

    def __init__(self, path, ids=(), everything=False, rate_cap=RATE_CAP,
                 capacity=CAPACITY, max_bytes=FILE_MAX):
        self.ring = Ring(capacity=capacity, rate_cap=rate_cap, ids=ids, everything=everything)
        self.log = ConsoleLog(path, max_bytes=max_bytes)
        self.log.clear()                   # a new arming starts a new window
        self.partial = ""                  # why this transport can only show part of the bus

    # the taps call these; every one of them is a no-op that returns None when
    # the entry was decimated away
    def add(self, kind, text, cid="", bus="", t=None, wall=None):
        return self.ring.add(kind, text, cid=cid, bus=bus, t=t, wall=wall)

    def wants(self, kind, cid=""):
        return self.ring.wants(kind, cid)

    def tap(self, bus=""):
        """A callable for a transport that knows nothing about this module:
        `tap(kind, text, id, t)`. Never raises — a console bug must not take a
        transport down with it."""
        def _tap(kind, text, cid="", t=None):
            try:
                self.add(kind, text, cid=cid, bus=bus)
            except Exception:
                pass
        return _tap

    def flush(self):
        """Write what the taps added since the last flush. The reader calls this
        once a cycle, so the file is a cycle behind at worst."""
        return self.log.append(self.ring.drain())

    def stats(self):
        s = self.ring.stats()
        s["on"] = True
        if self.partial:
            s["partial"] = self.partial
        return s
