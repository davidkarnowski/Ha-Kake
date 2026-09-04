#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Energy Signal Probe — 2012 Nissan Leaf
Two-phase test to find energy/power data accessible from Car-CAN:

Phase 1: Passive scan — monitor Car-CAN for known energy-related CAN IDs
Phase 2: UDS polling — send diagnostic requests to VCM, inverter, and other
         ECUs through the VCM bridge to pull power/energy data

Adapter must be on HS (Car-CAN). Vehicle must be ON.

Usage: python usb_energy_probe.py
"""

import sys
import time
import serial

DEFAULT_PORT = "/dev/tty.usbserial-0001"
DEFAULT_BAUD = 38400

# CAN IDs we're looking for on Car-CAN (some may be bridged from EV-CAN)
ENERGY_IDS = {
    # EV-CAN origin (may or may not appear on Car-CAN)
    "1DA": "Motor torque/RPM (inverter)",
    "1DB": "HV battery voltage + current",
    "1DC": "Battery power limits",
    "1D4": "Torque request (VCM→inverter)",
    "1F2": "DC-DC converter",
    "5BC": "GIDs / energy remaining",
    "55B": "Battery SOC",
    "55A": "Motor/inverter temps",
    "54A": "Climate setpoint",
    "54B": "HVAC status",
    "54C": "Ambient temp / fan voltage",
    "54F": "Cabin temperature",
    "5C0": "Battery temp / heater",
    "59E": "Battery capacity",
    "380": "Charger status (OBC)",
    "50A": "Battery heater grant",
    # Known Car-CAN
    "1D5": "Torque/motor (Car-CAN)",
    "174": "Gear position",
    "260": "Available power display",
    "284": "Speed/odometer",
    "180": "Steering/chassis",
    "358": "Turn signals",
}

# Known ECU diagnostic addresses for UDS polling
# Format: (tx_header, rx_filter, name)
ECUS = [
    ("797", "79A", "VCM (Vehicle Control Module)"),
    ("79B", "7BB", "LBC (Lithium Battery Controller)"),
    ("793", "7BD", "Inverter / Motor Controller"),
    ("743", "763", "ABS / VDC Module"),
    ("744", "764", "Climate Control (HVAC)"),
    ("745", "765", "BCM (Body Control Module)"),
    ("746", "766", "Steering Column"),
    ("784", "78C", "EPS (Electric Power Steering)"),
]

# UDS groups to try on each ECU
UDS_GROUPS = [
    ("2101", "Group 01 — primary status"),
    ("2102", "Group 02 — extended data"),
    ("2103", "Group 03"),
    ("2104", "Group 04 — temperatures"),
    ("2105", "Group 05"),
    ("2106", "Group 06"),
]


def send(ser, cmd, wait=0.3, timeout=5.0):
    """Send command, return response lines."""
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
    if text.startswith(cmd):
        text = text[len(cmd):].strip()
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n")
             if l.strip() and l.strip() != "OK"]
    return lines


def send_raw(ser, cmd, wait=0.3, timeout=5.0):
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


# ── Phase 1: Passive Car-CAN Scan ──────────────────────────────────────

def phase1_passive_scan(ser, duration=8):
    print(f"\n{'=' * 60}")
    print("PHASE 1: Passive Car-CAN Scan")
    print(f"{'=' * 60}")
    print(f"Monitoring all Car-CAN traffic for {duration}s...")
    print("Looking for energy-related CAN IDs (especially EV-CAN bridges)\n")

    send_raw(ser, "ATZ", wait=1.5)
    send_raw(ser, "ATE0")
    send_raw(ser, "ATSP6")
    send_raw(ser, "ATH1")
    send_raw(ser, "ATS1")

    # Monitor all traffic
    ser.reset_input_buffer()
    ser.write(b"ATMA\r")
    time.sleep(duration)
    ser.write(b"\r")
    time.sleep(0.5)

    raw = b""
    while ser.in_waiting > 0:
        raw += ser.read(ser.in_waiting)
        time.sleep(0.1)

    text = raw.decode("ascii", errors="replace")
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n") if l.strip()]

    # Tally CAN IDs and keep sample frames
    seen = {}  # can_id -> {"count": N, "sample": "raw line"}
    for line in lines:
        if "BUFFER" in line or "STOPPED" in line or "SEARCHING" in line:
            continue
        parts = line.split()
        if parts and len(parts[0]) == 3 and all(c in "0123456789ABCDEFabcdef" for c in parts[0]):
            can_id = parts[0].upper()
            if can_id not in seen:
                seen[can_id] = {"count": 0, "sample": line}
            seen[can_id]["count"] += 1

    # Classify
    energy_found = {}
    other_found = {}

    for can_id in sorted(seen.keys()):
        info = seen[can_id]
        if can_id in ENERGY_IDS:
            energy_found[can_id] = info
        else:
            other_found[can_id] = info

    print(f"Total: {sum(v['count'] for v in seen.values())} frames, {len(seen)} unique CAN IDs\n")

    if energy_found:
        print(f"Energy-related CAN IDs found ({len(energy_found)}):")
        print(f"  {'ID':<6} {'Count':>6}  {'Signal':<40} {'Sample Frame'}")
        print(f"  {'─'*6} {'─'*6}  {'─'*40} {'─'*40}")
        for can_id, info in sorted(energy_found.items()):
            desc = ENERGY_IDS.get(can_id, "?")
            print(f"  0x{can_id:<4} {info['count']:>5}  {desc:<40} {info['sample'][:50]}")
    else:
        print("No energy-related CAN IDs found on Car-CAN passive scan.")

    if other_found:
        print(f"\nOther CAN IDs ({len(other_found)}):")
        for can_id, info in sorted(other_found.items()):
            print(f"  0x{can_id:<4} {info['count']:>5}  {info['sample'][:60]}")

    # Try to decode any energy frames we found
    if "1DB" in energy_found:
        print(f"\n  Decoding 0x1DB sample:")
        decode_1db(energy_found["1DB"]["sample"])

    if "1DA" in energy_found:
        print(f"\n  Decoding 0x1DA sample:")
        decode_1da(energy_found["1DA"]["sample"])

    if "5BC" in energy_found:
        print(f"\n  Decoding 0x5BC sample:")
        decode_5bc(energy_found["5BC"]["sample"])

    if "260" in energy_found:
        print(f"\n  Decoding 0x260 sample:")
        decode_260(energy_found["260"]["sample"])

    return energy_found


def decode_1db(line):
    """Decode 0x1DB — HV battery voltage + current."""
    parts = line.split()
    if len(parts) < 8:
        print(f"    Too short: {line}")
        return
    d = [int(x, 16) for x in parts[1:8]]
    raw_i = (d[0] << 3) | ((d[1] & 0xE0) >> 5)
    if raw_i & 0x0400:
        raw_i |= 0xF800
        raw_i -= 0x10000
    current_a = -raw_i / 2.0
    raw_v = (d[2] << 2) | ((d[3] & 0xC0) >> 6)
    voltage_v = raw_v / 2.0
    power_kw = voltage_v * current_a / 1000.0
    print(f"    Voltage: {voltage_v:.1f} V | Current: {current_a:.1f} A | Power: {power_kw:.2f} kW")


def decode_1da(line):
    """Decode 0x1DA — Motor torque + RPM."""
    parts = line.split()
    if len(parts) < 7:
        print(f"    Too short: {line}")
        return
    d = [int(x, 16) for x in parts[1:7]]
    raw_torque = ((d[2] & 0x07) << 8) | d[3]
    if raw_torque & 0x0400:
        raw_torque |= 0xF800
        raw_torque -= 0x10000
    torque_nm = raw_torque / 2.0
    raw_rpm = (d[4] << 8) | d[5]
    if raw_rpm & 0x4000:
        raw_rpm |= 0x8000
        raw_rpm -= 0x10000
    rpm = raw_rpm / 2.0
    motor_kw = rpm * torque_nm * 0.10472 / 1000.0
    print(f"    Torque: {torque_nm:.1f} Nm | RPM: {rpm:.0f} | Motor power: {motor_kw:.2f} kW")


def decode_5bc(line):
    """Decode 0x5BC — GIDs."""
    parts = line.split()
    if len(parts) < 5:
        print(f"    Too short: {line}")
        return
    d = [int(x, 16) for x in parts[1:5]]
    gids = (d[0] << 2) | ((d[1] & 0xC0) >> 6)
    kwh = gids * 77.5 / 1000.0
    avg_temp = d[2] - 40
    print(f"    GIDs: {gids} | Energy: {kwh:.1f} kWh | Avg temp: {avg_temp} C")


def decode_260(line):
    """Decode 0x260 — Available power display."""
    parts = line.split()
    if len(parts) < 3:
        print(f"    Too short: {line}")
        return
    d = [int(x, 16) for x in parts[1:3]]
    drive_kw = d[0]
    regen_kw = d[1]
    print(f"    Available drive: {drive_kw} kW | Available regen: {regen_kw} kW")


# ── Phase 2: UDS Polling ───────────────────────────────────────────────

def phase2_uds_poll(ser):
    print(f"\n{'=' * 60}")
    print("PHASE 2: UDS Diagnostic Polling")
    print(f"{'=' * 60}")
    print("Probing ECUs for energy/power diagnostic data via UDS...\n")

    for tx_hdr, rx_filter, ecu_name in ECUS:
        print(f"{'─' * 60}")
        print(f"ECU: {ecu_name}  (TX: 0x{tx_hdr} → RX: 0x{rx_filter})")
        print(f"{'─' * 60}")

        # Configure for this ECU
        send_raw(ser, "ATZ", wait=1.5)
        send_raw(ser, "ATE0")
        send_raw(ser, "ATL1")
        send_raw(ser, "ATH1")
        send_raw(ser, "ATS1")
        send_raw(ser, "ATSP6")
        send_raw(ser, f"ATSH {tx_hdr}")
        send_raw(ser, f"ATCRA {rx_filter}")
        send_raw(ser, "ATCAF1")
        send_raw(ser, f"ATFCSH {tx_hdr}")
        send_raw(ser, "ATFCSD 30 00 20")
        send_raw(ser, "ATFCSM1")

        any_response = False
        for group_cmd, group_desc in UDS_GROUPS:
            lines = send(ser, group_cmd, wait=0.5, timeout=5.0)

            # Check for valid response
            has_data = (lines
                        and "NO DATA" not in " ".join(lines)
                        and "CAN ERROR" not in " ".join(lines)
                        and "ERROR" not in " ".join(lines))

            if has_data:
                any_response = True
                # Count data frames (lines starting with rx_filter)
                data_lines = [l for l in lines if l.startswith(rx_filter)]
                total_bytes = sum(len(l.split()) - 1 for l in data_lines)
                print(f"  {group_cmd} ({group_desc}): {len(data_lines)} frames, ~{total_bytes} bytes")
                # Show first 3 lines as sample
                for l in data_lines[:3]:
                    print(f"    {l}")
                if len(data_lines) > 3:
                    print(f"    ... ({len(data_lines) - 3} more)")

        if not any_response:
            print(f"  No response from this ECU (all groups returned NO DATA)")

        print()


# ── Phase 3: Targeted Decode of Known Car-CAN Energy IDs ──────────────

def phase3_targeted_capture(ser, ids_to_capture):
    """Capture specific CAN IDs with ATCRA filter for clean decoding."""
    print(f"\n{'=' * 60}")
    print("PHASE 3: Targeted Capture of Energy CAN IDs")
    print(f"{'=' * 60}")

    send_raw(ser, "ATZ", wait=1.5)
    send_raw(ser, "ATE0")
    send_raw(ser, "ATSP6")
    send_raw(ser, "ATH1")
    send_raw(ser, "ATS1")

    targets = {
        "1DB": ("HV Battery V/I", decode_1db, 1.0),
        "1DA": ("Motor Torque/RPM", decode_1da, 1.0),
        "1D5": ("Torque (Car-CAN)", None, 1.0),
        "5BC": ("GIDs", decode_5bc, 2.0),
        "260": ("Available Power", decode_260, 1.0),
        "54B": ("HVAC Status", None, 1.0),
        "55B": ("SOC", None, 1.0),
    }

    for can_id in ids_to_capture:
        if can_id not in targets:
            continue
        desc, decoder, duration = targets[can_id]

        print(f"\n  0x{can_id} — {desc}")
        print(f"  {'─' * 50}")

        send_raw(ser, f"ATCRA {can_id}")
        ser.reset_input_buffer()
        ser.write(b"ATMA\r")
        time.sleep(duration)
        ser.write(b"\r")
        time.sleep(0.3)

        raw = b""
        while ser.in_waiting > 0:
            raw += ser.read(ser.in_waiting)
            time.sleep(0.1)

        text = raw.decode("ascii", errors="replace")
        lines = [l.strip() for l in text.replace("\r", "\n").split("\n")
                 if l.strip() and l.strip().startswith(can_id)]

        if lines:
            print(f"  {len(lines)} frames in {duration}s ({len(lines)/duration:.0f}/s)")
            # Decode first and last
            if decoder:
                print(f"  First: ", end="")
                decoder(lines[0])
                if len(lines) > 1:
                    print(f"  Last:  ", end="")
                    decoder(lines[-1])
            else:
                print(f"  Sample: {lines[0]}")
                if len(lines) > 1:
                    print(f"  Sample: {lines[-1]}")
        else:
            print(f"  No frames received")

        # Reset filter
        send_raw(ser, "ATAR")


# ── Main ────────────────────────────────────────────────────────────────

def main(port=DEFAULT_PORT, baud=DEFAULT_BAUD):
    print("Energy Signal Probe — 2012 Nissan Leaf")
    print(f"Port: {port} @ {baud}")
    print("Adapter must be on HS. Vehicle must be ON.")
    print("=" * 60)

    ser = serial.Serial(
        port=port, baudrate=baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=1, write_timeout=1,
    )
    time.sleep(0.2)
    ser.reset_input_buffer()

    # Phase 1: Passive scan
    energy_found = phase1_passive_scan(ser, duration=8)

    # Phase 2: UDS polling
    phase2_uds_poll(ser)

    # Phase 3: Targeted capture of any energy IDs found in Phase 1
    if energy_found:
        phase3_targeted_capture(ser, list(energy_found.keys()))

    ser.close()
    print(f"\n{'=' * 60}")
    print("Done. Port closed.")


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD
    main(port, baud)
