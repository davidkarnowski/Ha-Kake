#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Energy Data Decoder — 2012 Nissan Leaf
Decodes newly discovered UDS groups and probes VCM diagnostic sessions.

Part 1: LBC groups 03, 05, 06 (unknown payloads — raw dump + decode attempt)
Part 2: HVAC group 01 (climate state)
Part 3: VCM diagnostic session unlock (10 03) then retry groups
Part 4: BCM group 01 (body electrical — may show accessory loads)

Usage: python usb_energy_decode.py
       Adapter on HS, vehicle ON.
"""

import sys
import time
import serial

DEFAULT_PORT = "/dev/tty.usbserial-0001"
DEFAULT_BAUD = 38400


class SerialELM:
    def __init__(self, port, baud):
        self.ser = serial.Serial(
            port=port, baudrate=baud,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=1, write_timeout=1,
        )
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def send(self, cmd, wait=0.3, timeout=8.0):
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode("ascii"))
        response = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting > 0:
                response += self.ser.read(self.ser.in_waiting)
                if b">" in response:
                    break
            else:
                time.sleep(0.05)
        time.sleep(wait)
        text = response.decode("ascii", errors="replace").replace(">", "").strip()
        if text.startswith(cmd):
            text = text[len(cmd):].strip()
        return [l.strip() for l in text.replace("\r", "\n").split("\n")
                if l.strip() and l.strip() != "OK"]

    def configure(self, tx_hdr, rx_filter):
        """Configure for a specific ECU."""
        self.send("ATZ", wait=1.5)
        self.send("ATE0")
        self.send("ATL1")
        self.send("ATH1")
        self.send("ATS1")
        self.send("ATSP6")
        self.send(f"ATSH {tx_hdr}")
        self.send(f"ATCRA {rx_filter}")
        self.send("ATCAF1")
        self.send(f"ATFCSH {tx_hdr}")
        self.send("ATFCSD 30 00 20")
        self.send("ATFCSM1")

    def close(self):
        self.ser.close()


def parse_isotp(lines, rx_id):
    """Parse multi-frame ISO-TP response into raw payload bytes."""
    data = bytearray()
    for line in lines:
        parts = line.strip().split()
        if not parts or parts[0] != rx_id:
            continue
        hx = parts[1:]
        if not hx:
            continue
        pci = int(hx[0], 16)
        if (pci & 0xF0) == 0x10:
            for b in hx[4:]:
                data.append(int(b, 16))
        elif (pci & 0xF0) == 0x20:
            for b in hx[1:]:
                data.append(int(b, 16))
        elif pci < 0x10:
            # Single frame: skip PCI and service bytes
            for b in hx[3:3 + pci - 2]:
                data.append(int(b, 16))
    return data


def hex_dump(data, prefix="    "):
    """Pretty hex dump with offset."""
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:3d}: {hex_part:<48}  {ascii_part}")


def print_raw_lines(lines, rx_id, max_lines=40):
    """Print raw response lines."""
    data_lines = [l for l in lines if l.startswith(rx_id)]
    for l in data_lines[:max_lines]:
        print(f"    {l}")
    if len(data_lines) > max_lines:
        print(f"    ... ({len(data_lines) - max_lines} more)")
    return data_lines


# ── Part 1: LBC Groups 03, 05, 06 ──────────────────────────────────────

def part1_lbc_new_groups(elm):
    print(f"\n{'=' * 60}")
    print("PART 1: LBC (Battery Controller) — Groups 03, 05, 06")
    print(f"{'=' * 60}")

    elm.configure("79B", "7BB")

    # Group 03
    print(f"\n{'─' * 60}")
    print("LBC Group 03  (0x79B → 0x7BB, command: 2103)")
    print(f"{'─' * 60}")
    lines = elm.send("2103", wait=0.5, timeout=10.0)
    print("  Raw frames:")
    print_raw_lines(lines, "7BB")
    data = parse_isotp(lines, "7BB")
    print(f"\n  Payload: {len(data)} bytes")
    hex_dump(data)

    if len(data) >= 10:
        print(f"\n  Decode attempt (Group 03 — battery shunts/current):")
        # Community docs suggest group 03 has shunt data and current
        # Byte 0-1: battery current (signed, 10-bit or 16-bit)
        # Byte 2-3: shunt resistor mV
        for i in range(0, min(len(data), 20), 2):
            val_u = (data[i] << 8) | data[i+1]
            val_s = val_u - 0x10000 if val_u > 0x7FFF else val_u
            print(f"    Bytes {i:2d}-{i+1:2d}: 0x{val_u:04X} = {val_u:6d} (unsigned)  {val_s:6d} (signed)")

    # Group 05
    print(f"\n{'─' * 60}")
    print("LBC Group 05  (0x79B → 0x7BB, command: 2105)")
    print(f"{'─' * 60}")
    lines = elm.send("2105", wait=0.5, timeout=10.0)
    print("  Raw frames:")
    print_raw_lines(lines, "7BB")
    data = parse_isotp(lines, "7BB")
    print(f"\n  Payload: {len(data)} bytes")
    hex_dump(data)

    if len(data) >= 20:
        print(f"\n  Decode attempt (Group 05 — extended battery data):")
        print(f"  Scanning for voltage-like values (300-420V range):")
        for i in range(0, len(data) - 1):
            val = (data[i] << 8) | data[i+1]
            # Check for values that look like pack voltage (scaled)
            for scale in [1, 2, 0.5, 0.1, 10]:
                v = val * scale
                if 300 <= v <= 420:
                    print(f"    Bytes {i:2d}-{i+1:2d}: 0x{val:04X} = {val} × {scale} = {v:.1f} V ?")
            # Check for current-like values (scaled, signed)
            val_s = val - 0x10000 if val > 0x7FFF else val
            for scale in [0.1, 0.5, 1, 0.01]:
                a = val_s * scale
                if -100 <= a <= 100 and a != 0:
                    if abs(a) > 0.5:  # filter noise
                        print(f"    Bytes {i:2d}-{i+1:2d}: 0x{val:04X} = {val_s} × {scale} = {a:.2f} A ?")

    # Group 06
    print(f"\n{'─' * 60}")
    print("LBC Group 06  (0x79B → 0x7BB, command: 2106)")
    print(f"{'─' * 60}")
    lines = elm.send("2106", wait=0.5, timeout=10.0)
    print("  Raw frames:")
    print_raw_lines(lines, "7BB")
    data = parse_isotp(lines, "7BB")
    print(f"\n  Payload: {len(data)} bytes")
    hex_dump(data)

    if len(data) >= 4:
        print(f"\n  Decode attempt (Group 06 — cell balancing/flags):")
        for i, b in enumerate(data):
            if b != 0x00 and b != 0xFF:
                bits = f"{b:08b}"
                print(f"    Byte {i:2d}: 0x{b:02X} = {b:3d} = {bits}")


# ── Part 2: HVAC ───────────────────────────────────────────────────────

def part2_hvac(elm):
    print(f"\n{'=' * 60}")
    print("PART 2: HVAC (Climate Control) — Group 01")
    print(f"{'=' * 60}")

    elm.configure("744", "764")

    print(f"\n{'─' * 60}")
    print("HVAC Group 01  (0x744 → 0x764, command: 2101)")
    print(f"{'─' * 60}")
    lines = elm.send("2101", wait=0.5, timeout=5.0)
    print("  Raw frames:")
    print_raw_lines(lines, "764")
    data = parse_isotp(lines, "764")
    print(f"\n  Payload: {len(data)} bytes")
    hex_dump(data)

    if len(data) >= 5:
        print(f"\n  Decode attempt:")
        for i, b in enumerate(data):
            val_s = b - 256 if b > 127 else b
            # Temperature-like values
            desc = ""
            if 10 <= b <= 50:
                desc += f"  temp? {b}°C / {b*9//5+32}°F"
            if 150 <= b <= 250:
                desc += f"  temp? {b-256}°C (signed)"
            # Known HVAC byte patterns from 0x54B
            if i == 0:
                desc += f"  (HVAC byte 0)"
            print(f"    Byte {i:2d}: 0x{b:02X} = {b:3d} / {val_s:4d}{desc}")

    # Try reading multiple times to see which bytes change
    print(f"\n  Stability check (3 reads, looking for changing bytes):")
    reads = []
    for r in range(3):
        time.sleep(1.0)
        lines = elm.send("2101", wait=0.5, timeout=5.0)
        d = parse_isotp(lines, "764")
        reads.append(d)
        print(f"    Read {r+1}: {' '.join(f'{b:02X}' for b in d)}")

    if len(reads) >= 2 and all(len(r) == len(reads[0]) for r in reads):
        changing = []
        for i in range(len(reads[0])):
            vals = set(r[i] for r in reads)
            if len(vals) > 1:
                changing.append(i)
        if changing:
            print(f"    Changing bytes: {changing}")
        else:
            print(f"    All bytes stable across reads")


# ── Part 3: VCM Diagnostic Session ────────────────────────────────────

def part3_vcm_session(elm):
    print(f"\n{'=' * 60}")
    print("PART 3: VCM (Vehicle Control Module) — Diagnostic Session")
    print(f"{'=' * 60}")
    print("Previous attempts returned 7F 21 80 (conditions not correct).")
    print("Trying extended diagnostic sessions...\n")

    elm.configure("797", "79A")

    # Try different diagnostic sessions
    sessions = [
        ("1001", "Default session"),
        ("1002", "Programming session"),
        ("1003", "Extended diagnostic session"),
        ("1040", "Nissan-specific session 0x40"),
        ("1081", "Nissan-specific session 0x81"),
        ("10C0", "Nissan-specific session 0xC0"),
    ]

    working_session = None
    for sess_cmd, sess_name in sessions:
        print(f"  Trying session {sess_cmd} ({sess_name})...")
        lines = elm.send(sess_cmd, wait=0.5, timeout=3.0)
        resp = " ".join(lines) if lines else "(no response)"

        # Check for positive response (50 xx)
        is_positive = any("50" in l for l in lines) if lines else False
        # Check for negative response (7F)
        is_negative = any("7F" in l for l in lines) if lines else False

        status = "ACCEPTED" if is_positive else "REJECTED" if is_negative else "NO RESPONSE"
        print(f"    Response: {resp}  [{status}]")

        if is_positive:
            working_session = sess_cmd
            # Now try UDS groups
            print(f"\n    Session active! Trying diagnostic groups...")
            for group in ["2101", "2102", "2103", "2104", "2105", "2106",
                          "2108", "210A", "210C", "2110", "2120", "2130"]:
                lines = elm.send(group, wait=0.3, timeout=5.0)
                has_data = (lines
                            and "NO DATA" not in " ".join(lines)
                            and "7F" not in " ".join(lines))
                if has_data:
                    data = parse_isotp(lines, "79A")
                    data_lines = [l for l in lines if l.startswith("79A")]
                    print(f"\n    >>> {group}: {len(data_lines)} frames, {len(data)} bytes payload")
                    print_raw_lines(lines, "79A")
                    print(f"    Hex dump:")
                    hex_dump(data, prefix="      ")
                else:
                    error = ""
                    if lines and "7F" in " ".join(lines):
                        # Extract NRC
                        for l in lines:
                            if "7F" in l:
                                error = f" (NRC: {l})"
                    print(f"    {group}: no data{error}")

            # Also try standard OBD service 01 (might unlock after session)
            print(f"\n    Trying standard OBD PIDs in this session...")
            for pid in ["0100", "0105", "010C", "010D", "0151"]:
                lines = elm.send(pid, wait=0.3, timeout=3.0)
                has_data = (lines
                            and "NO DATA" not in " ".join(lines)
                            and "7F" not in " ".join(lines)
                            and "ERROR" not in " ".join(lines))
                if has_data:
                    print(f"    >>> {pid}: {' '.join(lines)}")
                else:
                    print(f"    {pid}: no data")

            break

    if not working_session:
        print(f"\n  No diagnostic session accepted by VCM.")
        print(f"  The VCM may require security access (27 service) or")
        print(f"  Nissan CONSULT-III protocol instead of standard UDS.")


# ── Part 4: BCM ────────────────────────────────────────────────────────

def part4_bcm(elm):
    print(f"\n{'=' * 60}")
    print("PART 4: BCM (Body Control Module) — Group 01")
    print(f"{'=' * 60}")

    elm.configure("745", "765")

    print(f"\n{'─' * 60}")
    print("BCM Group 01  (0x745 → 0x765, command: 2101)")
    print(f"{'─' * 60}")
    lines = elm.send("2101", wait=0.5, timeout=10.0)
    print("  Raw frames:")
    print_raw_lines(lines, "765")
    data = parse_isotp(lines, "765")
    print(f"\n  Payload: {len(data)} bytes")
    hex_dump(data)

    if len(data) >= 10:
        print(f"\n  Scanning for notable values:")
        for i in range(len(data)):
            b = data[i]
            desc = []
            if 10 <= b <= 50:
                desc.append(f"temp? {b}°C")
            if i < len(data) - 1:
                val16 = (data[i] << 8) | data[i+1]
                # 12V battery voltage (scale /1024 or /100)
                v1024 = val16 / 1024.0
                v100 = val16 / 100.0
                if 11.5 <= v1024 <= 15.0:
                    desc.append(f"12V? {v1024:.2f}V (/1024)")
                if 11.5 <= v100 <= 15.0:
                    desc.append(f"12V? {v100:.2f}V (/100)")
            if desc:
                print(f"    Byte {i:2d}: 0x{b:02X} = {b:3d}  {'  |  '.join(desc)}")

    # Also grab groups 04, 05
    for grp in ["2104", "2105"]:
        print(f"\n  BCM {grp}:")
        lines = elm.send(grp, wait=0.5, timeout=5.0)
        data = parse_isotp(lines, "765")
        if data:
            print(f"    {len(data)} bytes: {' '.join(f'{b:02X}' for b in data)}")
        else:
            print(f"    No data")


# ── Main ────────────────────────────────────────────────────────────────

def main(port=DEFAULT_PORT, baud=DEFAULT_BAUD):
    print("Energy Data Decoder — 2012 Nissan Leaf")
    print(f"Port: {port} @ {baud}")
    print("Adapter on HS, vehicle ON.")
    print("=" * 60)

    elm = SerialELM(port, baud)

    try:
        part1_lbc_new_groups(elm)
        part2_hvac(elm)
        part3_vcm_session(elm)
        part4_bcm(elm)
    finally:
        elm.close()

    print(f"\n{'=' * 60}")
    print("Done. Port closed.")


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD
    main(port, baud)
