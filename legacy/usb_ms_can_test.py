#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
USB MS-CAN (Medium Speed) Test — 2012 Nissan Leaf
Tests whether the MS switch on the USB adapter routes to EV-CAN (pins 12/13).

Tries multiple CAN speeds and monitors for known EV-CAN broadcast messages.

Expected EV-CAN signals (passive, no request needed):
  0x1DA — Motor torque/RPM (100 Hz, from inverter)
  0x1DB — HV battery voltage + current (100 Hz, from LBC)
  0x1DC — Battery power limits (100 Hz, from LBC)
  0x5BC — GIDs / energy remaining (~2 Hz, from LBC)
  0x55B — Battery SOC (10 Hz, from LBC)
  0x54B — HVAC status (10 Hz, from climate module)
  0x380 — Charger status (10 Hz, from OBC)
  0x55A — Motor/inverter temperatures (from inverter)

If we see these, the MS switch gives us direct EV-CAN access.
If we see 0x174, 0x358, 0x284 instead — it's still Car-CAN.

Usage:
  1. Flip adapter switch to MS
  2. python usb_ms_can_test.py

  Vehicle must be ON (not just ACC) for most signals.
"""

import sys
import time
import serial

DEFAULT_PORT = "/dev/tty.usbserial-0001"
DEFAULT_BAUD = 38400

# Known EV-CAN IDs we expect to see
EV_CAN_IDS = {
    "1DA": "Motor torque/RPM (inverter)",
    "1DB": "HV battery voltage + current (LBC)",
    "1DC": "Battery power limits (LBC)",
    "5BC": "GIDs / energy remaining (LBC)",
    "55B": "Battery SOC (LBC)",
    "54B": "HVAC status (climate module)",
    "54C": "Ambient temp / fan voltage",
    "54A": "Climate setpoint",
    "380": "Charger status (OBC)",
    "55A": "Motor/inverter temperatures",
    "5C0": "Battery temp / heater status",
    "59E": "Battery capacity (GIDs)",
    "1D4": "Torque request (VCM)",
    "1F2": "DC-DC converter",
    "50A": "Battery heater grant",
}

# Known Car-CAN IDs (if we see these, MS didn't switch buses)
CAR_CAN_IDS = {
    "174": "Gear position",
    "358": "Turn signals",
    "284": "Speed/odometer",
    "285": "Speed/odometer (redundant)",
    "180": "Steering/chassis",
    "1D5": "Torque/motor (Car-CAN copy)",
    "354": "Rolling counter",
}


def send(ser, cmd, wait=0.5, timeout=5.0):
    """Send command, return raw text."""
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
    return response.decode("ascii", errors="replace").replace(">", "").strip()


def monitor_bus(ser, protocol_name, duration=5):
    """Run ATMA for duration seconds, collect and classify CAN IDs seen."""
    print(f"\n  Monitoring ({protocol_name}) for {duration}s...")
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(duration)
    # Send byte to stop ATMA
    ser.write(b"\r")
    time.sleep(0.5)

    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)

    text = raw.decode("ascii", errors="replace").strip()
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n") if l.strip()]

    # Extract CAN IDs from lines (first token before space)
    seen_ids = {}
    errors = 0
    for line in lines:
        if "BUFFER FULL" in line or "STOPPED" in line or "SEARCHING" in line:
            continue
        if "CAN ERROR" in line or "ERROR" in line:
            errors += 1
            continue
        parts = line.split()
        if parts and len(parts[0]) == 3:
            can_id = parts[0].upper()
            seen_ids[can_id] = seen_ids.get(can_id, 0) + 1

    return seen_ids, errors, len(lines)


def classify_results(seen_ids):
    """Classify seen CAN IDs as EV-CAN, Car-CAN, or unknown."""
    ev_hits = {}
    car_hits = {}
    unknown = {}

    for can_id, count in sorted(seen_ids.items()):
        if can_id in EV_CAN_IDS:
            ev_hits[can_id] = (count, EV_CAN_IDS[can_id])
        elif can_id in CAR_CAN_IDS:
            car_hits[can_id] = (count, CAR_CAN_IDS[can_id])
        else:
            unknown[can_id] = count

    return ev_hits, car_hits, unknown


def try_protocol(ser, proto_num, proto_name, duration=5):
    """Try a CAN protocol and monitor for traffic."""
    print(f"\n{'─' * 55}")
    print(f"Protocol {proto_num}: {proto_name}")
    print(f"{'─' * 55}")

    send(ser, "ATZ", wait=1.5)
    send(ser, "ATE0")
    resp = send(ser, f"ATSP{proto_num}")
    print(f"  ATSP{proto_num}: {resp}")
    send(ser, "ATH1")
    send(ser, "ATS1")

    seen_ids, errors, total_lines = monitor_bus(ser, proto_name, duration)

    if errors > 0 and not seen_ids:
        print(f"  Result: CAN ERROR (no traffic at this speed)")
        return None

    if not seen_ids:
        print(f"  Result: No CAN frames received ({total_lines} lines, {errors} errors)")
        return None

    ev_hits, car_hits, unknown = classify_results(seen_ids)

    print(f"\n  Received {sum(seen_ids.values())} frames across {len(seen_ids)} CAN IDs")

    if ev_hits:
        print(f"\n  EV-CAN signals found ({len(ev_hits)}):")
        for can_id, (count, desc) in sorted(ev_hits.items()):
            print(f"    0x{can_id}  {count:4d} frames  {desc}")

    if car_hits:
        print(f"\n  Car-CAN signals found ({len(car_hits)}):")
        for can_id, (count, desc) in sorted(car_hits.items()):
            print(f"    0x{can_id}  {count:4d} frames  {desc}")

    if unknown:
        print(f"\n  Unknown CAN IDs ({len(unknown)}):")
        for can_id, count in sorted(unknown.items()):
            print(f"    0x{can_id}  {count:4d} frames")

    # Verdict
    if ev_hits and not car_hits:
        print(f"\n  >>> EV-CAN CONFIRMED — direct battery/motor bus access!")
    elif car_hits and not ev_hits:
        print(f"\n  >>> Car-CAN — same bus as HS mode (MS switch didn't change bus)")
    elif ev_hits and car_hits:
        print(f"\n  >>> MIXED — seeing both EV-CAN and Car-CAN IDs (VCM bridging?)")
    else:
        print(f"\n  >>> UNKNOWN BUS — CAN IDs don't match known Leaf signals")

    return seen_ids


def decode_sample_frames(ser):
    """If EV-CAN is confirmed, grab and decode a few key frames."""
    print(f"\n{'=' * 55}")
    print("Decoding sample EV-CAN frames")
    print(f"{'=' * 55}")

    send(ser, "ATZ", wait=1.5)
    send(ser, "ATE0")
    send(ser, "ATH1")
    send(ser, "ATS1")

    # 0x1DB — Battery voltage + current
    print(f"\n  0x1DB — HV Battery Voltage & Current")
    print(f"  {'─' * 45}")
    send(ser, "ATSP6")
    send(ser, "ATCRA 1DB")
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(1.0)
    ser.write(b"\r")
    time.sleep(0.3)
    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)
    text = raw.decode("ascii", errors="replace")
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n")
             if l.strip() and l.strip().startswith("1DB")]

    if lines:
        # Decode first valid frame
        parts = lines[0].split()
        if len(parts) >= 8:
            d = [int(x, 16) for x in parts[1:8]]
            # Current: bytes 0-1, 11 bits, two's complement, sign inverted
            raw_i = (d[0] << 3) | ((d[1] & 0xE0) >> 5)
            if raw_i & 0x0400:
                raw_i |= 0xF800
                raw_i -= 0x10000
            current_a = -raw_i / 2.0

            # Voltage: bytes 2-3, 10 bits
            raw_v = (d[2] << 2) | ((d[3] & 0xC0) >> 6)
            voltage_v = raw_v / 2.0

            power_kw = voltage_v * current_a / 1000.0

            print(f"  Raw frame: {lines[0]}")
            print(f"  Voltage:   {voltage_v:.1f} V")
            print(f"  Current:   {current_a:.1f} A  ({'discharge' if current_a > 0 else 'charge' if current_a < 0 else 'idle'})")
            print(f"  Power:     {power_kw:.2f} kW")
            print(f"  ({len(lines)} frames captured in 1s)")
        else:
            print(f"  Frame too short: {lines[0]}")
    else:
        print("  No 0x1DB frames received")

    # 0x5BC — GIDs
    print(f"\n  0x5BC — GIDs (Energy Remaining)")
    print(f"  {'─' * 45}")
    send(ser, "ATCRA 5BC")
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(2.0)
    ser.write(b"\r")
    time.sleep(0.3)
    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)
    text = raw.decode("ascii", errors="replace")
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n")
             if l.strip() and l.strip().startswith("5BC")]

    if lines:
        parts = lines[0].split()
        if len(parts) >= 6:
            d = [int(x, 16) for x in parts[1:6]]
            gids = (d[0] << 2) | ((d[1] & 0xC0) >> 6)
            kwh = gids * 77.5 / 1000.0
            avg_temp = d[3] - 40

            print(f"  Raw frame: {lines[0]}")
            print(f"  GIDs:      {gids}")
            print(f"  Energy:    {kwh:.1f} kWh  (at 77.5 Wh/GID)")
            print(f"  Avg temp:  {avg_temp} C")
            print(f"  ({len(lines)} frames captured in 2s)")
        else:
            print(f"  Frame too short: {lines[0]}")
    else:
        print("  No 0x5BC frames received")

    # 0x55B — SOC
    print(f"\n  0x55B — Battery SOC")
    print(f"  {'─' * 45}")
    send(ser, "ATCRA 55B")
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(1.0)
    ser.write(b"\r")
    time.sleep(0.3)
    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)
    text = raw.decode("ascii", errors="replace")
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n")
             if l.strip() and l.strip().startswith("55B")]

    if lines:
        parts = lines[0].split()
        if len(parts) >= 3:
            d = [int(x, 16) for x in parts[1:3]]
            raw_soc = (d[0] << 2) | (d[1] >> 6)
            if raw_soc != 0x3FF:
                soc = raw_soc / 10.0
                print(f"  Raw frame: {lines[0]}")
                print(f"  SOC:       {soc:.1f} %")
            else:
                print(f"  Raw frame: {lines[0]}")
                print(f"  SOC:       INVALID (0x3FF)")
            print(f"  ({len(lines)} frames captured in 1s)")
        else:
            print(f"  Frame too short: {lines[0]}")
    else:
        print("  No 0x55B frames received")


def main(port=DEFAULT_PORT, baud=DEFAULT_BAUD):
    print("USB MS-CAN Bus Test — 2012 Nissan Leaf")
    print(f"Port: {port} @ {baud}")
    print("=" * 55)
    print()
    print("IMPORTANT: Adapter switch must be in MS position!")
    print("Vehicle must be ON (not just ACC) for most signals.")

    ser = serial.Serial(
        port=port, baudrate=baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=1, write_timeout=1,
    )
    time.sleep(0.2)
    ser.reset_input_buffer()

    # Identify adapter
    print(f"\nAdapter: {send(ser, 'ATZ', wait=1.5)}")
    send(ser, "ATE0")

    # Adapter voltage (sanity check — confirms OBD power)
    rv = send(ser, "ATRV")
    print(f"OBD voltage: {rv}")

    # Try protocols — EV-CAN is 500k, but MS-CAN adapters
    # may use 125k or 250k depending on wiring
    ev_can_found = False

    protocols = [
        ("6", "CAN 500k 11-bit (EV-CAN expected)"),
        ("8", "CAN 250k 11-bit (Ford MS-CAN typical)"),
        ("7", "CAN 500k 29-bit"),
        ("9", "CAN 250k 29-bit"),
    ]

    for proto_num, proto_name in protocols:
        result = try_protocol(ser, proto_num, proto_name, duration=5)
        if result:
            ev_hits, car_hits, _ = classify_results(result)
            if ev_hits:
                ev_can_found = True
                # Try decoding some frames
                decode_sample_frames(ser)
                break

    if not ev_can_found:
        print(f"\n{'=' * 55}")
        print("EV-CAN not detected on MS switch.")
        print("Possible reasons:")
        print("  - MS routes to AV-CAN (pins 11/3) not EV-CAN (pins 12/13)")
        print("  - MS routes to Ford MS-CAN pins (not applicable to Leaf)")
        print("  - Adapter wiring doesn't match Leaf OBD pinout")
        print("  - Vehicle not in ON state")

    ser.close()
    print(f"\n{'=' * 55}")
    print("Done. Port closed.")


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD
    main(port, baud)
