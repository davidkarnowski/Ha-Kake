#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Turn Signal Demo — 2012 Nissan Leaf
Displays turn signal state changes in real time with human-readable output.

Usage:
  source venv/bin/activate
  python turn_signal_demo.py
"""

import asyncio
import datetime as dt

from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

SIGNAL_STATES = {
    0x80: ("OFF",          "  "),
    0x82: ("LEFT   <<<",   "← "),
    0x84: ("RIGHT  >>>",   "→ "),
    0x86: ("HAZARDS !!!",  "⚠ "),
}


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def main():
    print("Connecting to OBDBLE adapter...")

    async with BleakClient(ADDR) as client:
        print(f"Connected (MTU={client.mtu_size})\n")

        buf = ""
        last_state = None
        frame_count = 0
        cmd_done = asyncio.Event()

        def on_notify(_sender, data: bytearray):
            nonlocal buf, last_state, frame_count

            text = bytes(data).decode("ascii", errors="replace")

            # During command setup, just watch for prompt
            if not cmd_done.is_set():
                buf += text
                if ">" in buf:
                    cmd_done.set()
                    buf = ""
                return

            # Streaming mode: assemble lines
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

                parts = line.split()
                if len(parts) < 4 or parts[0] != "358":
                    continue

                frame_count += 1
                data_bytes = parts[1:]
                raw_hex = " ".join(data_bytes)

                # Byte 3 (index 2) is the turn signal byte
                try:
                    signal_byte = int(data_bytes[2], 16)
                except (IndexError, ValueError):
                    continue

                label, icon = SIGNAL_STATES.get(signal_byte, (f"UNKNOWN (0x{signal_byte:02X})", "? "))

                # Only print full line on state changes; show dots for repeats
                if signal_byte != last_state:
                    last_state = signal_byte
                    print(f"\n{icon}{ts()}  [{raw_hex}]  {label}", flush=True)
                else:
                    print(".", end="", flush=True)

        await client.start_notify(FFE1, on_notify)
        await asyncio.sleep(0.2)

        # Configure adapter
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
        await send("ATCRA 358")

        # Start streaming
        cmd_done.set()  # switch to streaming mode
        buf = ""
        await client.write_gatt_char(FFE1, b"ATMA\r", response=True)

        print("Ready! Toggle your turn signals.\n")
        print("Press Ctrl+C to stop.\n")

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

        # Cleanup
        try:
            await client.write_gatt_char(FFE1, b"\r", response=True)
        except Exception:
            pass
        await client.stop_notify(FFE1)

        print(f"\n\nDone. {frame_count} frames captured.")


if __name__ == "__main__":
    asyncio.run(main())
