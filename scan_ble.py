#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Phase 1, Step 1: Passive BLE scan.
Lists all nearby BLE devices — no connections, no writes.
"""

import asyncio
from bleak import BleakScanner


async def main():
    print("Scanning for BLE devices (10 seconds)...\n")
    devices = await BleakScanner.discover(timeout=10, return_adv=True)

    if not devices:
        print("No BLE devices found. Is Bluetooth enabled?")
        return

    print(f"Found {len(devices)} device(s):\n")
    print(f"{'Name':<30} {'Address':<40} {'RSSI':>5}")
    print("-" * 77)

    for addr, (device, adv_data) in sorted(
        devices.items(), key=lambda x: x[1][1].rssi or -999, reverse=True
    ):
        name = device.name or adv_data.local_name or "(unknown)"
        rssi = adv_data.rssi
        print(f"{name:<30} {addr:<40} {rssi:>5} dBm")

    # Highlight likely OBD adapters
    obd_keywords = ["lelink", "obd", "vlink", "elm", "car"]
    print("\n--- Likely OBD-II adapters ---")
    found = False
    for addr, (device, adv_data) in devices.items():
        name = (device.name or adv_data.local_name or "").lower()
        if any(kw in name for kw in obd_keywords):
            print(f"  >>> {device.name or adv_data.local_name}  [{addr}]")
            found = True
    if not found:
        print("  (none auto-detected — check the full list above)")


if __name__ == "__main__":
    asyncio.run(main())
