#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Phase 1, Step 2: Connect to a BLE device and enumerate GATT services.
Read-only — lists services, characteristics, and their properties.

Usage: python enumerate_gatt.py <BLE_ADDRESS_OR_NAME>
"""

import asyncio
import sys
from bleak import BleakClient, BleakScanner


async def main(target: str):
    print(f"Searching for device: {target}")
    device = await BleakScanner.find_device_by_address(target, timeout=10)
    if device is None:
        # Try by name
        device = await BleakScanner.find_device_by_name(target, timeout=10)
    if device is None:
        print(f"Device '{target}' not found. Run scan_ble.py first.")
        return

    print(f"Found: {device.name} [{device.address}]")
    print("Connecting...\n")

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")
        print(f"MTU size: {client.mtu_size}\n")

        for service in client.services:
            print(f"Service: {service.uuid}")
            print(f"  Description: {service.description}")

            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  Characteristic: {char.uuid}")
                print(f"    Description: {char.description}")
                print(f"    Properties:  {props}")
                print(f"    Handle:      0x{char.handle:04X}")

                # Try to read if readable
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        # Show as hex and attempt ASCII
                        hex_str = value.hex()
                        try:
                            ascii_str = value.decode("ascii", errors="replace")
                        except Exception:
                            ascii_str = ""
                        print(f"    Value (hex): {hex_str}")
                        if ascii_str.isprintable():
                            print(f"    Value (ascii): {ascii_str}")
                    except Exception as e:
                        print(f"    Read error: {e}")

                for desc in char.descriptors:
                    print(f"    Descriptor: {desc.uuid} = {desc.description}")

            print()

    print("Disconnected.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python enumerate_gatt.py <BLE_ADDRESS_OR_NAME>")
        print("  Run scan_ble.py first to find your adapter's address.")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
