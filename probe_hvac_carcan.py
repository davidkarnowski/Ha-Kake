#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Probe: HVAC amp UDS groups, VCM extra UDS, and Car-CAN passive IDs with ATCAF0.

Phase A — passive Car-CAN sweep (ATCAF0, ATCRA per ID, ~2 s each)
Phase B — HVAC amp (0x744 → 0x764): 21 01..21 20, 21 81
Phase C — VCM (0x797 → 0x79A): 21 81 (VIN), 22 1201..1205, 21 01

Raw results saved to tests/fixtures/probe_<timestamp>.json.
Usage: ./venv/bin/python probe_hvac_carcan.py [--adapter ble|usb] [--phase A|B|C|all]
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elm327 import detect_adapter          # noqa: E402
from leaf_decoders import parse_isotp, is_no_data  # noqa: E402

PASSIVE_IDS = [
    ("421", "gear (b0 bits 3-5)"),
    ("5C5", "odometer b1-3, handbrake, start button"),
    ("385", "TPMS PSI = b2-5 / 4"),
    ("5B3", "SOH % b1 bits 1-7"),
    ("284", "speed b4-5"),
    ("5A9", "range b1-2"),
    ("5B9", "charge bars"),
    ("60D", "doors / lights"),
    ("355", "units flag b6 bit5"),
    ("180", "throttle b5 /2"),
    ("292", "brake b6"),
    ("174", "gear (known) b3"),
]


async def base_config(elm):
    await elm.send("ATZ", wait=1.5)
    await elm.send("ATE0")
    await elm.send("ATL1")
    await elm.send("ATH1")
    await elm.send("ATS1")
    await elm.send("ATSP6")


async def passive_capture(elm, can_id, seconds=2.0):
    await elm.send("ATCAF0")
    await elm.send(f"ATCRA {can_id}")
    lines = await elm.send("ATMA", wait=0.0, timeout=seconds)
    # interrupt monitor mode
    await elm.send("", wait=0.3, timeout=2.0)
    frames = [l for l in lines if l.upper().startswith(can_id.upper())]
    return frames


async def uds_setup(elm, tx, rx):
    await elm.send("ATCAF1")
    await elm.send(f"ATSH {tx}")
    await elm.send(f"ATCRA {rx}")
    await elm.send(f"ATFCSH {tx}")
    await elm.send("ATFCSD 30 00 20")
    await elm.send("ATFCSM1")
    await elm.send("ATAT1")


def summarize_uds(cmd, lines, rx):
    if is_no_data(lines):
        return f"  {cmd}: {' '.join(lines)[:40] or 'no data'}"
    d = parse_isotp(lines, rx)
    neg = any(" 7F " in (" " + l + " ") for l in lines)
    hexs = " ".join(f"{b:02X}" for b in d)
    return f"  {cmd}: {len(lines)} frames, {len(d)} B{' (NEG RESP)' if neg else ''}: {hexs[:96]}{'…' if len(hexs) > 96 else ''}"


async def main(args):
    out = {"captured": dt.datetime.now().isoformat(), "passive": {}, "hvac": {}, "vcm": {}}
    elm = await detect_adapter(prefer=args.adapter)
    await base_config(elm)
    try:
        if args.phase in ("A", "all"):
            print("\n── Phase A: passive Car-CAN (ATCAF0) ──")
            for cid, desc in PASSIVE_IDS:
                frames = await passive_capture(elm, cid, seconds=2.0)
                out["passive"][cid] = frames
                uniq = sorted(set(frames))
                print(f"  {cid} [{desc}]: {len(frames)} frames, {len(uniq)} unique")
                for f in uniq[:4]:
                    print(f"      {f}")
            await elm.send("ATCRA")  # clear filter

        if args.phase in ("B", "all"):
            print("\n── Phase B: HVAC amp 0x744 → 0x764 ──")
            await uds_setup(elm, "744", "764")
            cmds = [f"21{g:02X}" for g in range(1, 0x21)] + ["2181", "2201", "220101"]
            for cmd in cmds:
                lines = await elm.send(cmd, wait=0.3, timeout=4.0)
                out["hvac"][cmd] = lines
                print(summarize_uds(cmd, lines, "764"))

        if args.phase in ("C", "all"):
            print("\n── Phase C: VCM 0x797 → 0x79A ──")
            await uds_setup(elm, "797", "79A")
            for cmd in ["2181", "221201", "221202", "221203", "221204", "221205", "2101", "1A81", "0902"]:
                lines = await elm.send(cmd, wait=0.3, timeout=4.0)
                out["vcm"][cmd] = lines
                print(summarize_uds(cmd, lines, "79A"))
    finally:
        await elm.close()

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures",
                        f"probe_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nsaved {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", choices=["auto", "usb", "ble"], default="auto")
    ap.add_argument("--phase", choices=["A", "B", "C", "all"], default="all")
    a = ap.parse_args()
    a.adapter = None if a.adapter == "auto" else a.adapter
    asyncio.run(main(a))
