#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gear Position Capture — 2012 Nissan Leaf
Captures CAN data one gear at a time. Prompts you to shift, then records
stable byte values for each position.

Monitors 0x354 and 0x174 simultaneously (both gear candidates).

Usage:
  source venv/bin/activate
  python gear_capture.py
"""

import asyncio
import datetime as dt

from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

GEARS = ["Park", "Reverse", "Neutral", "Drive", "Eco"]
WATCH_IDS = {"354", "174"}

CAPTURE_SECONDS = 5


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class ELM:
    def __init__(self, client):
        self.client = client
        self.buf = ""
        self.cmd_mode = True
        self.cmd_event = asyncio.Event()
        self.frames = {}  # {can_id: [data_str, ...]}

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
            if can_id not in WATCH_IDS:
                continue

            data_str = " ".join(parts[1:])
            if can_id not in self.frames:
                self.frames[can_id] = []
            self.frames[can_id].append(data_str)

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
        """Capture frames for `duration` seconds using ATMA."""
        self.frames = {}
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

        # Filter to only 354 and 174 using CAN filter + mask
        # 354 = 0x354 = 0011 0101 0100
        # 174 = 0x174 = 0001 0111 0100
        # These differ too much for a single mask, so we'll watch one at a time
        # Actually, let's just capture each ID separately per gear

        results = {}  # {gear: {can_id: most_common_data}}

        print()
        print("=" * 60)
        print("GEAR POSITION CAPTURE")
        print(f"Will capture {CAPTURE_SECONDS}s of data per gear, per CAN ID")
        print("=" * 60)

        for gear in GEARS:
            print(f"\n>>> Shift to {gear.upper()}, then press ENTER <<<")
            await asyncio.get_event_loop().run_in_executor(None, input)

            results[gear] = {}

            for can_id in sorted(WATCH_IDS):
                await elm.send(f"ATCRA {can_id}")
                print(f"  Capturing 0x{can_id} in {gear}...", end=" ", flush=True)
                await elm.capture(CAPTURE_SECONDS)
                await elm.send("ATAR")

                frames = elm.frames.get(can_id, [])
                print(f"{len(frames)} frames")

                if frames:
                    # Find most common frame (stable value)
                    from collections import Counter
                    common = Counter(frames).most_common(1)[0][0]
                    results[gear][can_id] = common
                    print(f"    0x{can_id}: [{common}]")
                else:
                    print(f"    0x{can_id}: (no data)")

        # Summary table
        print("\n\n" + "=" * 60)
        print("SUMMARY — Byte values per gear position")
        print("=" * 60)

        for can_id in sorted(WATCH_IDS):
            print(f"\n  CAN ID 0x{can_id}:")
            print(f"  {'Gear':<10} {'Data':<30} {'Key Bytes'}")
            print(f"  {'-'*10} {'-'*30} {'-'*20}")

            all_data = []
            for gear in GEARS:
                data = results[gear].get(can_id, "(no data)")
                all_data.append(data)

            # Find bytes that differ between gears
            if all(isinstance(d, str) and d != "(no data)" for d in all_data):
                split_data = [d.split() for d in all_data]
                max_len = max(len(s) for s in split_data)
                changing_bytes = []
                for i in range(max_len):
                    vals = set()
                    for s in split_data:
                        if i < len(s):
                            vals.add(s[i])
                    if len(vals) > 1:
                        changing_bytes.append(i)

                for gear, data in zip(GEARS, all_data):
                    parts = data.split()
                    key = ", ".join(f"b{i}={parts[i]}" for i in changing_bytes if i < len(parts))
                    if not key:
                        key = "(no change)"
                    print(f"  {gear:<10} [{data}]  {key}")

                if changing_bytes:
                    print(f"\n  >>> Gear-encoding bytes: {', '.join(f'byte {i}' for i in changing_bytes)}")
                else:
                    print(f"\n  >>> No bytes changed between gears — not a gear signal")
            else:
                for gear, data in zip(GEARS, all_data):
                    print(f"  {gear:<10} [{data}]")

        await client.stop_notify(FFE1)

    print("\nDisconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
