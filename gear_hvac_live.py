#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focused live capture for calibration. Prints only transitions.

  gear  — alternate 0x421 / 0x174 passive captures (~2 Hz) for N seconds
  hvac  — poll HVAC amp groups 10/11 continuously, print byte diffs

Usage: ./venv/bin/python gear_hvac_live.py gear 120
       ./venv/bin/python gear_hvac_live.py hvac 120
"""
import asyncio
import datetime as dt
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from elm327 import detect_adapter, set_uds_target, passive_capture   # noqa: E402
from leaf_decoders import parse_isotp, is_no_data, last_complete_frame  # noqa: E402


def ts():
    return dt.datetime.now().strftime("%H:%M:%S")


async def base(elm):
    await elm.send("ATZ", wait=1.5)
    for c in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6"):
        await elm.send(c)


async def gear_mode(elm, seconds):
    last = {}
    end = asyncio.get_event_loop().time() + seconds
    print(f"{ts()} watching 0x421 / 0x174 for {seconds}s — shift now", flush=True)
    while asyncio.get_event_loop().time() < end:
        for cid, need in (("421", 1), ("174", 4)):
            lines = await passive_capture(elm, cid, 0.25)
            b = last_complete_frame(lines, need)
            if not b:
                continue
            key = b[0] if cid == "421" else b[3]
            if last.get(cid) != key:
                print(f"{ts()}  {cid}: {' '.join(f'{x:02X}' for x in b)}   <-- byte changed", flush=True)
                last[cid] = key
    print(f"{ts()} done", flush=True)


async def hvac_mode(elm, seconds):
    await set_uds_target(elm, "744", "764")
    prev = {}
    end = asyncio.get_event_loop().time() + seconds
    print(f"{ts()} polling HVAC groups 10/11 for {seconds}s — change climate settings now", flush=True)
    n = 0
    while asyncio.get_event_loop().time() < end:
        for cmd in ("2110", "2111"):
            lines = await elm.send(cmd, wait=0.1, timeout=4.0)
            if is_no_data(lines):
                continue
            d = bytes(parse_isotp(lines, "764"))
            p = prev.get(cmd)
            if p is None:
                print(f"{ts()}  {cmd} baseline: {d.hex(' ')}", flush=True)
            elif p != d:
                diffs = [f"b{i}:{p[i]:02X}->{d[i]:02X}" for i in range(min(len(p), len(d))) if p[i] != d[i]]
                print(f"{ts()}  {cmd} changed: {' '.join(diffs)}", flush=True)
            prev[cmd] = d
        n += 1
    print(f"{ts()} done ({n} polls)", flush=True)


async def main():
    mode, seconds = sys.argv[1], float(sys.argv[2])
    elm = await detect_adapter(prefer="ble")
    await base(elm)
    try:
        await (gear_mode if mode == "gear" else hvac_mode)(elm, seconds)
    finally:
        await elm.close()


asyncio.run(main())
