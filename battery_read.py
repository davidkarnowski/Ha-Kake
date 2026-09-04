#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Battery Reader (console) — 2012 Nissan Leaf
Unified replacement for battery_cell_read.py / usb_battery_read.py / BatteryLogger.py.

Works over BLE or USB via elm327.py, decodes with leaf_decoders.py, prints
temperatures as °C / °F, and optionally appends every reading to the SQLite
store shared with the web dashboard.

Usage:
  ./venv/bin/python battery_read.py                 # one full read, auto-detect adapter
  ./venv/bin/python battery_read.py --adapter ble   # force BLE
  ./venv/bin/python battery_read.py --loop 30       # repeat every 30 s
  ./venv/bin/python battery_read.py --cells         # include the 96-cell table
  ./venv/bin/python battery_read.py --store         # also write to web/leaf_battery.db
  ./venv/bin/python battery_read.py --raw           # dump raw ELM327 lines too
"""

import argparse
import asyncio
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from elm327 import detect_adapter, configure_leaf_bms   # noqa: E402
from leaf_decoders import decode_reading, fmt_temp, NOMINAL_CAPACITY_AH  # noqa: E402

GROUPS = ["2101", "2105", "2104", "2102", "2106"]


def print_reading(rec, show_cells=False):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 58}\n  {now}\n{'=' * 58}")

    if "soc" in rec:
        print("\n  Battery State")
        print(f"  {'─' * 44}")
        print(f"  SOC:          {rec['soc']:7.2f} %")
        print(f"  Capacity:     {rec['capacity_ah']:7.2f} Ah  (nominal {NOMINAL_CAPACITY_AH:.0f} Ah)")
        print(f"  SOH:          {rec['soh']:7.1f} %")
        print(f"  HX:           {rec['hx']:7.2f}")
        print(f"  Pack:         {rec['pack_v']:7.2f} V")
        print(f"  HV current:   {rec['hv_current1_a']:+7.2f} A / {rec['hv_current2_a']:+7.2f} A  (sensor 1 / 2)")
        print(f"  12V battery:  {rec['lv_volts']:7.2f} V")
        print(f"  Insulation:   {rec['insulation_kohm']:7d} kΩ")

    if "power_kw" in rec:
        state = "DRAW" if rec.get("discharging") else "CHARGE/REGEN"
        print(f"\n  Power:        {abs(rec['power_kw']):7.3f} kW  {state}  ({rec['current_a']:+.2f} A)")

    if "temps_c" in rec:
        print("\n  Pack Temperatures")
        print(f"  {'─' * 44}")
        for i, c in enumerate(rec["temps_c"]):
            print(f"  Sensor {i + 1}:     {fmt_temp(c)}")
        print(f"  Average:      {fmt_temp(rec['temp_avg_c'])}")

    if "cells" in rec:
        cells = rec["cells"]
        print("\n  Cell Pairs")
        print(f"  {'─' * 44}")
        print(f"  Count:        {rec['cell_count']}")
        print(f"  Min:          {rec['cell_min']} mV  (pair #{rec['cell_min_idx']})")
        print(f"  Max:          {rec['cell_max']} mV  (pair #{rec['cell_max_idx']})")
        print(f"  Avg:          {rec['cell_avg']} mV")
        print(f"  Spread:       {rec['cell_spread']} mV")
        print(f"  Sum:          {rec['pack_v_cells']} V")
        if rec.get("balancing_active") is not None:
            print(f"  Balancing:    {rec['balancing_active']} pairs flagged (group 06, tentative)")
        if show_cells:
            mn, mx = rec["cell_min"], rec["cell_max"]
            print(f"\n  {'Pair':<6}{'mV':>6}   bar")
            for i, mv in enumerate(cells):
                bar = "#" * min(40, max(0, (mv - mn + 2) // 2))
                tag = "  << MIN" if mv == mn else "  << MAX" if mv == mx else ""
                print(f"  {i:<6}{mv:>6}   {bar}{tag}")


async def main(args):
    print("Battery Reader — 2012 Nissan Leaf")
    print("Detecting adapter…")
    elm = await detect_adapter(prefer=args.adapter)
    await configure_leaf_bms(elm)
    store = None
    if args.store:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
        from store import Store
        store = Store()
        print(f"Storing to {store.path} ({store.count()} readings so far)")

    try:
        while True:
            responses = {}
            for cmd in GROUPS:
                responses[cmd] = await elm.send(cmd, wait=0.4, timeout=15.0 if cmd == "2102" else 10.0)
                if args.raw:
                    print(f"\n[{cmd}] {len(responses[cmd])} lines")
                    for line in responses[cmd]:
                        print("   ", line)
            rec = decode_reading(responses)
            if not rec:
                print("  No data from BMS — is the car ON?")
            else:
                print_reading(rec, show_cells=args.cells)
                if store:
                    store.insert_reading(rec, ts=dt.datetime.now(dt.timezone.utc), adapter=elm.adapter_type)
            if not args.loop:
                break
            await asyncio.sleep(args.loop)
    finally:
        await elm.close()
        if store:
            store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Console battery reader — 2012 Nissan Leaf")
    ap.add_argument("--adapter", choices=["auto", "usb", "ble"], default="auto")
    ap.add_argument("--loop", type=float, metavar="SECONDS", help="repeat every N seconds")
    ap.add_argument("--cells", action="store_true", help="print the 96-cell table")
    ap.add_argument("--store", action="store_true", help="append readings to web/leaf_battery.db")
    ap.add_argument("--raw", action="store_true", help="dump raw ELM327 response lines")
    a = ap.parse_args()
    a.adapter = None if a.adapter == "auto" else a.adapter
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        print("\nStopped.")
