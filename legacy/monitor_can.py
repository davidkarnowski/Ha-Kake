#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
# Read-only CAN observation via ELM327 monitor mode over BLE (LELink/OBDBLE style)
# macOS + bleak
#
# Usage:
#   source venv/bin/activate
#   python monitor_can.py <BLE_ADDRESS> [--duration 30] [--log can.log]

import argparse
import asyncio
import datetime as dt
import sys
from typing import Optional

from bleak import BleakClient


FFE1_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


def utc_ts() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def log(msg: str, fh=None):
    """Print to stdout with timestamp and optionally write to log file."""
    line = f"[{utc_ts()}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


class LineAssembler:
    """Assemble ASCII lines from BLE notification chunks."""

    def __init__(self) -> None:
        self.buf = ""

    def push(self, text: str):
        self.buf += text
        self.buf = self.buf.replace(">", "\n>\n")
        parts = self.buf.splitlines(keepends=False)

        if self.buf.endswith("\n") or self.buf.endswith("\r"):
            self.buf = ""
            lines = parts
        else:
            if len(parts) > 0:
                self.buf = parts[-1]
                lines = parts[:-1]
            else:
                lines = []

        out = []
        for ln in lines:
            for sub in ln.split("\r"):
                s = sub.strip()
                if s:
                    out.append(s)
        return out


async def write_cmd(client: BleakClient, cmd: str, settle: float = 0.3, log_fh=None) -> None:
    """Send an AT command and log it."""
    payload = (cmd + "\r").encode("ascii", errors="strict")
    log(f"TX >>> {cmd}", log_fh)
    await client.write_gatt_char(FFE1_UUID, payload, response=False)
    await asyncio.sleep(settle)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("address", help="BLE address/UUID")
    ap.add_argument("--duration", type=float, default=30.0, help="Seconds to monitor (default: 30)")
    ap.add_argument("--log", type=str, default="", help="Optional log file path")
    args = ap.parse_args()

    log_fh: Optional[object] = None
    if args.log:
        log_fh = open(args.log, "a", encoding="utf-8")

    log(f"=== CAN Monitor Start ===", log_fh)
    log(f"Target: {args.address}", log_fh)
    log(f"Duration: {args.duration}s", log_fh)

    assembler = LineAssembler()
    frame_count = 0

    def handle_line(line: str) -> None:
        nonlocal frame_count
        frame_count += 1
        log(f"CAN [{frame_count:>5}] {line}", log_fh)

    log(f"Connecting to BLE device...", log_fh)

    async with BleakClient(args.address) as client:
        if not client.is_connected:
            raise RuntimeError("Failed to connect to BLE device.")

        log(f"Connected! MTU={client.mtu_size}", log_fh)

        # Raw notification handler with verbose output
        raw_notify_count = 0

        def on_notify(_sender: int, data: bytearray) -> None:
            nonlocal raw_notify_count
            raw_notify_count += 1
            try:
                text = bytes(data).decode("ascii", errors="replace")
            except Exception:
                log(f"RX [{raw_notify_count}] (decode error) hex={data.hex()}", log_fh)
                return

            log(f"RX [{raw_notify_count:>4}] hex={data.hex()} ascii={repr(text)}", log_fh)

            for ln in assembler.push(text):
                if ln in ("OK", ">"):
                    log(f"  (filtered: {repr(ln)})", log_fh)
                    continue
                handle_line(ln)

        await client.start_notify(FFE1_UUID, on_notify)
        log("Notifications enabled on FFE1", log_fh)
        await asyncio.sleep(0.3)

        # === Configure adapter ===
        log("--- Configuring adapter ---", log_fh)

        await write_cmd(client, "ATZ", settle=1.0, log_fh=log_fh)   # Reset
        await write_cmd(client, "ATE0", log_fh=log_fh)               # Echo off
        await write_cmd(client, "ATL0", log_fh=log_fh)               # Linefeeds off
        await write_cmd(client, "ATS1", log_fh=log_fh)               # Spaces on (easier to read)
        await write_cmd(client, "ATH1", log_fh=log_fh)               # Headers on
        await write_cmd(client, "ATSP0", settle=0.5, log_fh=log_fh)  # Auto-detect protocol

        log("--- Starting monitor (ATMA) ---", log_fh)
        await write_cmd(client, "ATMA", settle=0.05, log_fh=log_fh)

        # Run for duration
        log(f"Monitoring for {args.duration}s... (Ctrl+C to stop early)", log_fh)
        try:
            await asyncio.sleep(args.duration)
        except asyncio.CancelledError:
            pass
        finally:
            log(f"--- Stopping monitor ---", log_fh)
            log(f"Total raw BLE notifications: {raw_notify_count}", log_fh)
            log(f"Total CAN frames parsed: {frame_count}", log_fh)

            # Soft stop
            try:
                await client.write_gatt_char(FFE1_UUID, b"\r", response=False)
                await asyncio.sleep(0.2)
            except Exception:
                pass
            try:
                await client.stop_notify(FFE1_UUID)
            except Exception:
                pass

    log(f"=== CAN Monitor End ===", log_fh)

    if log_fh:
        log_fh.close()


if __name__ == "__main__":
    asyncio.run(main())
