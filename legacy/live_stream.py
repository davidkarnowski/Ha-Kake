#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Live CAN frame stream viewer for 2012 Nissan Leaf.
Filters on specific CAN IDs and displays a real-time updating terminal view.

Usage:
  source venv/bin/activate
  python live_stream.py <BLE_ADDRESS> [--ids 358] [--duration 60]

Examples:
  python live_stream.py <adapter-address> --ids 358            # Turn signals only
  python live_stream.py <adapter-address> --ids 358,354,1D5    # Multiple IDs
  python live_stream.py <adapter-address> --all                 # All traffic (may overflow)
"""

import argparse
import asyncio
import datetime as dt
import os
import sys

from bleak import BleakClient

FFE1_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Known Leaf CAN ID descriptions
CAN_ID_LABELS = {
    "002": "Unknown",
    "130": "Counter/Status",
    "174": "Gear position (byte 3)",
    "176": "Rolling Counter",
    "180": "Steering/Chassis",
    "1D5": "Torque/Motor",
    "1DB": "HV Batt V/I (EV-CAN)",
    "1F9": "Idle Frame",
    "245": "Unknown",
    "260": "Unknown",
    "284": "Speed/Odo",
    "285": "Speed/Odo",
    "292": "Unknown",
    "300": "Status",
    "354": "Gear/Shift",
    "358": "Turn Signals/Body",
    "5B3": "SOH/GIDs (EV-CAN)",
    "5BC": "Battery Status (EV-CAN)",
    "6F6": "Unknown",
}


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class LiveViewer:
    def __init__(self, client, filter_ids=None, log_fh=None):
        self.client = client
        self.filter_ids = filter_ids  # None = all
        self.log_fh = log_fh
        self.buf = ""
        self.frame_count = 0
        self.notify_count = 0
        self.id_counts = {}
        self.id_last_data = {}
        self.id_last_time = {}
        self.errors = 0
        self.started = False
        self._cmd_mode = False
        self._cmd_event = None
        self._cmd_response = None

    def _on_notify(self, _sender, data: bytearray):
        self.notify_count += 1
        try:
            text = bytes(data).decode("ascii", errors="replace")
        except Exception:
            return

        # In command mode, capture raw responses
        if self._cmd_mode:
            self._cmd_response.append(text)
            if ">" in text:
                self._cmd_event.set()
            return

        self.buf += text

        # Extract lines on \r or \n
        while "\r" in self.buf or "\n" in self.buf:
            idx_r = self.buf.find("\r")
            idx_n = self.buf.find("\n")
            if idx_r == -1:
                idx = idx_n
            elif idx_n == -1:
                idx = idx_r
            else:
                idx = min(idx_r, idx_n)

            line = self.buf[:idx].strip()
            self.buf = self.buf[idx + 1:]
            if line:
                self._handle_line(line)

        # Handle '>' without newline
        if ">" in self.buf:
            parts = self.buf.split(">")
            for p in parts[:-1]:
                p = p.strip()
                if p:
                    self._handle_line(p)
            self.buf = parts[-1]

    def _handle_line(self, line):
        # Skip noise
        if line in ("OK", ">", "SEARCHING...", "STOPPED", "NO DATA"):
            return
        if line == "BUFFER FULL":
            self.errors += 1
            return
        if "<DATA ERROR" in line:
            # Still parse the frame portion
            line = line.split("<")[0].strip()
            self.errors += 1
            if not line:
                return

        # Parse: first token is CAN ID, rest is data
        parts = line.split()
        if not parts:
            return

        can_id = parts[0].upper()
        data_bytes = parts[1:] if len(parts) > 1 else []

        self.frame_count += 1
        self.id_counts[can_id] = self.id_counts.get(can_id, 0) + 1
        self.id_last_data[can_id] = " ".join(data_bytes)
        self.id_last_time[can_id] = ts()

        # Log to file
        if self.log_fh:
            self.log_fh.write(f"{ts()} {can_id} {' '.join(data_bytes)}\n")
            if self.frame_count % 20 == 0:
                self.log_fh.flush()

        # Print the live frame
        label = CAN_ID_LABELS.get(can_id, "")
        data_str = " ".join(data_bytes)

        # Highlight changes with color
        print(f"\033[92m{ts()}\033[0m "
              f"\033[96m{can_id:>4}\033[0m "
              f"\033[93m{data_str:<30}\033[0m "
              f"\033[90m{label}\033[0m",
              flush=True)

    async def send_cmd(self, cmd, timeout=5.0):
        """Send AT command using persistent notification handler."""
        event = asyncio.Event()
        cmd_response = []

        # Temporarily capture responses
        self._cmd_event = event
        self._cmd_response = cmd_response
        self._cmd_mode = True

        payload = (cmd + "\r").encode("ascii")
        await self.client.write_gatt_char(FFE1_UUID, payload, response=True)

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

        self._cmd_mode = False
        return "".join(cmd_response).replace(">", "").replace("\r", " ").replace("\n", " ").strip()

    async def run(self, duration):
        print("\033[2J\033[H", end="")  # Clear screen
        print("=" * 70)
        print("  Nissan Leaf 2012 — Live CAN Stream")
        print("=" * 70)

        # Start persistent notification handler FIRST
        await self.client.start_notify(FFE1_UUID, self._on_notify)
        await asyncio.sleep(0.2)

        # Configure adapter
        print("\n\033[90mConfiguring adapter...\033[0m", flush=True)

        cmds = [
            ("ATZ", 1.0),
            ("ATE0", 0.2),
            ("ATL1", 0.2),
            ("ATS1", 0.2),
            ("ATH1", 0.2),
            ("ATSP6", 0.3),
        ]
        for cmd, wait in cmds:
            resp = await self.send_cmd(cmd)
            print(f"  {cmd:<8} => {resp}", flush=True)
            await asyncio.sleep(wait)

        # Set CAN receive filter if specific IDs requested
        if self.filter_ids and len(self.filter_ids) == 1:
            can_id = self.filter_ids[0]
            resp = await self.send_cmd(f"ATCRA {can_id}")
            print(f"  ATCRA {can_id} => {resp}", flush=True)
        elif self.filter_ids:
            # Multiple IDs: we'll filter in software, no hardware filter
            print(f"  Software filter: {', '.join(self.filter_ids)}", flush=True)

        print(f"\n\033[1mStreaming for {duration}s — press Ctrl+C to stop\033[0m")
        if self.filter_ids:
            labels = [f"0x{fid} ({CAN_ID_LABELS.get(fid, '?')})" for fid in self.filter_ids]
            print(f"Filtering: {', '.join(labels)}")
        print(f"\n{'TIME':<14} {'ID':>4} {'DATA':<30} {'LABEL'}")
        print("-" * 70, flush=True)

        # Send ATMA (notifications already running)
        payload = b"ATMA\r"
        await self.client.write_gatt_char(FFE1_UUID, payload, response=True)
        self.started = True

        # Run
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            pass

        # Stop
        try:
            await self.client.write_gatt_char(FFE1_UUID, b"\r", response=True)
            await asyncio.sleep(0.3)
        except Exception:
            pass

        try:
            await self.client.stop_notify(FFE1_UUID)
        except Exception:
            pass

        # Summary
        print("\n" + "=" * 70)
        print("  STREAM SUMMARY")
        print("=" * 70)
        print(f"  BLE notifications: {self.notify_count}")
        print(f"  CAN frames parsed: {self.frame_count}")
        print(f"  Errors (DATA ERROR / BUFFER FULL): {self.errors}")
        print(f"\n  Frames per CAN ID:")
        for cid in sorted(self.id_counts.keys()):
            label = CAN_ID_LABELS.get(cid, "")
            count = self.id_counts[cid]
            last = self.id_last_data.get(cid, "")
            print(f"    0x{cid:>4}: {count:>6} frames  last={last}  {label}")


async def main():
    ap = argparse.ArgumentParser(description="Live CAN stream viewer for Nissan Leaf")
    ap.add_argument("address", help="BLE address")
    ap.add_argument("--ids", type=str, default="358",
                    help="Comma-separated CAN IDs to filter (default: 358)")
    ap.add_argument("--all", action="store_true", help="Show all CAN traffic (no filter)")
    ap.add_argument("--duration", type=float, default=60.0, help="Duration in seconds (default: 60)")
    ap.add_argument("--log", type=str, default="", help="Log file path")
    args = ap.parse_args()

    filter_ids = None
    if not args.all:
        filter_ids = [fid.strip().upper() for fid in args.ids.split(",")]

    log_fh = None
    if args.log:
        log_fh = open(args.log, "a", encoding="utf-8")
        log_fh.write(f"# Stream started {dt.datetime.now().isoformat()}\n")

    async with BleakClient(args.address) as client:
        if not client.is_connected:
            print("Failed to connect!")
            return

        viewer = LiveViewer(client, filter_ids=filter_ids, log_fh=log_fh)
        await viewer.run(args.duration)

    if log_fh:
        log_fh.close()

    print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(main())
