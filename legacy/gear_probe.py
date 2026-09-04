#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gear/transmission state probe — 2012 Nissan Leaf
Filters one CAN ID at a time (hardware filter) to avoid BUFFER FULL.
Prints only when data changes.

Usage:
  source venv/bin/activate
  python gear_probe.py            # scans all candidate IDs, 8s each
  python gear_probe.py --id 354   # watch a single ID continuously
"""

import argparse
import asyncio
import datetime as dt

from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Candidate IDs for gear state (from our unfiltered capture)
CANDIDATE_IDS = ["354", "174", "176", "180", "1D5", "284", "285", "300", "130", "1F9"]


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class ELM:
    def __init__(self, client):
        self.client = client
        self.buf = ""
        self.cmd_mode = True
        self.cmd_event = asyncio.Event()
        self.lines = []
        self.last_data = None
        self.changes = []

    def on_notify(self, _sender, data: bytearray):
        text = bytes(data).decode("ascii", errors="replace")

        if self.cmd_mode:
            self.buf += text
            if ">" in self.buf:
                self.cmd_event.set()
                self.buf = ""
            return

        self.buf += text
        while "\r" in self.buf or "\n" in self.buf:
            idx_r = self.buf.find("\r")
            idx_n = self.buf.find("\n")
            if idx_r == -1:
                idx = idx_n
            elif idx_n == -1:
                idx = idx_r
            else:
                idx = min(idx_r, idx_n)

            line = self.buf[:idx].strip()
            self.buf = self.buf[idx + 1:]

            if not line or line in ("OK", ">", "SEARCHING...", "STOPPED", "BUFFER FULL"):
                continue
            if "<DATA ERROR" in line:
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            can_id = parts[0]
            data_str = " ".join(parts[1:])

            if data_str != self.last_data:
                if self.last_data is not None:
                    # Find which bytes changed
                    prev = self.last_data.split()
                    curr = data_str.split()
                    diffs = []
                    for i in range(max(len(prev), len(curr))):
                        p = prev[i] if i < len(prev) else "--"
                        c = curr[i] if i < len(curr) else "--"
                        if p != c:
                            diffs.append(f"b{i}:{p}→{c}")
                    print(f"  {ts()}  [{data_str}]  {', '.join(diffs)}", flush=True)
                else:
                    print(f"  {ts()}  [{data_str}]", flush=True)
                self.changes.append((ts(), data_str))
                self.last_data = data_str

        # Handle '>' without newline
        if ">" in self.buf:
            self.buf = self.buf.split(">")[-1]

    async def send(self, cmd, wait=0.3):
        self.buf = ""
        self.cmd_mode = True
        self.cmd_event.clear()
        await self.client.write_gatt_char(FFE1, (cmd + "\r").encode(), response=True)
        try:
            await asyncio.wait_for(self.cmd_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(wait)

    async def stream(self, duration):
        self.cmd_mode = False
        self.buf = ""
        await self.client.write_gatt_char(FFE1, b"ATMA\r", response=True)
        await asyncio.sleep(duration)
        self.cmd_mode = True
        try:
            await self.client.write_gatt_char(FFE1, b"\r", response=True)
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def scan_all(elm, per_id=8):
    """Scan each candidate ID for `per_id` seconds."""
    print(f"Scanning {len(CANDIDATE_IDS)} CAN IDs, {per_id}s each.")
    print(f"Shift through gears as prompted.\n")

    results = {}

    for i, can_id in enumerate(CANDIDATE_IDS):
        elm.last_data = None
        elm.changes = []

        await elm.send(f"ATCRA {can_id}")

        print(f"[{i+1}/{len(CANDIDATE_IDS)}] 0x{can_id} — listening {per_id}s (shift gears now!)")
        await elm.stream(per_id)

        results[can_id] = len(elm.changes)
        if len(elm.changes) <= 1:
            print(f"  (no changes)\n")
        else:
            print(f"  ({len(elm.changes)} unique states)\n")

        await elm.send("ATAR")

    # Summary
    print("\n" + "=" * 60)
    print("SCAN RESULTS — IDs that changed during gear shifts:")
    print("=" * 60)
    for can_id, count in sorted(results.items(), key=lambda x: -x[1]):
        marker = " <<<" if count > 2 else ""
        print(f"  0x{can_id}: {count} unique states{marker}")


async def watch_one(elm, can_id):
    """Watch a single ID continuously."""
    elm.last_data = None
    elm.changes = []

    await elm.send(f"ATCRA {can_id}")

    print(f"Watching 0x{can_id} — shift through P, R, N, D, Eco. Ctrl+C to stop.\n")

    elm.cmd_mode = False
    elm.buf = ""
    await elm.client.write_gatt_char(FFE1, b"ATMA\r", response=True)

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    elm.cmd_mode = True
    try:
        await elm.client.write_gatt_char(FFE1, b"\r", response=True)
    except Exception:
        pass

    print(f"\n\nAll unique states seen:")
    for t, d in elm.changes:
        print(f"  {t}  [{d}]")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=str, default="", help="Watch a single CAN ID (e.g. 354)")
    ap.add_argument("--time", type=int, default=8, help="Seconds per ID in scan mode (default: 8)")
    args = ap.parse_args()

    async with BleakClient(ADDR) as client:
        print(f"Connected (MTU={client.mtu_size})")

        elm = ELM(client)
        await client.start_notify(FFE1, elm.on_notify)
        await asyncio.sleep(0.2)

        print("Configuring adapter...")
        await elm.send("ATZ", 1.5)
        await elm.send("ATE0")
        await elm.send("ATL1")
        await elm.send("ATS1")
        await elm.send("ATH1")
        await elm.send("ATSP6")
        print()

        if args.id:
            await watch_one(elm, args.id.upper())
        else:
            await scan_all(elm, per_id=args.time)

        await client.stop_notify(FFE1)

    print("\nDisconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
