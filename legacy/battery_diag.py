#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Battery Read Diagnostic — 2012 Nissan Leaf
Tests various approaches to reading BMS data via ELM327.
Shows all AT command responses for debugging.

Usage:
  source venv/bin/activate
  python battery_diag.py
"""

import asyncio
import datetime as dt

from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"


class ELM:
    def __init__(self, client):
        self.client = client
        self.buf = ""
        self.cmd_event = asyncio.Event()
        self.raw_response = ""

    def on_notify(self, _sender, data: bytearray):
        text = bytes(data).decode("ascii", errors="replace")
        self.buf += text
        self.raw_response += text

        if ">" in self.buf:
            self.cmd_event.set()
            self.buf = ""

    async def send(self, cmd, wait=0.3, timeout=8.0):
        """Send command, return raw response text (everything before '>')."""
        self.buf = ""
        self.raw_response = ""
        self.cmd_event.clear()
        await self.client.write_gatt_char(FFE1, (cmd + "\r").encode(), response=True)
        try:
            await asyncio.wait_for(self.cmd_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(wait)

        # Clean up response
        resp = self.raw_response.replace(">", "").strip()
        # Remove echo if present
        if resp.startswith(cmd):
            resp = resp[len(cmd):].strip()

        lines = [l.strip() for l in resp.replace("\r", "\n").split("\n") if l.strip()]
        return lines


async def main():
    print("Battery Read Diagnostic")
    print("=" * 60)
    print("Connecting...\n")

    async with BleakClient(ADDR) as client:
        print(f"Connected (MTU={client.mtu_size})\n")

        elm = ELM(client)
        await client.start_notify(FFE1, elm.on_notify)
        await asyncio.sleep(0.2)

        async def cmd(c, wait=0.3, timeout=8.0):
            lines = await elm.send(c, wait, timeout)
            resp_str = " | ".join(lines) if lines else "(empty)"
            print(f"  {c:20s} → {resp_str}", flush=True)
            return lines

        # === Basic setup ===
        print("--- Basic Setup ---")
        await cmd("ATZ", 1.5)
        await cmd("ATE0")
        await cmd("ATL1")
        await cmd("ATH1")
        await cmd("ATS1")
        await cmd("ATSP6")

        # === Test: verify we can see Car-CAN traffic ===
        print("\n--- Verify CAN bus (quick ATMA on 0x174) ---")
        await cmd("ATCRA 174")
        # Quick 1-second capture
        elm.buf = ""
        elm.raw_response = ""
        elm.cmd_event.clear()
        await client.write_gatt_char(FFE1, b"ATMA\r", response=True)
        await asyncio.sleep(1.0)
        await client.write_gatt_char(FFE1, b"\r", response=True)
        await asyncio.sleep(0.5)
        resp = elm.raw_response.replace(">", "").strip()
        can_lines = [l.strip() for l in resp.replace("\r", "\n").split("\n") if l.strip() and l.strip() not in ("STOPPED", "SEARCHING...")]
        print(f"  Got {len(can_lines)} CAN frames on 0x174 (bus is alive)")
        if can_lines:
            print(f"  Sample: {can_lines[0]}")
        await cmd("ATAR")

        # === Test flow control support ===
        print("\n--- Flow Control Support Test ---")
        fc1 = await cmd("ATFCSH 79B")
        fc2 = await cmd("ATFCSD 30 00 20")
        fc3 = await cmd("ATFCSM1")

        fc_supported = True
        for resp, name in [(fc1, "ATFCSH"), (fc2, "ATFCSD"), (fc3, "ATFCSM")]:
            if any("?" in line for line in resp):
                print(f"  !!! {name} NOT SUPPORTED !!!")
                fc_supported = False

        print(f"\n  Flow control supported: {'YES' if fc_supported else 'NO'}")

        # === Set header and try request ===
        print("\n--- BMS Request Setup ---")
        await cmd("ATSH 79B")
        await cmd("ATCRA 7BB")

        # === Test 1: With ATCAF1 (auto formatting ON — ELM reassembles) ===
        print("\n--- Test 1: ATCAF1 (auto-format ON) + 2102 ---")
        await cmd("ATCAF1")
        resp1 = await cmd("2102", timeout=10.0)

        # === Test 2: With ATCAF0 (raw frames) ===
        print("\n--- Test 2: ATCAF0 (raw frames) + 2102 ---")
        await cmd("ATCAF0")
        resp2 = await cmd("2102", timeout=10.0)

        # === Test 3: Try without CRA filter ===
        print("\n--- Test 3: No CRA filter + 2102 ---")
        await cmd("ATAR")
        await cmd("ATCAF1")
        resp3 = await cmd("2102", timeout=10.0)

        # === Test 4: Try group 01 (shorter response) ===
        print("\n--- Test 4: Group 01 (battery state, shorter) ---")
        await cmd("ATCRA 7BB")
        resp4 = await cmd("2101", timeout=10.0)

        # === Test 5: Try without flow control ===
        if fc_supported:
            print("\n--- Test 5: Disable FC, try raw capture ---")
            await cmd("ATFCSM0")  # Default FC mode
            await cmd("ATCAF0")
            resp5 = await cmd("2102", timeout=10.0)

        # === Test 6: Try STN commands (some clones are STN chips) ===
        print("\n--- Test 6: STN chip detection ---")
        await cmd("STI")       # STN firmware ID
        await cmd("STDI")      # STN device info

        # === Summary ===
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"  Flow control commands: {'Accepted' if fc_supported else 'REJECTED'}")
        print(f"  Test 1 (CAF1): {len(resp1)} lines — {resp1[0] if resp1 else '(empty)'}")
        print(f"  Test 2 (CAF0): {len(resp2)} lines — {resp2[0] if resp2 else '(empty)'}")
        print(f"  Test 3 (no CRA): {len(resp3)} lines — {resp3[0] if resp3 else '(empty)'}")
        print(f"  Test 4 (group 01): {len(resp4)} lines — {resp4[0] if resp4 else '(empty)'}")

        if all("NO DATA" in " ".join(r) for r in [resp1, resp2, resp3, resp4] if r):
            print("\n  All tests returned NO DATA.")
            print("  Likely cause: ELM327 clone accepts FC commands but doesn't")
            print("  actually implement flow control. The BMS sends the ISO-TP")
            print("  First Frame, waits for a Flow Control response, never gets")
            print("  one, and times out.")
            print("\n  Options:")
            print("    1. Try a genuine ELM327 v2.1 or STN1110/STN2120 adapter")
            print("    2. Try ATCFC0 (disable flow control entirely)")
            print("    3. Capture whatever the BMS sends before giving up")

        await client.stop_notify(FFE1)

    print("\nDisconnected.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
