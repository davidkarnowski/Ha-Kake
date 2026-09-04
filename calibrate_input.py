#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Input walk — capture ECU bytes while you step a control through its settings.

Pauses the dashboard reader (web/reader.pause), connects to the adapter, then
for every step prints an instruction, waits for Enter (or --auto seconds),
captures a few samples of the target's UDS groups, and moves on. At the end
it ranks the bytes that changed with the input — constant within a step,
different between steps, and (for up-then-down walks) giving the same value
for the same setting on the way back — and saves the raw capture as JSON.

Presets (HVAC amp 0x744 → 0x764, groups 10 / 11 / 01):
  fan       fan 1 → 7 → 1   (HVAC on, A/C off preferred)
  ac        A/C off → on → off
  recirc    fresh → recirc → fresh
  mode      face → face+feet → feet → feet+defrost → defrost → face
  setpoint  60 °F → 65 → … → 85 → 60  (°F dash; adjust with --steps)
  custom    --steps "label1,label2,…"  and optionally --target lbc / hvac

Usage:
  ./venv/bin/python calibrate_input.py fan
  ./venv/bin/python calibrate_input.py fan --auto 12       # advance every 12 s, no keyboard
  ./venv/bin/python calibrate_input.py custom --steps "off,low,high,off" --samples 5
  ./venv/bin/python calibrate_input.py setpoint --steps "60,70,80,90,80,70,60"

Run it in a terminal you can type in. Ctrl-C at any time restores the dashboard.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from elm327 import detect_adapter, configure_leaf_bms, set_uds_target, passive_capture   # noqa: E402
from leaf_decoders import parse_isotp, is_no_data, last_complete_frame                    # noqa: E402

PAUSE_FILE = os.path.join(ROOT, "web", "reader.pause")
STATE_FILE = os.path.join(ROOT, "web", "battery_state.json")

TARGETS = {
    "hvac":  {"kind": "uds", "tx": "744", "rx": "764", "groups": ["2110", "2100", "2111", "2101"]},
    "lbc":   {"kind": "uds", "tx": "79B", "rx": "7BB", "groups": ["2101", "2105"]},
    # passive Car-CAN: capture these IDs each step (ATCAF0), no request
    "doors": {"kind": "passive", "ids": ["60D", "5C5", "625"]},
    "lights": {"kind": "passive", "ids": ["60D", "625", "358", "5C5"]},
    "pedals": {"kind": "passive", "ids": ["180", "292", "1D5"]},
}
for _t in TARGETS.values():
    _t.setdefault("kind", "uds")

MODES = ["upper vents", "upper + lower", "lower vents", "lower + defrost"]
PRESETS = {
    "fan": {
        "target": "hvac",
        "intro": "HVAC ON, A/C OFF, auto OFF. We walk the fan 1 → 7 and back down.",
        "steps": [("fan 1", "Set the fan to speed 1")] + [(f"fan {n}", f"Fan up to speed {n}") for n in range(2, 8)]
                 + [(f"fan {n}", f"Fan down to speed {n}") for n in range(6, 0, -1)],
    },
    "hvac": {"target": "hvac", "intro": "Fan at speed 3, A/C off, auto off. We toggle the whole HVAC system with its ON/OFF button.",
             "steps": [("on", "HVAC ON (fan 3)"), ("off", "Press the HVAC OFF button"), ("on", "HVAC ON again (fan 3)"), ("off", "OFF again"), ("on", "ON again — leave it on")]},
    "ac": {"target": "hvac", "intro": "HVAC on, fan 3, auto off. We toggle the A/C compressor button.",
           "steps": [("A/C off", "A/C OFF"), ("A/C on", "Press A/C ON — wait for the compressor to start"), ("A/C off", "A/C OFF again"), ("A/C on", "A/C ON again"), ("A/C off", "A/C OFF — leave it off")]},
    "fresh": {"target": "hvac", "intro": "HVAC on, fan 3. The FRESH AIR button.",
              "steps": [("recirc", "Start in RECIRC"), ("fresh", "Press FRESH AIR"), ("recirc", "Back to RECIRC"), ("fresh", "FRESH AIR again")]},
    "recirc": {"target": "hvac", "intro": "HVAC on, fan 3. The RECIRC button.",
               "steps": [("fresh", "Start in FRESH air"), ("recirc", "Press RECIRC"), ("fresh", "FRESH again"), ("recirc", "RECIRC again"), ("fresh", "FRESH — leave it")]},
    "auto": {"target": "hvac",
             "intro": "HVAC on. AUTO only switches ON with its button; you leave AUTO by turning the fan knob (fan may change by itself while auto — expected).",
             "steps": [("auto off", "Start with AUTO off: fan on 3, AUTO light out"),
                       ("auto on", "Press AUTO (light on) — let the fan settle"),
                       ("auto off", "Leave AUTO by turning the fan knob to 3 (AUTO light goes out)"),
                       ("auto on", "Press AUTO again"),
                       ("auto off", "Fan knob to 3 again — AUTO off, leave it there")]},
    "mode": {"target": "hvac", "intro": "HVAC on, fan 3, auto off. The MODE button cycles four positions; we go round twice.",
             "steps": [(m, f"Mode: {m.upper()}") for m in MODES] + [(m, f"Mode: {m.upper()} (second pass)") for m in MODES]},
    "setpoint": {"target": "hvac", "intro": "HVAC on, fan 3, auto off. Temperature 60 → 90 °F in 5° steps and back.",
                 "steps": [(f"{t} °F", f"Setpoint {t} °F") for t in range(60, 91, 5)] + [(f"{t} °F", f"Setpoint back to {t} °F") for t in range(85, 59, -5)]},
    "doors": {"target": "doors",
              "intro": "Start with EVERY door and the hatch shut and the car unlocked. We open and shut each one alone so a single bit moves at a time.",
              "steps": [("all shut", "All doors + hatch shut, car unlocked (baseline)"),
                        ("driver open", "Open the DRIVER door"), ("driver shut", "Shut the DRIVER door"),
                        ("pass open", "Open the FRONT PASSENGER door"), ("pass shut", "Shut the FRONT PASSENGER door"),
                        ("rl open", "Open the REAR LEFT door"), ("rl shut", "Shut the REAR LEFT door"),
                        ("rr open", "Open the REAR RIGHT door"), ("rr shut", "Shut the REAR RIGHT door"),
                        ("hatch open", "Open the HATCH / trunk"), ("hatch shut", "Shut the HATCH"),
                        ("all shut", "Everything shut again (confirm we are back to baseline)")]},
    "locks": {"target": "doors",
              "intro": "Doors shut. We lock and unlock the car (key fob or the door button).",
              "steps": [("unlocked", "UNLOCKED (baseline)"), ("locked", "LOCK the car"), ("unlocked", "UNLOCK"),
                        ("locked", "LOCK again"), ("unlocked", "UNLOCK — leave it unlocked")]},
    "lights": {"target": "lights",
               "intro": "READY on, parked. The headlight stalk clicks OFF -> AUTO -> PARKING -> HEADLIGHTS; high beam is a push; fog is a separate switch. Hold each ~3 s. In daylight AUTO may leave the lamps off — that is fine, we are reading the switch state.",
               "steps": [("off", "Headlight switch OFF"),
                         ("auto", "First click: AUTO"),
                         ("parking", "Second click: PARKING / position lights"),
                         ("headlights", "Third click: HEADLIGHTS (low beam)"),
                         ("high beam", "Push the stalk forward: HIGH BEAM"),
                         ("headlights", "Release to LOW beam"),
                         ("off", "Headlight switch OFF"),
                         ("fog on", "Separate FOG switch ON (with headlights if required)"),
                         ("fog off", "FOG switch OFF"),
                         ("off", "Everything OFF")]},
    "pedals": {"target": "pedals",
               "intro": "READY, foot on the brake, gear in PARK, parking brake set. Pressing the accelerator in Park is safe — the car will not move. We step the accelerator, then the brake.",
               "steps": [("rest", "Both pedals up (baseline)"),
                         ("throttle 25", "Accelerator ~1/4"),
                         ("throttle 50", "Accelerator ~1/2"),
                         ("throttle 100", "Accelerator to the floor"),
                         ("throttle 50", "Ease back to ~1/2"),
                         ("rest", "Accelerator up"),
                         ("brake soft", "Press the brake gently"),
                         ("brake hard", "Press the brake firmly"),
                         ("rest", "Both pedals up")]},
    "custom": {"target": "hvac", "intro": "Custom walk.", "steps": []},
}
CHAIN_ALL = ["hvac", "ac", "fresh", "recirc", "auto", "mode", "setpoint"]


def hexs(b):
    return " ".join(f"{x:02X}" for x in b)


def reader_status():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("status")
    except Exception:
        return None


def pause_reader():
    open(PAUSE_FILE, "w").close()
    t0 = time.time()
    while time.time() - t0 < 15:
        if reader_status() in ("paused", "stopped", None):
            return True
        time.sleep(0.5)
    return reader_status() == "paused"


def resume_reader():
    try:
        os.remove(PAUSE_FILE)
    except FileNotFoundError:
        pass


async def capture(elm, target, samples, gap):
    if target["kind"] == "passive":
        ids = target["ids"]
        out = {i: [] for i in ids}
        for n in range(samples):
            first = (n == 0)
            for cid in ids:
                frames = await passive_capture(elm, cid, max(0.2, gap), set_caf=first)
                first = False
                b = last_complete_frame(frames, 2)
                if b:
                    out[cid].append(b)
        return out
    groups, rx = target["groups"], target["rx"]
    out = {g: [] for g in groups}
    for _ in range(samples):
        for g in groups:
            lines = await elm.send(g, wait=0.05, timeout=4.0)
            if not is_no_data(lines):
                d = parse_isotp(lines, rx)
                if len(d) > 1:
                    out[g].append(list(d))
        await asyncio.sleep(gap)
    return out


def analyse(steps, captures, groups):
    """Rank bytes by how well they follow the walk."""
    labels = [s[0] for s in steps]
    report = []
    for g in groups:
        per_step = [captures[i][g] for i in range(len(steps))]
        if not all(per_step):
            continue
        n = min(len(s[0]) for s in per_step)
        for i in range(n):
            vals = []
            stable = True
            for samples in per_step:
                col = [s[i] for s in samples if len(s) > i]
                if len(set(col)) > 1:
                    stable = False
                vals.append(max(set(col), key=col.count))
            if len(set(vals)) < 2:
                continue                                         # never changed
            # consistency: same label → same value (catches up/down walks)
            by_label = {}
            consistent = True
            for lab, v in zip(labels, vals):
                if lab in by_label and by_label[lab] != v:
                    consistent = False
                by_label[lab] = v
            distinct = len(set(vals))
            # monotonic with the step order of the first (rising) half?
            half = vals[: max(2, len(vals) // 2 + 1)]
            mono = all(a <= b for a, b in zip(half, half[1:])) or all(a >= b for a, b in zip(half, half[1:]))
            score = (3 if consistent else 0) + (2 if stable else 0) + (2 if mono else 0) + min(distinct, 8) / 8.0
            report.append((score, g, i, vals, stable, consistent, mono, distinct))
    report.sort(key=lambda r: -r[0])
    return report


async def walk(elm, name, args):
    preset = PRESETS[name]
    target = TARGETS[args.target or preset["target"]]
    steps = [(s.strip(), f"Set: {s.strip()}") for s in args.steps.split(",") if s.strip()] if (args.steps and name == "custom") else preset["steps"]
    if not steps:
        sys.exit("no steps — use --steps \"a,b,c\"")
    groups = target["ids"] if target["kind"] == "passive" else target["groups"]

    print(f"\n{'#' * 70}\nInput walk: {name}  →  {args.target or preset['target']} {'IDs' if target['kind']=='passive' else 'groups'} {groups}")
    print(f"{len(steps)} steps, {args.samples} samples each" + (f", auto-advance {args.auto}s" if args.auto else ", press Enter to advance"))
    print(f"\n  {preset['intro']}\n")
    if target["kind"] != "passive":
        await set_uds_target(elm, target["tx"], target["rx"])
    captures = []
    prev = None
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(ROOT, "tests", "fixtures", f"walk_{name}_{stamp}.json")

    def write(complete):
        tmp = out + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"preset": name, "target": target, "steps": [s[0] for s in steps], "samples": args.samples,
                       "captures": captures, "steps_done": len(captures), "complete": complete,
                       "captured": dt.datetime.now().isoformat()}, f, indent=1)
        os.replace(tmp, out)

    try:
        for idx, (label, instruction) in enumerate(steps):
            print(f"\n[{idx + 1}/{len(steps)}] {instruction}")
            if args.auto:
                for s in range(int(args.auto), 0, -1):
                    print(f"\r    capturing in {s:2d}s …", end="", flush=True)
                    await asyncio.sleep(1)
                print()
            else:
                await asyncio.get_event_loop().run_in_executor(None, input, "    … then press Enter ")
            await asyncio.sleep(args.settle)
            cap = await capture(elm, target, args.samples, args.gap)
            captures.append(cap)
            for g in groups:
                if cap[g]:
                    cur = cap[g][-1]
                    diff = ""
                    if prev and prev.get(g):
                        p = prev[g][-1]
                        ch = [f"b{i}:{p[i]:02X}→{cur[i]:02X}" for i in range(min(len(p), len(cur))) if p[i] != cur[i]]
                        diff = "   changed: " + (" ".join(ch) if ch else "nothing")
                    print(f"    {g}: {hexs(cur)}{diff}")
                else:
                    print(f"    {g}: no data")
            prev = cap
            write(False)                      # partial results survive an abort
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n  aborted after {len(captures)} of {len(steps)} steps — partial capture kept: {out}")
        raise
    write(True)

    print(f"\n{'=' * 70}\nBytes that follow the input  (score: consistent=3, stable=2, monotonic=2, +distinct/8)\n{'=' * 70}")
    steps = steps[:len(captures)]
    labels = [s[0] for s in steps]
    print("step:    " + "  ".join(f"{l[:6]:>6}" for l in labels))
    rep = analyse(steps, captures, groups)
    for score, g, i, vals, stable, consistent, mono, distinct in rep[:14]:
        flags = ("C" if consistent else "-") + ("S" if stable else "-") + ("M" if mono else "-")
        print(f"{g} b{i:<3d} " + "  ".join(f"{v:6d}" for v in vals) + f"   score {score:.1f} [{flags}] {distinct} values")
    if not rep:
        print("nothing changed across the walk — check the target / that the control really moved")
    print(f"\nraw capture saved: {out}")


async def main(args):
    names = ["doors", "locks"] if args.presets == ["body"] else (CHAIN_ALL if args.presets == ["all"] else args.presets)
    if args.start:
        if args.start not in names:
            sys.exit(f"--from {args.start!r} is not in this run: {' '.join(names)}")
        names = names[names.index(args.start):]
    print("Walks:", " → ".join(names))
    print("Pausing the dashboard reader…", end=" ", flush=True)
    print("ok" if pause_reader() else "(no reader running)")
    elm = await detect_adapter(prefer=args.adapter)
    await configure_leaf_bms(elm)
    try:
        for i, name in enumerate(names):
            if i:
                await asyncio.get_event_loop().run_in_executor(None, input, f"\nNext walk: {name}. Press Enter when ready ")
            await walk(elm, name, args)
    finally:
        await elm.close()
        resume_reader()
        print("\nDashboard reader resumed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Step a control through its settings and find the bytes that follow it")
    ap.add_argument("presets", nargs="+", choices=sorted(PRESETS) + ["all", "body"], metavar="preset",
                    help=f"one or more of {', '.join(sorted(PRESETS))}; 'all' = HVAC chain; 'body' = doors locks")
    ap.add_argument("--from", dest="start", metavar="PRESET", help="start the chain at this walk (e.g. --from auto after an abort)")
    ap.add_argument("--steps", help="comma-separated step labels (overrides the preset's steps)")
    ap.add_argument("--target", choices=sorted(TARGETS), help="ECU target (default from preset)")
    ap.add_argument("--adapter", choices=["auto", "usb", "ble"], default="auto")
    ap.add_argument("--samples", type=int, default=4, help="samples per step (default 4)")
    ap.add_argument("--gap", type=float, default=0.4, help="seconds between samples (default 0.4)")
    ap.add_argument("--settle", type=float, default=1.5, help="seconds to wait after you change the control (default 1.5)")
    ap.add_argument("--auto", type=float, default=0, help="advance automatically every N seconds instead of Enter")
    a = ap.parse_args()
    a.adapter = None if a.adapter == "auto" else a.adapter
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        resume_reader()
        print("\nAborted — dashboard reader resumed.")
