#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drive vs Eco Diff — 2012 Nissan Leaf
Captures multiple CAN IDs in Drive, then Eco, and shows which bytes differ.
This helps find the CAN signal that distinguishes Drive from Eco mode.

Usage:
  source venv/bin/activate
  python drive_eco_diff.py
"""

import asyncio
import datetime as dt
from collections import Counter

from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Broad set of CAN IDs to check (everything we've seen on the bus)
SCAN_IDS = [
    "130", "174", "176", "180", "1D5", "1F9",
    "284", "285", "292", "300", "354", "358",
]

CAPTURE_SECONDS = 3


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class ELM:
    def __init__(self, client):
        self.client = client
        self.buf = ""
        self.cmd_mode = True
        self.cmd_event = asyncio.Event()
        self.frames = []

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

            self.frames.append(" ".join(parts[1:]))

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

    async def capture(self, duration):
        self.frames = []
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


def most_common_frame(frames):
    """Return the most common frame, or None."""
    if not frames:
        return None
    return Counter(frames).most_common(1)[0][0]


async def main():
    print("Connecting to OBDBLE adapter...")

    async with BleakClient(ADDR) as client:
        print(f"Connected (MTU={client.mtu_size})\n")

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

        captures = {}  # {mode: {can_id: most_common_data}}

        for mode in ["DRIVE", "ECO"]:
            print(f"\n>>> Put the car in {mode}, then press ENTER <<<")
            await asyncio.get_event_loop().run_in_executor(None, input)

            captures[mode] = {}

            for can_id in SCAN_IDS:
                await elm.send(f"ATCRA {can_id}")
                print(f"  0x{can_id}...", end=" ", flush=True)
                await elm.capture(CAPTURE_SECONDS)
                await elm.send("ATAR")

                common = most_common_frame(elm.frames)
                captures[mode][can_id] = common
                count = len(elm.frames)
                if common:
                    print(f"{count} frames  [{common}]")
                else:
                    print(f"(no data)")

        # Diff
        print("\n\n" + "=" * 70)
        print("DIFF — Drive vs Eco")
        print("=" * 70)

        found_diff = False
        for can_id in SCAN_IDS:
            d_data = captures["DRIVE"].get(can_id)
            e_data = captures["ECO"].get(can_id)

            if not d_data or not e_data:
                continue

            d_bytes = d_data.split()
            e_bytes = e_data.split()

            diffs = []
            for i in range(max(len(d_bytes), len(e_bytes))):
                db = d_bytes[i] if i < len(d_bytes) else "--"
                eb = e_bytes[i] if i < len(e_bytes) else "--"
                if db != eb:
                    diffs.append((i, db, eb))

            if diffs:
                found_diff = True
                print(f"\n  0x{can_id}:")
                print(f"    Drive: [{d_data}]")
                print(f"    Eco:   [{e_data}]")
                for i, db, eb in diffs:
                    print(f"    >>> byte {i}: {db} (Drive) → {eb} (Eco)")

        if not found_diff:
            print("\n  No stable differences found between Drive and Eco!")
            print("  (byte 4 on 0x174 is a rolling counter, so it varies)")

        await client.stop_notify(FFE1)

    print("\nDisconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
