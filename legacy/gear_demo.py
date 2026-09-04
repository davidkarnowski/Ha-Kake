#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gear Position Demo — 2012 Nissan Leaf
Displays current gear in real time using CAN ID 0x174 byte 3.

Decode:
  0xAA = Park / Neutral  (indistinguishable on Car-CAN)
  0x99 = Reverse
  0xBB = Drive / Eco     (indistinguishable on Car-CAN)

Usage:
  source venv/bin/activate
  python gear_demo.py
"""

import asyncio
import datetime as dt
from collections import deque

from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Debounce: require N consecutive identical readings before changing display
DEBOUNCE_COUNT = 5

GEAR_MAP = {
    0xAA: ("P/N", "Park/Neutral"),
    0x99: (" R ", "Reverse"),
    0xBB: ("D/E", "Drive/Eco"),
}

BAR_SLOTS = [("P/N", 0xAA), (" R ", 0x99), ("D/E", 0xBB)]


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def gear_bar(active_byte):
    """Render [P/N]  R  D/E bar with active gear highlighted."""
    parts = []
    for icon, val in BAR_SLOTS:
        if val == active_byte:
            parts.append(f"[{icon}]")
        else:
            parts.append(f" {icon} ")
    return " ".join(parts)


async def main():
    print("Connecting to OBDBLE adapter...")

    async with BleakClient(ADDR) as client:
        print(f"Connected (MTU={client.mtu_size})\n")

        buf = ""
        cmd_done = asyncio.Event()
        frame_count = 0
        last_gear = None
        debounce_buf = deque(maxlen=DEBOUNCE_COUNT)

        def on_notify(_sender, data: bytearray):
            nonlocal buf, frame_count, last_gear

            text = bytes(data).decode("ascii", errors="replace")

            if not cmd_done.is_set():
                buf += text
                if ">" in buf:
                    cmd_done.set()
                    buf = ""
                return

            buf += text
            while "\r" in buf or "\n" in buf:
                idx_r = buf.find("\r")
                idx_n = buf.find("\n")
                if idx_r == -1:
                    idx = idx_n
                elif idx_n == -1:
                    idx = idx_r
                else:
                    idx = min(idx_r, idx_n)

                line = buf[:idx].strip()
                buf = buf[idx + 1:]

                if not line or line in ("OK", ">", "SEARCHING...", "STOPPED", "BUFFER FULL"):
                    continue
                if "<DATA ERROR" in line:
                    continue

                parts = line.split()
                if len(parts) < 5 or parts[0] != "174":
                    continue

                frame_count += 1

                try:
                    b3 = int(parts[4], 16)  # byte 3 (parts[0]=ID, parts[1]=b0, ..., parts[4]=b3)
                except (IndexError, ValueError):
                    continue

                if b3 not in GEAR_MAP:
                    continue

                # Debounce: only change display after N consecutive identical readings
                debounce_buf.append(b3)
                if len(debounce_buf) == DEBOUNCE_COUNT and all(g == b3 for g in debounce_buf):
                    if b3 != last_gear:
                        last_gear = b3
                        icon, label = GEAR_MAP[b3]
                        bar = gear_bar(b3)
                        raw = " ".join(parts[1:])
                        print(f"\n  {ts()}  {bar}   {label}   [{raw}]", flush=True)
                    else:
                        print(".", end="", flush=True)

        await client.start_notify(FFE1, on_notify)
        await asyncio.sleep(0.2)

        async def send(cmd, wait=0.3):
            nonlocal buf
            buf = ""
            cmd_done.clear()
            await client.write_gatt_char(FFE1, (cmd + "\r").encode(), response=True)
            try:
                await asyncio.wait_for(cmd_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(wait)

        print("Configuring adapter...")
        await send("ATZ", 1.5)
        await send("ATE0")
        await send("ATL1")
        await send("ATS1")
        await send("ATH1")
        await send("ATSP6")
        await send("ATCRA 174")  # Hardware filter: only 0x174

        cmd_done.set()
        buf = ""
        await client.write_gatt_char(FFE1, b"ATMA\r", response=True)

        print("Ready! Shift through P, R, N, D, Eco.\n")
        print("Press Ctrl+C to stop.\n")

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

        try:
            await client.write_gatt_char(FFE1, b"\r", response=True)
        except Exception:
            pass
        await client.stop_notify(FFE1)

        print(f"\n\nDone. {frame_count} frames captured.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
