#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scratch analysis of the 2026-09-03 drive: group-05 current scale + road load.

READ-ONLY on the database (opened with ?immutable=1). Prints aggregates only.
Not part of the package; run manually:
    python tools/analyse_drive_20260903.py [db_path]
"""
import json, math, sqlite3, sys

DB = sys.argv[1] if len(sys.argv) > 1 else "web/leaf_battery.db"
T0, T1 = "2026-09-03T21:40:00Z", "2026-09-03T22:05:00Z"

con = sqlite3.connect(f"file:{DB}?immutable=1", uri=True)
con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute(
    "SELECT ts, ts_epoch, soc, pack_v, current_a, power_kw, gear, speed_mph, extra, "
    "capacity_ah FROM readings WHERE ts >= ? AND ts <= ? ORDER BY ts_epoch", (T0, T1))]
con.close()
for r in rows:
    e = json.loads(r["extra"] or "{}")
    r["g05"] = e.get("g05_current_a")
    r["hv1"] = e.get("hv_current1_a")
    r["hv2"] = e.get("hv_current2_a")
    r["odo"] = e.get("odometer_mi")
    for k in ("brake_pct", "brake_on", "headlights", "parking_lights", "lights_on",
              "start_state_name", "fog_lights", "high_beam", "hvac_blower_v"):
        r[k] = e.get(k)

def wrap(a, span=64.0):
    """s16/1024 aliasing: fold a true current into the ±32 A representable band."""
    return (a + span / 2) % span - span / 2

# ── freshness: g05 has period 5, so a value repeats across ~2 rows.
fresh = []
last = object()
for i, r in enumerate(rows):
    if r["g05"] is not None and r["g05"] != last:
        last = r["g05"]
        r["fresh"] = True
        fresh.append(i)
    else:
        r["fresh"] = False

print(f"rows={len(rows)}  fresh g05 samples={len(fresh)}")
g = [r["g05"] for r in rows if r["g05"] is not None]
print(f"g05 range {min(g):+.3f} .. {max(g):+.3f}   distinct={len(set(g))}")
h2 = [r["hv2"] for r in rows if r["hv2"] is not None]
h1 = [r["hv1"] for r in rows if r["hv1"] is not None]
print(f"hv2 range {min(h2):+.3f} .. {max(h2):+.3f}")
print(f"hv1 range {min(h1):+.3f} .. {max(h1):+.3f}")

# ── H-clamp: pile-up at the rail?
RAIL = 31.5
hi = sorted(abs(x) for x in g)
print(f"\n|g05| >= {RAIL}: {sum(1 for x in g if abs(x) >= RAIL)} of {len(g)} samples")
print(f"|g05| >= 31.0: {sum(1 for x in g if abs(x) >= 31.0)}")
print(f"top 8 |g05|: {[round(x,3) for x in hi[-8:]]}")

# ── steady selection: |d hv2/dt| small on both sides of the fresh sample.
def steady(i, thresh):
    """hv2 changes little between the previous, this and the next row."""
    vals = [rows[j]["hv2"] for j in (i - 1, i, i + 1) if 0 <= j < len(rows)]
    vals = [v for v in vals if v is not None]
    return len(vals) == 3 and (max(vals) - min(vals)) <= thresh

for thresh in (10.0, 5.0, 3.0):
    sel = [i for i in fresh if steady(i, thresh) and rows[i]["hv2"] is not None]
    if not sel:
        continue
    e_raw = [abs(rows[i]["g05"] - rows[i]["hv2"]) for i in sel]
    e_wrap = [abs(rows[i]["g05"] - wrap(rows[i]["hv2"])) for i in sel]
    big = [i for i in sel if abs(rows[i]["hv2"]) > 32.0]
    print(f"\n--- steady window (hv2 spread <= {thresh} A over 3 rows): n={len(sel)}"
          f"  of which |hv2|>32: {len(big)}")
    print(f"    median |g05 - hv2|      = {sorted(e_raw)[len(e_raw)//2]:.3f} A")
    print(f"    median |g05 - wrap(hv2)|= {sorted(e_wrap)[len(e_wrap)//2]:.3f} A")
    if big:
        br = [abs(rows[i]["g05"] - rows[i]["hv2"]) for i in big]
        bw = [abs(rows[i]["g05"] - wrap(rows[i]["hv2"])) for i in big]
        print(f"    above the band: median |raw|={sorted(br)[len(br)//2]:.3f}"
              f"  median |wrap|={sorted(bw)[len(bw)//2]:.3f}")
        for i in big:
            print(f"      {rows[i]['ts']}  hv2={rows[i]['hv2']:+8.3f}  "
                  f"wrap={wrap(rows[i]['hv2']):+8.3f}  g05={rows[i]['g05']:+8.3f}  "
                  f"err={rows[i]['g05']-wrap(rows[i]['hv2']):+7.3f}")

# ── in-band check: is the scale right below 32 A?
inb = [i for i in fresh if rows[i]["hv2"] is not None and abs(rows[i]["hv2"]) <= 30.0
       and steady(i, 3.0)]
if inb:
    e = sorted(abs(rows[i]["g05"] - rows[i]["hv2"]) for i in inb)
    print(f"\nin-band (|hv2|<=30, steady<=3 A): n={len(inb)} "
          f"median err={e[len(e)//2]:.3f} A  max={e[-1]:.3f} A")

# ── slope hypothesis: least squares g05 = m*hv2 + b over steady in-band rows
def fit(xs, ys):
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    sxx = sum((x-mx)**2 for x in xs); sxy = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    m = sxy/sxx; b = my - m*mx
    ss_res = sum((y-(m*x+b))**2 for x, y in zip(xs, ys))
    ss_tot = sum((y-my)**2 for y in ys)
    return m, b, 1 - ss_res/ss_tot
if inb:
    m, b, r2 = fit([rows[i]["hv2"] for i in inb], [rows[i]["g05"] for i in inb])
    print(f"in-band fit g05 = {m:.4f}*hv2 + {b:+.3f}   R2={r2:.4f}")
allf = [i for i in fresh if rows[i]["hv2"] is not None and steady(i, 5.0)]
if allf:
    m, b, r2 = fit([rows[i]["hv2"] for i in allf], [rows[i]["g05"] for i in allf])
    print(f"all-steady fit g05 = {m:.4f}*hv2 + {b:+.3f}   R2={r2:.4f}   n={len(allf)}")
    m, b, r2 = fit([wrap(rows[i]["hv2"]) for i in allf], [rows[i]["g05"] for i in allf])
    print(f"all-steady fit g05 = {m:.4f}*wrap(hv2) + {b:+.3f}   R2={r2:.4f}")

# ── sensor 1 vs sensor 2 ratio under load (side observation)
rat = [rows[i]["hv1"]/rows[i]["hv2"] for i in range(len(rows))
       if rows[i]["hv1"] and rows[i]["hv2"] and abs(rows[i]["hv2"]) > 15]
if rat:
    rat.sort()
    print(f"\nhv1/hv2 under load (|hv2|>15): n={len(rat)} median={rat[len(rat)//2]:.3f}"
          f"  p10={rat[len(rat)//10]:.3f} p90={rat[-len(rat)//10]:.3f}")

# ── how far the fused canonical current strays from sensor 2 during the drive
d = sorted(abs(r["current_a"] - r["hv2"]) for r in rows
           if r["current_a"] is not None and r["hv2"] is not None
           and (r["speed_mph"] or 0) > 1)
if d:
    print(f"|current_a - hv2| while moving: n={len(d)} median={d[len(d)//2]:.2f} "
          f"p90={d[int(.9*len(d))]:.2f} max={d[-1]:.2f} A")

# ── Hypothesis test over the above-band samples, with a fair ±1-row allowance
#    for the lbc01/lbc05 sampling skew (lbc01 is period 0, lbc05 period 5, so
#    the two reads are ~0.5-1 s apart). Every hypothesis gets the same freedom:
#    it may pick whichever of the three neighbouring hv2 reads suits it best.
print("\n=== hypothesis test, |hv2| > 32 A samples ===")
HYP = {
    "linear (g05 = hv2)":        lambda a: a,
    "clamp at +/-31.999":        lambda a: max(-31.999, min(31.999, a)),
    "wrap (s16 aliasing)":       wrap,
    "half scale (g05 = hv2/2)":  lambda a: a / 2.0,
}
res = {k: [] for k in HYP}
n_above = 0
for i in fresh:
    cand = [rows[j]["hv2"] for j in (i - 1, i, i + 1)
            if 0 <= j < len(rows) and rows[j]["hv2"] is not None]
    if not cand or max(abs(c) for c in cand) <= 32.0:
        continue
    n_above += 1
    for name, fn in HYP.items():
        res[name].append(min(abs(rows[i]["g05"] - fn(c)) for c in cand))
print(f"n = {n_above} fresh g05 samples with a neighbouring |hv2| > 32 A")
for name in HYP:
    e = sorted(res[name])
    if e:
        print(f"  {name:26} median |err| = {e[len(e)//2]:6.2f} A   "
              f"p90 = {e[int(.9*len(e))]:6.2f} A   max = {e[-1]:6.2f} A")

# ── clamp falsification: if it clamped, every above-band sample sits at the rail
pinned = 0
for i in fresh:
    cand = [rows[j]["hv2"] for j in (i - 1, i, i + 1)
            if 0 <= j < len(rows) and rows[j]["hv2"] is not None]
    if cand and max(abs(c) for c in cand) > 40.0:
        if abs(rows[i]["g05"]) >= 31.0:
            pinned += 1
print(f"\nclamp test: of the fresh samples whose neighbourhood exceeded 40 A, "
      f"{pinned} sit at |g05| >= 31 A")
band = [abs(x) for x in g]
print("histogram of |g05| (2 A bins):")
for lo in range(0, 34, 2):
    c = sum(1 for x in band if lo <= x < lo + 2)
    print(f"  {lo:2d}-{lo+2:2d} A  {'#'*c} {c}")

# ── Noise floor control: the same best-of-3 residual for IN-band samples taken
#    under equally fast transients. If the wrap residual matches this, the wrap
#    hypothesis is as good as the measurement can resolve.
def slew(i):
    c = [rows[j]["hv2"] for j in (i - 1, i, i + 1)
         if 0 <= j < len(rows) and rows[j]["hv2"] is not None]
    return (max(c) - min(c)) if len(c) == 3 else None

above, inband = [], []
for i in fresh:
    c = [rows[j]["hv2"] for j in (i - 1, i, i + 1)
         if 0 <= j < len(rows) and rows[j]["hv2"] is not None]
    if len(c) != 3:
        continue
    err = min(abs(rows[i]["g05"] - wrap(x)) for x in c)
    (above if max(abs(x) for x in c) > 32.0 else inband).append((slew(i), err, i))

hi_slew = [e for s, e, _ in inband if s and s > 15.0]
print(f"\nnoise-floor control: in-band fresh samples with >15 A of slew in the "
      f"3-row window: n={len(hi_slew)} median best-of-3 |err| = "
      f"{sorted(hi_slew)[len(hi_slew)//2]:.2f} A" if hi_slew else "")
ab = sorted(e for _, e, _ in above)
print(f"above-band wrap residual: n={len(ab)} median {ab[len(ab)//2]:.2f} A")
print("\nabove-band samples, steadiest first (slew, hv2, wrap(hv2), g05, resid):")
for s, e, i in sorted(above)[:0] or sorted(above, key=lambda t: t[0])[:10]:
    c = [rows[j]["hv2"] for j in (i - 1, i, i + 1)]
    best = min(c, key=lambda x: abs(rows[i]["g05"] - wrap(x)))
    print(f"  slew={s:5.1f}  {rows[i]['ts']}  hv2={best:+8.3f}  "
          f"wrap={wrap(best):+7.3f}  g05={rows[i]['g05']:+7.3f}  resid={e:+.3f}")

# ── Absolute check on the DIV-1024 scale: coulomb-count sensor 2 over the drive
#    and compare against the BMS's own SOC x capacity. This is independent of
#    the group-01/group-05 cross-check (which only proves the two share a scale).
seq = [(r["ts_epoch"], r["hv2"], r["soc"], r["capacity_ah"])
       for r in rows if r["hv2"] is not None and r["soc"] is not None]
cap = next((c for _, _, _, c in seq if c), None)
if cap and len(seq) > 2:
    ah = 0.0
    for (t0, i0, _, _), (t1, i1, _, _) in zip(seq, seq[1:]):
        dt = t1 - t0
        if 0 < dt < 30:                      # skip gaps
            ah += (i0 + i1) / 2.0 * dt / 3600.0
    d_soc = seq[-1][2] - seq[0][2]
    ah_soc = d_soc / 100.0 * cap
    print(f"\ncoulomb count over {seq[-1][0]-seq[0][0]:.0f} s: "
          f"integral(sensor 2) = {ah:+.3f} Ah")
    print(f"  BMS SOC {seq[0][2]:.2f} -> {seq[-1][2]:.2f} %  x capacity {cap:.2f} Ah "
          f"= {ah_soc:+.3f} Ah")
    if ah_soc:
        print(f"  ratio integral/SOC = {ah/ah_soc:.3f}  "
              f"(1.00 means the /1024 scale is right in absolute terms)")

# ═══ Part 2: road load, with grade unmeasured ═════════════════════════════
# There is no GPS, altitude or inclinometer in this capture, and 0x1D5 torque /
# 0x260 power limits are not in ITEMS, so grade cannot be regressed out. The
# canonical `current_a` is ALSO unusable here (apply_policy fuses it with a
# g05-derived offset that is the wrap error above 32 A, and zeroes positive
# current while discharging), so pack power is recomputed from sensor 2.
print("\n\n=== Part 2: road load ===")
for r in rows:
    r["mph"] = r["speed_mph"]
    r["kw"] = (r["pack_v"] * r["hv2"] / 1000.0) if (r["pack_v"] and r["hv2"] is not None) else None

mv = [r for r in rows if r["mph"] is not None]
print(f"odometer {mv[0]['odo']} -> {mv[-1]['odo']} mi  ({mv[-1]['odo']-mv[0]['odo']} mi)")
dist = sum((a["mph"] + b["mph"]) / 2 * (b["ts_epoch"] - a["ts_epoch"]) / 3600.0
           for a, b in zip(mv, mv[1:]) if 0 < b["ts_epoch"] - a["ts_epoch"] < 30)
print(f"speed-integrated distance = {dist:.2f} mi")

# Route shape: is it an out-and-back? A mirror-symmetric speed profile is the
# only evidence available without GPS.
drv = [r for r in rows if (r["mph"] or 0) > 1]
sp = [r["mph"] for r in drv]
half = len(sp) // 2
fwd, rev = sp[:half], list(reversed(sp[len(sp)-half:]))
def corr(a, b):
    n = len(a); ma = sum(a)/n; mb = sum(b)/n
    va = sum((x-ma)**2 for x in a); vb = sum((y-mb)**2 for y in b)
    return sum((x-ma)*(y-mb) for x, y in zip(a, b)) / math.sqrt(va*vb)
print(f"mirror correlation of the speed profile (out-and-back test) = {corr(fwd, rev):+.3f}"
      "   (near +1 would suggest the same roads driven back)")

# Steady-speed selection: speed changes little across 3 consecutive rows.
def bins(max_dv):
    out = {}
    for i in range(1, len(rows) - 1):
        w = [rows[j] for j in (i-1, i, i+1)]
        if any(r["mph"] is None or r["kw"] is None for r in w):
            continue
        s = [r["mph"] for r in w]
        if max(s) - min(s) > max_dv or rows[i]["mph"] < 5:
            continue
        if rows[i]["gear"] not in ("D", "Eco"):
            continue
        b = int(rows[i]["mph"] // 5) * 5
        out.setdefault(b, []).append(rows[i]["kw"])
    return out

for max_dv in (3.0, 5.0):
    B = bins(max_dv)
    print(f"\nsteady-speed bins (speed spread <= {max_dv} mph over 3 rows, D/Eco, >=5 mph)")
    print(f"  {'bin':>8} {'n':>3} {'min kW':>8} {'med kW':>8} {'max kW':>8}   model+base")
    for b in sorted(B):
        v = sorted(B[b])
        mid = b + 2.5
        model = -( (150.0*(mid*0.44704) + 0.38*(mid*0.44704)**3)/0.85/1000.0 + 0.15 )
        print(f"  {b:3d}-{b+5:<3d} {len(v):>4} {v[0]:8.2f} {v[len(v)//2]:8.2f} "
              f"{v[-1]:8.2f}   {model:8.2f}")

# Base draw: stopped, in READY (gear reported, speed 0), pack power.
base = sorted(r["kw"] for r in rows
              if r["mph"] == 0.0 and r["kw"] is not None and r["gear"] in ("P", "D", "Eco", "N"))
if base:
    print(f"\nstationary READY pack power: n={len(base)} median={base[len(base)//2]:.3f} kW "
          f"(min {base[0]:.3f}, max {base[-1]:.3f})")

# Regen episodes. brake_pct is not logged, so only lift-off/deceleration regen
# is observable, and on an unknown grade.
reg = [(r["mph"], r["kw"], r["gear"]) for r in rows
       if r["kw"] and r["kw"] > 0.5 and (r["mph"] or 0) > 3]
reg.sort(key=lambda t: -t[1])
print(f"\nregen (pack power > +0.5 kW while moving): n={len(reg)}, top 10:")
for mph, kw, gear in reg[:10]:
    print(f"   {mph:5.1f} mph  {kw:+6.2f} kW  {gear}")
if reg:
    kws = sorted(k for _, k, _ in reg)
    print(f"   median {kws[len(kws)//2]:+.2f} kW   p90 {kws[int(.9*len(kws))]:+.2f} kW"
          f"   max {kws[-1]:+.2f} kW")

# ── Regen split by the brake pedal. `brake_pct` IS logged; the accelerator is
#    not, so "coast" here means brake released and the car decelerating.
print("\n=== regen, split by brake_pct ===")
coast, braked = [], []
for i, r in enumerate(rows):
    if r["kw"] is None or (r["mph"] or 0) < 3 or r["gear"] not in ("D", "Eco"):
        continue
    if r["kw"] <= 0.2:
        continue
    (braked if (r["brake_pct"] or 0) > 2 else coast).append(r)
for name, grp in (("coast (brake <= 2 %)", coast), ("braked (brake > 2 %)", braked)):
    if not grp:
        print(f"  {name}: none")
        continue
    k = sorted(x["kw"] for x in grp)
    print(f"  {name}: n={len(grp)}  median {k[len(k)//2]:+.2f} kW  max {k[-1]:+.2f} kW")
    for x in sorted(grp, key=lambda r: -r["kw"])[:8]:
        print(f"      {x['ts']}  {x['mph']:5.1f} mph  {x['gear']:4} "
              f"brake={x['brake_pct']}  {x['kw']:+6.2f} kW")

# Coast regen vs speed, above REGEN_FULL_MPH, by gear: this is the one term
# grade cannot fake, because lift-off regen is a commanded torque, not a
# balance of forces. Grade changes how fast the car slows, not the regen power.
print("\ncoast regen by gear and speed band (brake <= 2 %):")
for gear in ("D", "Eco"):
    for lo in (3, 10, 15, 25, 35):
        v = sorted(x["kw"] for x in coast
                   if x["gear"] == gear and lo <= x["mph"] < lo + 10)
        if v:
            print(f"  {gear:4} {lo:2d}-{lo+10:2d} mph  n={len(v):2d}  "
                  f"median {v[len(v)//2]:+.2f}  max {v[-1]:+.2f} kW")

print("\nbrake_pct while moving:", sorted({r["brake_pct"] for r in rows
      if r["brake_pct"] is not None and (r["mph"] or 0) > 1}))
print("lights_on values:", sorted({str(r["lights_on"]) for r in rows}),
      " headlights:", sorted({str(r["headlights"]) for r in rows}),
      " start_state:", sorted({str(r["start_state_name"]) for r in rows}),
      " blower_v:", sorted({str(r["hvac_blower_v"]) for r in rows}))

# Peak traction actually seen (a lower bound on MOTOR_PEAK_KW, nothing more)
tr = sorted((r["kw"], r["mph"], r["ts"]) for r in rows
            if r["kw"] is not None and (r["mph"] or 0) > 1)
print(f"\npeak traction draw seen: {tr[0][0]:.2f} kW at {tr[0][1]:.1f} mph ({tr[0][2]})")
print(f"top speed: {max(r['mph'] or 0 for r in rows):.1f} mph")
