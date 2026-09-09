// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Pure geometry and colour maths for the 3D pack tile. No DOM, no three.js, no
// network — node loads it the way tests/test_alerts.py loads alerts.js:
//   globalThis.window = globalThis; require('pack_layout.js'); window.PackLayout
//
// Input is a profile's pack description as the page receives it
// (web/app.py vehicle_ctx → `PACK`; the contract is docs/PACK3D_GUIDE.md):
//   {module: {L, W, T}, case: {...}, layout: [stack, ...], sensors: [...], modes: [...]}
// A stack places `n` modules and says how they map to measured values:
//   split  values per module, sliced through the module's thickness (Leaf: 2 — the
//          2s2p module reports two cell pairs);
//   group  modules per value (Prius NiMH: 2 — the ECU reports one voltage per two
//          modules). group > 1 forces split 1.
// Millimetres, car coordinates: x forward, y up, z toward the passenger side.
// Bodies are what is drawn (one per module slice); values are what is measured
// (`body.v` indexes the mode's value list, e.g. `cells`). For the Leaf the two
// coincide; for a grouped pack several bodies share one value.
(function () {
  'use strict';
  const TRAY = 20;                       // the tray floor sits this far above the case bottom
  const GAP = 3;                         // hairline between bodies so they read as separate
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  function bodies(pack) {
    const M = pack.module, out = [], modules = [];
    let values = 0;
    for (const s of pack.layout) {
      const group = Math.max(1, s.group | 0), split = group > 1 ? 1 : Math.max(1, s.split == null ? 2 : s.split | 0);
      for (let k = 0; k < s.n; k++) {
        const m = modules.length;
        let cx, cy, cz, sx, sy, sz, thickAxis;
        if (s.kind === 'edge') {
          // on edge: long side along x, height along y, thickness along z;
          // module k runs from the +z end (passenger) toward −z (driver)
          cx = s.x; cy = TRAY + M.W / 2; cz = (s.n / 2 - 0.5 - k) * M.T + (s.z || 0);
          sx = M.L; sy = M.W; sz = M.T; thickAxis = 'z';
        } else {
          // flat: long side across the car (z), short side along x, thickness up (y);
          // module k counts from the bottom of the stack
          cx = s.x; cy = TRAY + M.T / 2 + k * M.T; cz = s.z;
          sx = M.W; sy = M.T; sz = M.L; thickAxis = 'y';
        }
        const vFirst = s.first + (group > 1 ? Math.floor(k / group) : k * split);
        modules.push({ m, cx, cy, cz, sx, sy, sz, kind: s.kind, side: Math.sign(s.z || 0), stack: s.name, vFirst, split, group });
        for (let h = 0; h < split; h++) {
          const v = vFirst + (group > 1 ? 0 : h);
          // slices through the thickness, the first on the +axis side
          const slice = M.T / split, off = ((split - 1) / 2 - h) * slice;
          const b = { i: out.length, v, m, cx, cy, cz, sx: sx - GAP, sy, sz, loc: s.name, verify: s.verify || '', halfAxis: thickAxis, shared: group > 1 };
          if (thickAxis === 'z') { b.cz += off; b.sz = slice - (split > 1 ? GAP / 2 : GAP); b.sy -= GAP; }
          else { b.cy += off; b.sy = slice - (split > 1 ? GAP / 2 : GAP); b.sz -= GAP; }
          out.push(b);
          values = Math.max(values, v + 1);
        }
      }
    }
    return { bodies: out, modules, values };
  }

  function stats(vals) {
    let min = Infinity, max = -Infinity, sum = 0, n = 0, imin = -1, imax = -1;
    for (let i = 0; i < vals.length; i++) {
      const v = vals[i]; if (v == null) continue;
      sum += v; n++;
      if (v < min) { min = v; imin = i; }
      if (v > max) { max = v; imax = i; }
    }
    return { min, max, imin, imax, mean: n ? sum / n : 0 };
  }

  // Colour scales: t ∈ [0, 1] feeds Tiles.cellColor (0 = red/low, 1 = blue/high);
  // a mode with `invert: true` (temperatures: hot should be red) flips t.
  //   abs  — the grid's own scale: lowest value of the frame → highest
  //   dev  — deviation from the mean at that instant; ±cfg.dev spans the scale
  //          (the weak-cell view: under load every pair sags, this shows who sags more)
  //   drop — drop from the value's own rest reading (first frame seen); cfg.drop → 0
  //          (a per-pair internal-resistance proxy; voltage packs only)
  const SCALES = {
    abs:  { label: 'absolute (grid scale)',
            t: (v, f) => f.max === f.min ? 1 : (v - f.min) / (f.max - f.min),
            lo: (f, u) => `${f.min} ${u}`, hi: (f, u) => `${f.max} ${u}` },
    dev:  { label: 'deviation from the mean',
            t: (v, f, i, rest, cfg) => clamp(0.5 + (v - f.mean) / (2 * ((cfg && cfg.dev) || 50)), 0, 1),
            lo: (f, u, cfg) => `−${(cfg && cfg.dev) || 50} ${u}`, hi: (f, u, cfg) => `+${(cfg && cfg.dev) || 50} ${u} vs mean` },
    drop: { label: 'drop from own rest value',
            t: (v, f, i, rest, cfg) => 1 - clamp(((rest && rest[i] != null ? rest[i] : v) - v) / ((cfg && cfg.drop) || 300), 0, 1),
            lo: (f, u, cfg) => `−${(cfg && cfg.drop) || 300} ${u}`, hi: (f, u) => `0 ${u} from rest` },
  };

  // the default mode when a profile declares none: the Leaf's cell pairs
  const DEFAULT_MODE = { id: 'volt', key: 'cells', name: 'cell pair', unit: 'mV', scales: ['abs', 'dev', 'drop'], dev: 50, drop: 300, invert: false };

  window.PackLayout = { bodies, stats, SCALES, DEFAULT_MODE, clamp, TRAY };
})();
