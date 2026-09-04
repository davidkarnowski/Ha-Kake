#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live door / lock watcher — prints only when a byte on a body CAN ID changes.

Pauses the dashboard reader, then streams passive captures of the door/lock
candidate IDs and prints a line whenever any byte changes, with the changed
bytes flagged. Open and shut each door, lock and unlock — you see it instantly.

Usage: ./venv/bin/python door_watch.py [seconds] [--ids 60D,5C5,625]
"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from elm327 import detect_adapter, passive_capture          # noqa: E402
from leaf_decoders import last_complete_frame               # noqa: E402

PAUSE_FILE = os.path.join(ROOT, "web", "reader.pause")


def ts():
    return dt.datetime.now().strftime("%H:%M:%S")


async def main(seconds, ids):
    open(PAUSE_FILE, "w").close()
    print("Paused the dashboard reader. Open/shut doors and lock/unlock — changes print live.\n")
    elm = await detect_adapter(prefer="ble")
    for c in ("ATZ", "ATE0", "ATL1", "ATH1", "ATS1", "ATSP6"):
        await elm.send(c, wait=0.3 if c == "ATZ" else 0)
    last = {}
    end = asyncio.get_event_loop().time() + seconds
    try:
        first = True
        while asyncio.get_event_loop().time() < end:
            for cid in ids:
                frames = await passive_capture(elm, cid, 0.25, set_caf=first)
                first = False
                b = last_complete_frame(frames, 2)
                if not b:
                    continue
                prev = last.get(cid)
                if prev is None:
                    print(f"{ts()}  {cid} baseline: {' '.join(f'{x:02X}' for x in b)}")
                elif prev != b:
                    marks = " ".join((f"[{x:02X}]" if i >= len(prev) or prev[i] != x else f"{x:02X}") for i, x in enumerate(b))
                    diff = ", ".join(f"b{i} {prev[i]:02X}->{b[i]:02X}" for i in range(min(len(prev), len(b))) if prev[i] != b[i])
                    print(f"{ts()}  {cid}: {marks}   ({diff})")
                last[cid] = b
    finally:
        await elm.close()
        os.remove(PAUSE_FILE) if os.path.exists(PAUSE_FILE) else None
        print("\nDashboard reader resumed.")


if __name__ == "__main__":
    secs = 180.0
    ids = ["60D", "5C5", "625"]
    args = sys.argv[1:]
    if args and args[0].replace(".", "").isdigit():
        secs = float(args[0]); args = args[1:]
    if "--ids" in args:
        ids = args[args.index("--ids") + 1].split(",")
    try:
        asyncio.run(main(secs, ids))
    except KeyboardInterrupt:
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
        print("\nStopped — reader resumed.")
