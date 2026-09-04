#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Real-Time Power Monitor — 2012 Nissan Leaf
Polls LBC groups 01 + 05 to display live pack voltage, current, and power.

Usage: python usb_power_monitor.py
       python usb_power_monitor.py --interval 2
       Adapter on HS, vehicle ON.
"""

import argparse
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

    def close(self):
        self.ser.close()


def parse_isotp(lines, rx_id="7BB"):
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
    return data


def decode_power(g01_data, g05_data):
    """Decode power-related values from LBC groups 01 and 05."""
    result = {}

    # Group 01: SOC, capacity, 12V
    if len(g01_data) >= 36:
        soc_raw = (g01_data[29] << 16) | (g01_data[30] << 8) | g01_data[31]
        result["soc"] = soc_raw / 10000.0
        cap_raw = (g01_data[33] << 16) | (g01_data[34] << 8) | g01_data[35]
        result["capacity_ah"] = cap_raw / 10000.0

    # Group 05: current, cell voltages, power
    if len(g05_data) >= 24:
        # Cell max voltage (bytes 6-7, mV)
        result["cell_max_mv"] = (g05_data[6] << 8) | g05_data[7]
        # Cell min voltage (bytes 8-9, mV)
        result["cell_min_mv"] = (g05_data[8] << 8) | g05_data[9]

        # Discharge flag (bytes 20-21)
        discharge_flag = (g05_data[20] << 8) | g05_data[21]
        result["discharging"] = discharge_flag == 0xFFFF

        # Pack current (bytes 22-23, signed, ×0.001 A)
        raw_current = (g05_data[22] << 8) | g05_data[23]
        if raw_current > 0x7FFF:
            raw_current -= 0x10000
        result["current_raw"] = raw_current

        # If discharging, current is negative in raw; negate for "discharge = positive"
        if result["discharging"]:
            result["current_a"] = -raw_current * 0.001
        else:
            result["current_a"] = raw_current * 0.001

        # Pack voltage from cell min/max midpoint × 96 cells (rough estimate)
        # Better: use it combined with group 01 cell data if available
        cell_avg_mv = (result["cell_max_mv"] + result["cell_min_mv"]) / 2.0
        result["pack_v_est"] = cell_avg_mv * 96 / 1000.0

        # Power
        result["power_kw"] = result["pack_v_est"] * result["current_a"] / 1000.0

    # Per-segment deltas (bytes 26-45, 10 × 16-bit values, filter 0xFFFF)
    if len(g05_data) >= 46:
        segments = []
        for i in range(10):
            off = 26 + i * 2
            v = (g05_data[off] << 8) | g05_data[off + 1]
            if v < 0xFFFF:
                segments.append(v)
        if segments:
            result["seg_deltas"] = segments

    # Cell group voltages (bytes 46-65, 10 × 16-bit values, filter 0xFFFF)
    if len(g05_data) >= 66:
        cell_groups = []
        for i in range(10):
            off = 46 + i * 2
            v = (g05_data[off] << 8) | g05_data[off + 1]
            if v < 5000:
                cell_groups.append(v)
        if cell_groups:
            result["cell_groups"] = cell_groups

    return result


def power_bar(kw, max_kw=10.0, width=30):
    """ASCII bar for power level."""
    abs_kw = abs(kw)
    filled = int(min(abs_kw / max_kw, 1.0) * width)
    if kw > 0.05:
        # Discharge
        bar = "#" * filled + "·" * (width - filled)
        return f"[{bar}] DRAW"
    elif kw < -0.05:
        # Charge/regen
        bar = "=" * filled + "·" * (width - filled)
        return f"[{bar}] REGEN"
    else:
        return f"[{'·' * width}] IDLE"


def main():
    ap = argparse.ArgumentParser(description="Leaf Power Monitor")
    ap.add_argument("--interval", type=float, default=3.0, help="Poll interval (default: 3s)")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    args = ap.parse_args()

    print("Power Monitor — 2012 Nissan Leaf")
    print(f"Port: {args.port} @ {args.baud}")
    print("=" * 65)

    elm = SerialELM(args.port, args.baud)

    # Configure for LBC
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
    print("Ready. Polling...\n")

    reading = 0
    prev_soc = None

    try:
        while True:
            reading += 1
            ts = time.strftime("%H:%M:%S")

            # Read group 01 (SOC, capacity)
            r01 = elm.send("2101", wait=0.3, timeout=10.0)
            g01 = parse_isotp(r01) if r01 and "NO DATA" not in " ".join(r01) else bytearray()

            # Read group 05 (current, cell voltages, power)
            r05 = elm.send("2105", wait=0.3, timeout=10.0)
            g05 = parse_isotp(r05) if r05 and "NO DATA" not in " ".join(r05) else bytearray()

            d = decode_power(g01, g05)

            if not d.get("current_raw") and not d.get("soc"):
                print(f"  [{reading:3d}] {ts}  No data from LBC")
                time.sleep(args.interval)
                continue

            soc = d.get("soc", 0)
            current = d.get("current_a", 0)
            power = d.get("power_kw", 0)
            pack_v = d.get("pack_v_est", 0)
            cell_min = d.get("cell_min_mv", 0)
            cell_max = d.get("cell_max_mv", 0)
            cell_spread = cell_max - cell_min
            discharging = d.get("discharging", False)

            # SOC trend
            soc_trend = ""
            if prev_soc is not None:
                delta = soc - prev_soc
                if delta > 0.01:
                    soc_trend = f" (+{delta:.2f})"
                elif delta < -0.01:
                    soc_trend = f" ({delta:.2f})"
            prev_soc = soc

            # Display
            print(f"  [{reading:3d}] {ts}")
            print(f"  ┌─────────────────────────────────────────────────────────────┐")
            print(f"  │  SOC:     {soc:6.2f} %{soc_trend:<12s}  Capacity: {d.get('capacity_ah', 0):5.2f} Ah     │")
            print(f"  │  Pack V:  {pack_v:6.1f} V  (cells {cell_min}-{cell_max} mV, spread {cell_spread} mV)  │")
            print(f"  │                                                             │")
            print(f"  │  Current: {current:+7.3f} A  {'(discharge)' if discharging else '(idle/charge)':12s}              │")
            print(f"  │  Power:   {power:+7.3f} kW                                        │")
            print(f"  │  {power_bar(power):57s}   │")
            print(f"  │                                                             │")

            # Cell group voltages
            cg = d.get("cell_groups", [])
            if cg:
                cg_min = min(cg)
                cg_max = max(cg)
                cg_spread = cg_max - cg_min
                print(f"  │  Cell groups (10 segments, mV):                            │")
                row1 = "  ".join(f"{v:4d}" for v in cg[:5])
                row2 = "  ".join(f"{v:4d}" for v in cg[5:])
                print(f"  │    {row1}                        │")
                print(f"  │    {row2}    spread: {cg_spread:2d} mV       │")

            # Segment deltas (load distribution)
            sd = d.get("seg_deltas", [])
            if sd:
                sd_min = min(sd)
                sd_max = max(sd)
                sd_spread = sd_max - sd_min
                print(f"  │  Segment deltas: min={sd_min} max={sd_max} spread={sd_spread:<18d} │")

            print(f"  └─────────────────────────────────────────────────────────────┘")
            print()

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        elm.close()
        print("Port closed.")


if __name__ == "__main__":
    main()
