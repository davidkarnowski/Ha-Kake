#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
USB CAN bus connectivity test — 2012 Nissan Leaf
Tries multiple protocols and monitor modes to determine if the adapter
can see any CAN traffic at all.

Usage: python usb_can_test.py [PORT] [BAUD]
"""

import sys
import time
import serial

DEFAULT_PORT = "/dev/tty.usbserial-0001"
DEFAULT_BAUD = 38400


def send(ser, cmd, wait=0.5, timeout=5.0):
    """Send command, return raw response text."""
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii"))

    response = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting > 0:
            response += ser.read(ser.in_waiting)
            if b">" in response:
                break
        else:
            time.sleep(0.05)

    time.sleep(wait)
    text = response.decode("ascii", errors="replace").replace(">", "").strip()
    return text


def main(port=DEFAULT_PORT, baud=DEFAULT_BAUD):
    print(f"USB CAN Bus Connectivity Test")
    print(f"Port: {port} @ {baud}")
    print("=" * 55)

    ser = serial.Serial(
        port=port, baudrate=baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=1, write_timeout=1,
    )
    time.sleep(0.2)
    ser.reset_input_buffer()

    # Reset and configure
    print("\n1. Reset and identify adapter")
    print(f"   ATZ:  {send(ser, 'ATZ', wait=1.5)}")
    print(f"   ATE0: {send(ser, 'ATE0')}")
    print(f"   ATI:  {send(ser, 'ATI')}")

    # Check current protocol
    print(f"\n2. Protocol info")
    print(f"   ATDP:  {send(ser, 'ATDP')}")       # Describe Protocol
    print(f"   ATDPN: {send(ser, 'ATDPN')}")      # Describe Protocol Number

    # Try standard OBD query (0100) on auto protocol
    print(f"\n3. Auto-detect protocol (ATSP0 + 0100)")
    print(f"   ATSP0: {send(ser, 'ATSP0')}")
    resp = send(ser, "0100", timeout=10.0)
    print(f"   0100:  {resp}")

    # Try CAN 500k 11-bit (protocol 6 — Car-CAN)
    print(f"\n4. Protocol 6: ISO 15765-4 CAN 500k 11-bit")
    print(f"   ATSP6: {send(ser, 'ATSP6')}")
    print(f"   ATH1:  {send(ser, 'ATH1')}")
    print(f"   ATS1:  {send(ser, 'ATS1')}")

    # Try monitor all — listen for any CAN frames (3 seconds)
    print(f"\n   ATMA (monitor all, 3s)...")
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(3.0)
    # Send any byte to stop ATMA
    ser.write(b"\r")
    time.sleep(0.5)
    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)
    text = raw.decode("ascii", errors="replace").strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    print(f"   Got {len(lines)} lines")
    for line in lines[:20]:
        print(f"     {line}")
    if len(lines) > 20:
        print(f"     ... ({len(lines) - 20} more)")

    # Try CAN 500k 29-bit (protocol 7)
    print(f"\n5. Protocol 7: ISO 15765-4 CAN 500k 29-bit")
    send(ser, "ATZ", wait=1.5)
    send(ser, "ATE0")
    print(f"   ATSP7: {send(ser, 'ATSP7')}")
    print(f"   ATH1:  {send(ser, 'ATH1')}")

    print(f"\n   ATMA (monitor all, 3s)...")
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(3.0)
    ser.write(b"\r")
    time.sleep(0.5)
    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)
    text = raw.decode("ascii", errors="replace").strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    print(f"   Got {len(lines)} lines")
    for line in lines[:20]:
        print(f"     {line}")
    if len(lines) > 20:
        print(f"     ... ({len(lines) - 20} more)")

    # Try CAN 250k (protocols 8/9 — less common but worth checking)
    print(f"\n6. Protocol 8: ISO 15765-4 CAN 250k 11-bit")
    send(ser, "ATZ", wait=1.5)
    send(ser, "ATE0")
    print(f"   ATSP8: {send(ser, 'ATSP8')}")
    print(f"   ATH1:  {send(ser, 'ATH1')}")

    print(f"\n   ATMA (monitor all, 3s)...")
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(3.0)
    ser.write(b"\r")
    time.sleep(0.5)
    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)
    text = raw.decode("ascii", errors="replace").strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    print(f"   Got {len(lines)} lines")
    for line in lines[:20]:
        print(f"     {line}")
    if len(lines) > 20:
        print(f"     ... ({len(lines) - 20} more)")

    # Voltage reading (adapter feature, no CAN needed)
    print(f"\n7. Adapter voltage (ATRV)")
    send(ser, "ATZ", wait=1.5)
    send(ser, "ATE0")
    print(f"   ATRV: {send(ser, 'ATRV')}")

    ser.close()
    print(f"\n{'=' * 55}")
    print("Done. Check above for CAN frames or errors.")
    print("If all protocols show CAN ERROR / no data, check OBD wiring.")


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD
    main(port, baud)
