#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Phase 1, Step 3: Probe the ELM327 adapter with identity commands.
Writes ONLY to the adapter chip — no vehicle ECU traffic.

Commands sent:
  ATZ   — reset adapter
  ATI   — firmware version
  ATE0  — echo off
  AT@1  — device description

Usage: python probe_adapter.py <BLE_ADDRESS_OR_NAME> [TX_UUID] [RX_UUID]

If TX/RX UUIDs are not provided, the script will attempt to auto-detect
them from the GATT enumeration (looks for write + notify characteristics).
"""

import asyncio
import sys
from bleak import BleakClient, BleakScanner


# Common UUIDs for ELM327 BLE adapters (varies by manufacturer)
# These will be overridden if the user provides them or auto-detection succeeds
KNOWN_SERVICE_UUIDS = [
    "0000fff0-0000-1000-8000-00805f9b34fb",  # Common OBD service
    "e7810a71-73ae-499d-8c15-faa9aef0c3f2",  # Another common one
]


async def find_tx_rx(client: BleakClient):
    """Auto-detect TX (write) and RX (notify) characteristics."""
    tx_char = None
    rx_char = None

    for service in client.services:
        for char in service.characteristics:
            if "write-without-response" in char.properties or "write" in char.properties:
                if tx_char is None:
                    tx_char = char.uuid
            if "notify" in char.properties:
                if rx_char is None:
                    rx_char = char.uuid

    return tx_char, rx_char


async def send_command(client, tx_uuid: str, rx_uuid: str, command: str, timeout: float = 5.0):
    """Send an AT command and collect the response via notifications."""
    response_parts = []
    event = asyncio.Event()

    def notification_handler(sender, data: bytearray):
        hex_str = data.hex()
        text = data.decode("ascii", errors="replace")
        print(f"    [RX raw] hex={hex_str} ascii={repr(text)}")
        response_parts.append(text)
        # ELM327 signals end-of-response with '>'
        if ">" in text:
            event.set()

    await client.start_notify(rx_uuid, notification_handler)
    await asyncio.sleep(0.1)  # let notification subscription settle

    # ELM327 expects commands terminated with \r
    cmd_bytes = (command + "\r").encode("ascii")
    print(f"    [TX] sending {repr(cmd_bytes)}")

    # Try write with response first, fall back to without
    try:
        await client.write_gatt_char(tx_uuid, cmd_bytes, response=True)
    except Exception:
        await client.write_gatt_char(tx_uuid, cmd_bytes, response=False)

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"    [TIMEOUT] no '>' prompt received within {timeout}s")

    await client.stop_notify(rx_uuid)

    full_response = "".join(response_parts).strip()
    # Remove the trailing prompt '>'
    if full_response.endswith(">"):
        full_response = full_response[:-1].strip()
    return full_response


async def main(target: str, tx_uuid: str = None, rx_uuid: str = None):
    print(f"Searching for device: {target}")
    device = await BleakScanner.find_device_by_address(target, timeout=10)
    if device is None:
        device = await BleakScanner.find_device_by_name(target, timeout=10)
    if device is None:
        print(f"Device '{target}' not found.")
        return

    print(f"Found: {device.name} [{device.address}]")
    print("Connecting...\n")

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")

        # Auto-detect TX/RX if not provided
        if tx_uuid is None or rx_uuid is None:
            print("Auto-detecting TX/RX characteristics...")
            auto_tx, auto_rx = await find_tx_rx(client)
            tx_uuid = tx_uuid or auto_tx
            rx_uuid = rx_uuid or auto_rx

        if not tx_uuid or not rx_uuid:
            print("ERROR: Could not determine TX/RX UUIDs.")
            print("Run enumerate_gatt.py first, then pass UUIDs as arguments.")
            return

        print(f"TX (write):  {tx_uuid}")
        print(f"RX (notify): {rx_uuid}\n")

        # Adapter-only commands (safe, no vehicle bus traffic)
        commands = [
            ("ATZ", "Reset adapter"),
            ("ATI", "Firmware version"),
            ("ATE0", "Echo off"),
            ("AT@1", "Device description"),
        ]

        for cmd, desc in commands:
            print(f">>> {cmd}  ({desc})")
            try:
                resp = await send_command(client, tx_uuid, rx_uuid, cmd)
                print(f"    Response: {repr(resp)}\n")
            except Exception as e:
                print(f"    ERROR: {e}\n")
            # Extra delay after ATZ (adapter resets)
            if cmd == "ATZ":
                print("    (waiting 2s for adapter reset...)")
                await asyncio.sleep(2.0)
            else:
                await asyncio.sleep(0.3)

    print("Disconnected.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python probe_adapter.py <BLE_ADDRESS> [TX_UUID] [RX_UUID]")
        print("  Run enumerate_gatt.py first to find the UUIDs.")
        sys.exit(1)

    target = sys.argv[1]
    tx = sys.argv[2] if len(sys.argv) > 2 else None
    rx = sys.argv[3] if len(sys.argv) > 3 else None
    asyncio.run(main(target, tx, rx))
