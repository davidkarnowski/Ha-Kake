#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal diagnostic: test notification patterns + manual CCCD + write modes."""

import asyncio
from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"
CCCD = "00002902-0000-1000-8000-00805f9b34fb"


async def main():
    print("Connecting...")
    async with BleakClient(ADDR) as client:
        print(f"Connected: {client.is_connected}, MTU: {client.mtu_size}")

        # List services to confirm we see FFE1
        print("\nServices:")
        for svc in client.services:
            for char in svc.characteristics:
                print(f"  {char.uuid}: {', '.join(char.properties)}")
                for desc in char.descriptors:
                    print(f"    desc: {desc.uuid}")

        received = []

        def handler(sender, data: bytearray):
            text = data.decode("ascii", errors="replace")
            print(f"  NOTIFY: hex={data.hex()} ascii={repr(text)}", flush=True)
            received.append(text)

        # Test A: Normal start_notify
        print("\n=== Test A: start_notify + write(response=False) ===")
        await client.start_notify(FFE1, handler)
        await asyncio.sleep(0.5)
        await client.write_gatt_char(FFE1, b"ATI\r", response=False)
        await asyncio.sleep(2.0)
        print(f"  Got {len(received)} notifications")
        await client.stop_notify(FFE1)
        await asyncio.sleep(0.5)

        # Test B: start_notify + write with response=True
        print("\n=== Test B: start_notify + write(response=True) ===")
        received.clear()
        await client.start_notify(FFE1, handler)
        await asyncio.sleep(0.5)
        try:
            await client.write_gatt_char(FFE1, b"ATI\r", response=True)
            print("  write(response=True) succeeded")
        except Exception as e:
            print(f"  write(response=True) failed: {e}")
            await client.write_gatt_char(FFE1, b"ATI\r", response=False)
        await asyncio.sleep(2.0)
        print(f"  Got {len(received)} notifications")
        await client.stop_notify(FFE1)
        await asyncio.sleep(0.5)

        # Test C: Try reading the characteristic directly
        print("\n=== Test C: Direct read of FFE1 ===")
        try:
            val = await client.read_gatt_char(FFE1)
            print(f"  Read value: hex={val.hex()} ascii={repr(val.decode('ascii', errors='replace'))}")
        except Exception as e:
            print(f"  Read failed: {e}")

        # Test D: Write ATZ (reset) then ATI, give more time
        print("\n=== Test D: ATZ reset then ATI (longer waits) ===")
        received.clear()
        await client.start_notify(FFE1, handler)
        await asyncio.sleep(1.0)

        print("  Sending ATZ...")
        await client.write_gatt_char(FFE1, b"ATZ\r", response=False)
        await asyncio.sleep(3.0)
        print(f"  After ATZ: {len(received)} notifications")

        print("  Sending ATI...")
        await client.write_gatt_char(FFE1, b"ATI\r", response=False)
        await asyncio.sleep(3.0)
        print(f"  After ATI: {len(received)} notifications")

        await client.stop_notify(FFE1)

        # Test E: Manual CCCD write
        print("\n=== Test E: Manual CCCD enable + write ===")
        received.clear()
        # Write 0x0001 to CCCD to enable notifications
        try:
            await client.write_gatt_descriptor(0x0025, b"\x01\x00")
            print("  Manually wrote CCCD (handle 0x0025)")
        except Exception as e:
            print(f"  CCCD write failed: {e}")
            # Try by UUID
            try:
                # Find the CCCD descriptor for FFE1
                for svc in client.services:
                    for char in svc.characteristics:
                        if char.uuid == FFE1:
                            for desc in char.descriptors:
                                if "2902" in desc.uuid:
                                    await client.write_gatt_descriptor(desc.handle, b"\x01\x00")
                                    print(f"  Manually wrote CCCD (handle 0x{desc.handle:04X})")
            except Exception as e2:
                print(f"  CCCD by-handle also failed: {e2}")

        # Now listen without start_notify (CCCD already set)
        # Actually we still need a callback... use start_notify
        await client.start_notify(FFE1, handler)
        await asyncio.sleep(0.5)
        await client.write_gatt_char(FFE1, b"ATI\r", response=False)
        await asyncio.sleep(2.0)
        print(f"  Got {len(received)} notifications")

        await client.stop_notify(FFE1)

    print("\nAll tests done.")


if __name__ == "__main__":
    asyncio.run(main())
