#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Battery Logger — 2012 Nissan Leaf
Logs battery state, temperatures, and cell voltages to JSONL every 30 seconds.

Output: battery_log_YYYYMMDD_HHMMSS.jsonl in the script directory.

Usage:
  source venv/bin/activate
  python BatteryLogger.py
  python BatteryLogger.py --interval 60    # custom interval
  python BatteryLogger.py --output my.jsonl # custom output file
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

from bleak import BleakClient

ADDR = "<your-adapter-address — see config.local.json>"
FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"

NUM_CELLS = 96
NOMINAL_CAPACITY_AH = 66.0
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class ELM:
    def __init__(self, client):
        self.client = client
        self.buf = ""
        self.cmd_event = asyncio.Event()
        self.raw_response = ""

    def on_notify(self, _sender, data: bytearray):
        text = bytes(data).decode("ascii", errors="replace")
        self.buf += text
        self.raw_response += text
        if ">" in self.buf:
            self.cmd_event.set()
            self.buf = ""

    async def send(self, cmd, wait=0.3, timeout=8.0):
        self.buf = ""
        self.raw_response = ""
        self.cmd_event.clear()
        await self.client.write_gatt_char(FFE1, (cmd + "\r").encode(), response=True)
        try:
            await asyncio.wait_for(self.cmd_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(wait)
        resp = self.raw_response.replace(">", "").strip()
        if resp.startswith(cmd):
            resp = resp[len(cmd):].strip()
        return [l.strip() for l in resp.replace("\r", "\n").split("\n") if l.strip() and l.strip() != "OK"]


def parse_isotp(lines):
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
    if len(data) < 39:
        return None
    soc_raw = (data[29] << 16) | (data[30] << 8) | data[31]
    cap_raw = (data[33] << 16) | (data[34] << 8) | data[35]
    hx_raw = (data[26] << 8) | data[27]
    lv_raw = (data[20] << 8) | data[21]
    insulation_raw = (data[22] << 8) | data[23]
    capacity_ah = cap_raw / 10000.0
    return {
        "soc_pct": round(soc_raw / 10000.0, 2),
        "capacity_ah": round(capacity_ah, 4),
        "soh_pct": round((capacity_ah / NOMINAL_CAPACITY_AH) * 100.0, 1),
        "hx_pct": round(hx_raw / 100.0, 2),
        "lv_volts": round(lv_raw / 1024.0, 3),
        "insulation_kohm": insulation_raw,
    }


def decode_group04(data):
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


async def main():
    ap = argparse.ArgumentParser(description="Log Leaf battery data to JSONL")
    ap.add_argument("--interval", type=int, default=30, help="Seconds between readings (default: 30)")
    ap.add_argument("--output", type=str, default="", help="Output JSONL file path")
    args = ap.parse_args()

    if args.output:
        log_path = args.output
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(SCRIPT_DIR, f"battery_log_{stamp}.jsonl")

    print(f"Battery Logger — 2012 Nissan Leaf")
    print(f"Logging every {args.interval}s to: {log_path}")
    print(f"Press Ctrl+C to stop.\n")

    async with BleakClient(ADDR) as client:
        print(f"Connected (MTU={client.mtu_size})")

        elm = ELM(client)
        await client.start_notify(FFE1, elm.on_notify)
        await asyncio.sleep(0.2)

        await elm.send("ATZ", 1.5)
        await elm.send("ATE0")
        await elm.send("ATL1")
        await elm.send("ATH1")
        await elm.send("ATS1")
        await elm.send("ATSP6")
        await elm.send("ATSH 79B")
        await elm.send("ATCRA 7BB")
        await elm.send("ATCAF1")
        await elm.send("ATFCSH 79B")
        await elm.send("ATFCSD 30 00 20")
        await elm.send("ATFCSM1")

        print("Adapter configured. Logging started.\n")

        read_count = 0

        try:
            while True:
                read_count += 1
                timestamp = dt.datetime.now().isoformat()
                record = {"timestamp": timestamp, "reading": read_count}
                errors = []

                # Group 01 — battery state
                resp01 = await elm.send("2101", wait=0.5, timeout=10.0)
                if resp01 and "NO DATA" not in " ".join(resp01):
                    state = decode_group01(parse_isotp(resp01))
                    if state:
                        record.update(state)
                    else:
                        errors.append("group01_decode_failed")
                else:
                    errors.append("group01_no_data")

                # Group 04 — temperatures
                resp04 = await elm.send("2104", wait=0.5, timeout=10.0)
                if resp04 and "NO DATA" not in " ".join(resp04):
                    temps = decode_group04(parse_isotp(resp04))
                    if temps:
                        record["temps_degc"] = [t["deg_c"] for t in temps]
                        record["temps_raw"] = [t["raw"] for t in temps]
                    else:
                        errors.append("group04_decode_failed")
                else:
                    errors.append("group04_no_data")

                # Group 02 — cell voltages
                resp02 = await elm.send("2102", wait=0.5, timeout=15.0)
                if resp02 and "NO DATA" not in " ".join(resp02):
                    voltages = decode_cell_voltages(parse_isotp(resp02))
                    if len(voltages) == NUM_CELLS:
                        record["cell_mv"] = voltages
                        record["cell_min_mv"] = min(voltages)
                        record["cell_max_mv"] = max(voltages)
                        record["cell_spread_mv"] = max(voltages) - min(voltages)
                        record["pack_sum_v"] = round(sum(voltages) / 1000.0, 1)
                    else:
                        record["cell_mv"] = voltages
                        errors.append(f"incomplete_cells_{len(voltages)}")
                else:
                    errors.append("group02_no_data")

                if errors:
                    record["errors"] = errors

                # Write JSONL
                with open(log_path, "a") as f:
                    f.write(json.dumps(record) + "\n")

                # Console summary
                soc = record.get("soc_pct", "?")
                cap = record.get("capacity_ah", "?")
                spread = record.get("cell_spread_mv", "?")
                temps_str = ", ".join(f"{t}°C" for t in record.get("temps_degc", []))
                err_str = f"  errors: {errors}" if errors else ""

                print(f"  [{read_count:4d}] {timestamp}  SOC={soc}%  Cap={cap}Ah  Spread={spread}mV  T=[{temps_str}]{err_str}")

                await asyncio.sleep(args.interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

        await client.stop_notify(FFE1)

    print(f"\n\nStopped. {read_count} readings saved to {log_path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
