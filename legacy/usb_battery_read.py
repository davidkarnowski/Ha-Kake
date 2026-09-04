#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
USB Serial Battery Status Reader — 2012 Nissan Leaf
Equivalent to battery_cell_read.py but uses USB serial instead of BLE.

Reads cell voltages, battery state, and temperatures from the BMS (LBC).

UDS requests to 0x79B, responses from 0x7BB:
  2101 — Battery state (SOC, capacity, 12V, insulation, HX)
  2102 — 96 cell pair voltages (millivolts)
  2104 — 4 temperature sensors

Usage:
  python usb_battery_read.py                  # single read
  python usb_battery_read.py --loop           # continuous monitoring
  python usb_battery_read.py --port /dev/...  # specify port
"""

import argparse
import datetime as dt
import sys
import time

import serial

DEFAULT_PORT = "/dev/tty.usbserial-0001"
DEFAULT_BAUD = 38400

NUM_CELLS = 96
NOMINAL_CAPACITY_AH = 66.0


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class SerialELM:
    """ELM327 command interface over USB serial."""

    def __init__(self, port, baud):
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1,
            write_timeout=1,
        )
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def send(self, cmd, wait=0.3, timeout=8.0):
        """Send AT/UDS command, collect response until '>' prompt."""
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
        # Strip echo if present
        if text.startswith(cmd):
            text = text[len(cmd):].strip()
        lines = [l.strip() for l in text.replace("\r", "\n").split("\n")
                 if l.strip() and l.strip() != "OK"]
        return lines

    def close(self):
        self.ser.close()


# ── Parsing / decoding (identical to battery_cell_read.py) ──────────────

def parse_isotp(lines):
    """Parse multi-frame ISO-TP response into raw payload bytes."""
    data = bytearray()
    for line in lines:
        parts = line.strip().split()
        if not parts or parts[0] != "7BB":
            continue
        hex_bytes = parts[1:]
        if not hex_bytes:
            continue
        pci = int(hex_bytes[0], 16)
        if (pci & 0xF0) == 0x10:
            for b in hex_bytes[4:]:
                data.append(int(b, 16))
        elif (pci & 0xF0) == 0x20:
            for b in hex_bytes[1:]:
                data.append(int(b, 16))
    return data


def decode_group01(data):
    """Decode group 01 (battery state) from 39-byte ZE0 payload."""
    if len(data) < 39:
        return None
    soc_raw = (data[29] << 16) | (data[30] << 8) | data[31]
    soc = soc_raw / 10000.0
    cap_raw = (data[33] << 16) | (data[34] << 8) | data[35]
    capacity_ah = cap_raw / 10000.0
    hx_raw = (data[26] << 8) | data[27]
    hx = hx_raw / 100.0
    lv_raw = (data[20] << 8) | data[21]
    lv_volts = lv_raw / 1024.0
    insulation_raw = (data[22] << 8) | data[23]
    soh = (capacity_ah / NOMINAL_CAPACITY_AH) * 100.0
    return {
        "soc": soc,
        "capacity_ah": capacity_ah,
        "soh": soh,
        "hx": hx,
        "lv_volts": lv_volts,
        "insulation_kohm": insulation_raw,
    }


def decode_group04(data):
    """Decode group 04 (temperatures) from ZE0 payload."""
    temps = []
    for i in range(4):
        offset = i * 3
        if offset + 2 < len(data):
            raw = (data[offset] << 8) | data[offset + 1]
            deg_c = data[offset + 2]
            if deg_c > 127:
                deg_c -= 256
            temps.append({"raw": raw, "deg_c": deg_c})
    return temps


def decode_cell_voltages(data):
    """Decode 96 cell pair voltages. 16-bit BE, millivolts."""
    voltages = []
    for i in range(NUM_CELLS):
        offset = i * 2
        if offset + 1 < len(data):
            mv = (data[offset] << 8) | data[offset + 1]
            if mv < 5000:
                voltages.append(mv)
            else:
                break
        else:
            break
    return voltages


def print_voltages(voltages):
    """Pretty-print cell voltages with bar chart."""
    if not voltages:
        print("  No voltage data.")
        return
    min_v = min(voltages)
    max_v = max(voltages)
    avg_v = sum(voltages) / len(voltages)
    spread = max_v - min_v

    print(f"\n  {'Cell':<8} {'mV':>6}  {'Volts':>7}  {'Bar'}")
    print(f"  {'─'*8} {'─'*6}  {'─'*7}  {'─'*40}")

    for i, mv in enumerate(voltages):
        marker = ""
        if mv == min_v:
            marker = " << MIN"
        elif mv == max_v:
            marker = " << MAX"
        bar_len = max(0, (mv - min_v + 2) // 3)
        bar = "#" * min(bar_len, 40)
        print(f"  Cell {i:2d}  {mv:5d}  {mv/1000:.3f} V  {bar}{marker}")

    print(f"\n  {'─'*50}")
    print(f"  Min:      {min_v:5d} mV  ({min_v/1000:.3f} V)  Cell {voltages.index(min_v)}")
    print(f"  Max:      {max_v:5d} mV  ({max_v/1000:.3f} V)  Cell {voltages.index(max_v)}")
    print(f"  Avg:      {avg_v:5.0f} mV  ({avg_v/1000:.3f} V)")
    print(f"  Spread:   {spread:5d} mV  ({spread/1000:.3f} V)")
    print(f"  Pack sum: {sum(voltages)/1000:.1f} V")


# ── Main ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="USB Battery Reader — 2012 Nissan Leaf")
    ap.add_argument("--loop", action="store_true", help="Continuous monitoring")
    ap.add_argument("--interval", type=int, default=5, help="Seconds between reads in loop mode")
    ap.add_argument("--port", default=DEFAULT_PORT, help=f"Serial port (default: {DEFAULT_PORT})")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate (default: {DEFAULT_BAUD})")
    args = ap.parse_args()

    print("Battery Status Reader (USB) — 2012 Nissan Leaf")
    print("=" * 55)
    print(f"Port: {args.port} @ {args.baud} baud")
    print("Connecting...\n")

    try:
        elm = SerialELM(args.port, args.baud)
    except serial.SerialException as e:
        print(f"ERROR: Could not open {args.port}: {e}")
        sys.exit(1)

    print("Configuring adapter...")
    elm.send("ATZ", wait=1.5)
    elm.send("ATE0")
    elm.send("ATL1")
    elm.send("ATH1")
    elm.send("ATS1")
    elm.send("ATSP6")

    elm.send("ATSH 79B")
    elm.send("ATCRA 7BB")
    elm.send("ATCAF1")
    elm.send("ATFCSH 79B")
    elm.send("ATFCSD 30 00 20")
    elm.send("ATFCSM1")

    print("Ready.\n")

    try:
        while True:
            # ── Battery State (Group 01) ──
            print(f"{'=' * 55}")
            print(f"  {ts()}")
            print(f"{'=' * 55}")

            resp01 = elm.send("2101", wait=0.5, timeout=10.0)
            if resp01 and "NO DATA" not in " ".join(resp01):
                data01 = parse_isotp(resp01)
                state = decode_group01(data01)
                if state:
                    print(f"\n  Battery State")
                    print(f"  {'─'*40}")
                    print(f"  SOC:          {state['soc']:6.2f} %")
                    print(f"  Capacity:     {state['capacity_ah']:6.2f} Ah  (nominal {NOMINAL_CAPACITY_AH:.0f} Ah)")
                    print(f"  SOH:          {state['soh']:6.1f} %")
                    print(f"  HX:           {state['hx']:6.2f} %")
                    print(f"  12V battery:  {state['lv_volts']:6.2f} V")
                    print(f"  Insulation:   {state['insulation_kohm']:5d} kOhm")
                else:
                    print(f"  Group 01: decode failed (got {len(data01)} bytes, need 39)")
            else:
                print(f"  Group 01: no response ({resp01})")

            # ── Temperatures (Group 04) ──
            resp04 = elm.send("2104", wait=0.5, timeout=10.0)
            if resp04 and "NO DATA" not in " ".join(resp04):
                data04 = parse_isotp(resp04)
                temps = decode_group04(data04)
                if temps:
                    print(f"\n  Battery Temperatures")
                    print(f"  {'─'*40}")
                    for i, t in enumerate(temps):
                        print(f"  Sensor {i+1}:  {t['deg_c']:3d} °C   (raw: {t['raw']})")
                    avg_t = sum(t['deg_c'] for t in temps) / len(temps)
                    print(f"  Average:   {avg_t:.1f} °C")
            else:
                print(f"  Group 04: no response ({resp04})")

            # ── Cell Voltages (Group 02) ──
            print(f"\n  Cell Pair Voltages")
            print(f"  {'─'*40}")

            resp02 = elm.send("2102", wait=0.5, timeout=15.0)
            if resp02 and "NO DATA" not in " ".join(resp02):
                data02 = parse_isotp(resp02)
                voltages = decode_cell_voltages(data02)

                if len(voltages) < NUM_CELLS:
                    print(f"  Incomplete: {len(voltages)}/{NUM_CELLS} cells")
                else:
                    print(f"  {len(voltages)} cells, {len(resp02)} frames")

                print_voltages(voltages)
            else:
                print(f"  Group 02: no response ({resp02})")

            if not args.loop:
                break

            print(f"\n  Next read in {args.interval}s... (Ctrl+C to stop)\n")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        elm.close()
        print("Port closed.")


if __name__ == "__main__":
    main()
