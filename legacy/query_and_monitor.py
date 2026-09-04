#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Phase 2: Query-then-Monitor approach for 2012 Nissan Leaf.

Key fix: Uses a SINGLE persistent notification handler (no start/stop cycling).

Strategy:
  1. Configure adapter (ATZ, ATE0, ATH1, ATSP6)
  2. Send minimal read-only OBD query (0100 = supported PIDs)
  3. Try monitoring specific known Leaf CAN IDs
  4. Finally try unfiltered ATMA

All operations are READ-ONLY.

Usage:
  source venv/bin/activate
  python query_and_monitor.py <BLE_ADDRESS> [--log query.log]
"""

import argparse
import asyncio
import datetime as dt
from typing import Optional

from bleak import BleakClient


FFE1_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


def ts() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def log(msg: str, fh=None):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


class ELMSession:
    """Manages a persistent BLE notification stream to an ELM327 adapter."""

    def __init__(self, client: BleakClient, log_fh=None):
        self.client = client
        self.log_fh = log_fh
        self.buf = ""
        self.lines = []  # completed lines
        self.prompt_event = asyncio.Event()  # set when '>' seen
        self.notify_count = 0

    def _on_notify(self, _sender, data: bytearray):
        self.notify_count += 1
        text = bytes(data).decode("ascii", errors="replace")
        log(f"  RX [{self.notify_count:>4}] hex={data.hex()} ascii={repr(text)}", self.log_fh)

        self.buf += text

        # Extract complete lines (split on \r or \n)
        while "\r" in self.buf or "\n" in self.buf:
            # Find earliest line terminator
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
                self.lines.append(line)
                if line == ">":
                    self.prompt_event.set()

        # Also check for '>' in remaining buffer (some adapters send it without newline)
        if ">" in self.buf:
            # Split on '>'
            parts = self.buf.split(">")
            for p in parts[:-1]:
                p = p.strip()
                if p:
                    self.lines.append(p)
            self.lines.append(">")
            self.buf = parts[-1]
            self.prompt_event.set()

    async def start(self):
        await self.client.start_notify(FFE1_UUID, self._on_notify)
        log("Notifications started on FFE1", self.log_fh)
        await asyncio.sleep(0.2)

    async def stop(self):
        try:
            await self.client.stop_notify(FFE1_UUID)
        except Exception:
            pass

    async def send_cmd(self, cmd: str, timeout: float = 5.0) -> list:
        """Send command and wait for '>' prompt. Returns response lines."""
        # Clear state
        self.lines.clear()
        self.prompt_event.clear()

        payload = (cmd + "\r").encode("ascii")
        log(f"TX >>> {cmd}", self.log_fh)
        # IMPORTANT: This adapter requires write-with-response to trigger notifications
        await self.client.write_gatt_char(FFE1_UUID, payload, response=True)

        try:
            await asyncio.wait_for(self.prompt_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log(f"  (timeout {timeout}s — no '>' prompt)", self.log_fh)

        # Filter out echo of command and prompt
        result = [ln for ln in self.lines if ln not in (">", cmd)]
        return result

    async def send_and_stream(self, cmd: str, duration: float) -> list:
        """Send command (like ATMA) and collect lines for `duration` seconds."""
        self.lines.clear()
        self.prompt_event.clear()

        payload = (cmd + "\r").encode("ascii")
        log(f"TX >>> {cmd}  (streaming for {duration}s)", self.log_fh)
        # IMPORTANT: This adapter requires write-with-response to trigger notifications
        await self.client.write_gatt_char(FFE1_UUID, payload, response=True)

        await asyncio.sleep(duration)

        # Soft stop
        try:
            await self.client.write_gatt_char(FFE1_UUID, b"\r", response=True)
            await asyncio.sleep(0.5)
        except Exception:
            pass

        result = [ln for ln in self.lines if ln not in (">", "OK", "STOPPED")]
        return result


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address", help="BLE address")
    ap.add_argument("--log", type=str, default="", help="Log file path")
    args = ap.parse_args()

    log_fh = None
    if args.log:
        log_fh = open(args.log, "w", encoding="utf-8")

    log("=" * 60, log_fh)
    log("Nissan Leaf 2012 — Read-Only Query & Monitor", log_fh)
    log("=" * 60, log_fh)

    async with BleakClient(args.address) as client:
        log(f"Connected to {args.address}, MTU={client.mtu_size}", log_fh)

        elm = ELMSession(client, log_fh)
        await elm.start()

        # ─── Phase A: Configure adapter ───
        log("", log_fh)
        log("─── Phase A: Configure Adapter ───", log_fh)

        cmds = [
            ("ATZ",   "Reset adapter",                         1.5),
            ("ATE0",  "Echo off",                              0.3),
            ("ATL1",  "Linefeeds on",                          0.3),
            ("ATS1",  "Spaces on",                             0.3),
            ("ATH1",  "Headers on",                            0.3),
            ("ATAL",  "Allow long messages",                   0.3),
            ("ATSP6", "Protocol: ISO 15765-4 CAN 11-bit/500k", 0.5),
        ]

        for cmd, desc, wait in cmds:
            resp = await elm.send_cmd(cmd, timeout=5.0)
            log(f"  => {resp}  ({desc})", log_fh)
            await asyncio.sleep(wait)

        # ─── Phase B: Standard OBD query ───
        log("", log_fh)
        log("─── Phase B: Read-Only OBD Query (0100 = Supported PIDs) ───", log_fh)
        log("  NOTE: Leaf may return NO DATA — that's expected for standard PIDs.", log_fh)

        resp = await elm.send_cmd("0100", timeout=10.0)
        log(f"  0100 Response: {resp}", log_fh)

        # ─── Phase C: Try known Leaf CAN IDs ───
        log("", log_fh)
        log("─── Phase C: Monitor Known Leaf CAN IDs ───", log_fh)

        known_ids = [
            ("358", "Turn signal / connectivity test"),
            ("5B3", "SOH / GIDs"),
            ("5BC", "Battery status"),
            ("1DB", "HV battery voltage/current"),
        ]

        for can_id, desc in known_ids:
            log(f"", log_fh)
            log(f"  Trying CAN ID 0x{can_id} ({desc})...", log_fh)

            resp = await elm.send_cmd(f"ATCRA {can_id}", timeout=3.0)
            log(f"  ATCRA response: {resp}", log_fh)

            frames = await elm.send_and_stream("ATMA", duration=3.0)
            log(f"  Result for 0x{can_id}: {len(frames)} frames", log_fh)
            for f in frames[:10]:
                log(f"    FRAME: {f}", log_fh)
            if len(frames) > 10:
                log(f"    ... and {len(frames) - 10} more", log_fh)

            # Clear filter
            await elm.send_cmd("ATAR", timeout=3.0)

        # ─── Phase D: Unfiltered ATMA ───
        log("", log_fh)
        log("─── Phase D: Unfiltered Monitor (ATMA, 10s) ───", log_fh)

        await elm.send_cmd("ATAR", timeout=3.0)
        frames = await elm.send_and_stream("ATMA", duration=10.0)
        log(f"  Total unfiltered frames: {len(frames)}", log_fh)
        for f in frames[:50]:
            log(f"  CAN: {f}", log_fh)

        # ─── Summary ───
        log("", log_fh)
        log("=" * 60, log_fh)
        log("SUMMARY", log_fh)
        log("=" * 60, log_fh)
        log(f"  Total BLE notifications received: {elm.notify_count}", log_fh)

        if elm.notify_count == 0:
            log("  !! Zero notifications — BLE notify subscription may have failed", log_fh)
        elif len(frames) == 0:
            log("  Adapter responded to AT commands but no CAN frames seen.", log_fh)
            log("  Possible causes:", log_fh)
            log("    - Car is OFF (must be IGN-ON or READY)", log_fh)
            log("    - Clone ELM327 firmware may not support ATMA", log_fh)
            log("    - Leaf diagnostic bus quiet without active queries", log_fh)

        await elm.stop()

    log("", log_fh)
    log("Done. Disconnected.", log_fh)

    if log_fh:
        log_fh.close()


if __name__ == "__main__":
    asyncio.run(main())
