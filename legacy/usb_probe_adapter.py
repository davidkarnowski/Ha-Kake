#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
USB Serial probe for ELM327 adapter (CH340-based).
Equivalent to probe_adapter.py but uses serial port instead of BLE.

Sends adapter-only commands — no vehicle ECU traffic.

Commands sent:
  ATZ   — reset adapter
  ATI   — firmware version
  ATE0  — echo off
  AT@1  — device description

Usage: python usb_probe_adapter.py [PORT] [BAUD]

Defaults:
  PORT = /dev/tty.usbserial-0001
  BAUD = tries 38400, 115200, 9600 (auto-detect)
"""

import sys
import time
import serial


DEFAULT_PORT = "/dev/tty.usbserial-0001"
COMMON_BAUDS = [38400, 115200, 9600]


def send_command(ser, command, timeout=3.0):
    """Send an AT command and collect response until '>' prompt."""
    ser.reset_input_buffer()
    cmd_bytes = (command + "\r").encode("ascii")
    print(f"    [TX] sending {repr(cmd_bytes)}")
    ser.write(cmd_bytes)

    response = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting > 0:
            chunk = ser.read(ser.in_waiting)
            hex_str = chunk.hex()
            text = chunk.decode("ascii", errors="replace")
            print(f"    [RX raw] hex={hex_str} ascii={repr(text)}")
            response += chunk
            if b">" in response:
                break
        else:
            time.sleep(0.05)

    if b">" not in response:
        print(f"    [TIMEOUT] no '>' prompt received within {timeout}s")

    text = response.decode("ascii", errors="replace").strip()
    if text.endswith(">"):
        text = text[:-1].strip()
    return text


def try_baud(port, baud):
    """Try to connect at a given baud rate and get an ATI response."""
    print(f"\n  Trying {baud} baud...")
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1,
            write_timeout=1,
        )
    except serial.SerialException as e:
        print(f"  Could not open port: {e}")
        return None

    # Flush any garbage
    time.sleep(0.2)
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # Send ATZ to reset, wait for response
    ser.write(b"ATZ\r")
    time.sleep(1.5)
    resp = b""
    while ser.in_waiting > 0:
        resp += ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Send ATI to check for ELM327 banner
    ser.write(b"ATI\r")
    time.sleep(0.5)
    while ser.in_waiting > 0:
        resp += ser.read(ser.in_waiting)
        time.sleep(0.1)

    text = resp.decode("ascii", errors="replace")
    if "ELM" in text.upper() or ">" in text:
        print(f"  Got response at {baud} baud: {repr(text.strip())}")
        return ser
    else:
        print(f"  No ELM327 response at {baud} (got {repr(text[:60])})")
        ser.close()
        return None


def main(port=DEFAULT_PORT, baud=None):
    print(f"USB ELM327 Probe")
    print(f"Port: {port}")

    # If baud specified, use it directly; otherwise auto-detect
    if baud:
        bauds_to_try = [baud]
    else:
        bauds_to_try = COMMON_BAUDS
        print(f"Auto-detecting baud rate (trying {bauds_to_try})...")

    ser = None
    for b in bauds_to_try:
        ser = try_baud(port, b)
        if ser is not None:
            baud = b
            break

    if ser is None:
        print("\nERROR: Could not communicate with adapter at any baud rate.")
        print("Check: cable connected? CH340 driver installed? Correct port?")
        sys.exit(1)

    print(f"\nConnected: {port} @ {baud} baud")
    print(f"{'=' * 50}\n")

    # Adapter-only commands (safe, no vehicle bus traffic)
    commands = [
        ("ATZ",  "Reset adapter"),
        ("ATI",  "Firmware version"),
        ("ATE0", "Echo off"),
        ("AT@1", "Device description"),
    ]

    for cmd, desc in commands:
        print(f">>> {cmd}  ({desc})")
        try:
            resp = send_command(ser, cmd)
            print(f"    Response: {repr(resp)}\n")
        except Exception as e:
            print(f"    ERROR: {e}\n")
        # Extra delay after ATZ
        if cmd == "ATZ":
            print("    (waiting 2s for adapter reset...)")
            time.sleep(2.0)
        else:
            time.sleep(0.3)

    ser.close()
    print("Port closed.")


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else None
    main(port, baud)
