// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Pure geometry and colour maths for the 3D pack tile. No DOM, no three.js, no
// network — node loads it the way tests/test_alerts.py loads alerts.js:
//   globalThis.window = globalThis; require('pack_layout.js'); window.PackLayout
//
// Input is the profile's pack description as the page receives it
// (web/app.py vehicle_ctx → `PACK`): {module: {L, W, T}, case: {...},
// layout: [{name, kind, x, z, n, first, verify}, ...], sensors: [...]}.
// Millimetres, car coordinates: x forward, y up, z toward the passenger side.
(function () {
  'use strict';
  const TRAY = 20;                       // the tray floor sits this far above the case bottom
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  // One body per cell pair: a half-slab of its module, split through the module's
  // thickness (the two series pairs are stacked through it). The first pair of a
  // module sits on the +axis half. Returns {bodies[96], modules[48]}.
  function bodies(pack) {
    const M = pack.module, out = [], modules = [];
    for (const s of pack.layout) {
      for (let k = 0; k < s.n; k++) {
        const m = (s.first >> 1) + k;                    // module number 0..47
        let cx, cy, cz, sx, sy, sz, halfAxis;
        if (s.kind === 'edge') {
          // on edge: long side along x, height along y, thickness along z;
          // module k runs from the +z end (passenger) toward −z (driver)
          cx = s.x; cy = TRAY + M.W / 2; cz = (s.n / 2 - 0.5 - k) * M.T + (s.z || 0);
          sx = M.L; sy = M.W; sz = M.T; halfAxis = 'z';
        } else {
          // flat: long side across the car (z), short side along x, thickness up (y);
          // module k counts from the bottom of the stack
          cx = s.x; cy = TRAY + M.T / 2 + k * M.T; cz = s.z;
          sx = M.W; sy = M.T; sz = M.L; halfAxis = 'y';
        }
        modules.push({ m, cx, cy, cz, sx, sy, sz, kind: s.kind, side: Math.sign(s.z || 0), stack: s.name });
        for (let h = 0; h < 2; h++) {
          const i = s.first + k * 2 + h;
          const off = (h === 0 ? 1 : -1) * M.T / 4;
          const b = { i, m, cx, cy, cz, sx: sx - 3, sy, sz, loc: s.name, verify: s.verify || '', halfAxis };
          if (halfAxis === 'z') { b.cz += off; b.sz = M.T / 2 - 1.5; b.sy -= 3; }
          else { b.cy += off; b.sy = M.T / 2 - 1.5; b.sz -= 3; }
          out[i] = b;
        }
      }
    }
    return { bodies: out, modules };
  }

  function stats(cells) {
    let min = Infinity, max = -Infinity, sum = 0, imin = -1, imax = -1;
    for (let i = 0; i < cells.length; i++) {
      const v = cells[i]; if (v == null) continue;
      sum += v;
      if (v < min) { min = v; imin = i; }
      if (v > max) { max = v; imax = i; }
    }
    return { min, max, imin, imax, mean: cells.length ? sum / cells.length : 0 };
  }

  // Colour scales: t ∈ [0, 1] feeds Tiles.cellColor (0 = red/low, 1 = blue/high).
  //   abs  — the module grid's own scale: lowest pair of the frame → highest
  //   dev  — deviation from the pack mean at that instant; ±50 mV spans the scale.
  //          The weak-cell view: under load every pair sags together, this one
  //          shows who sags more.
  //   drop — drop from the pair's own rest voltage (first frame seen); 300 mV → red.
  //          A per-pair internal-resistance proxy.
  const SCALES = {
    abs:  { label: 'absolute mV (grid scale)',
            t: (mv, f) => f.max === f.min ? 1 : (mv - f.min) / (f.max - f.min),
            lo: f => `${f.min} mV`, hi: f => `${f.max} mV` },
    dev:  { label: 'deviation from pack mean',
            t: (mv, f) => clamp(0.5 + (mv - f.mean) / 100, 0, 1),
            lo: () => '−50 mV', hi: () => '+50 mV vs mean' },
    drop: { label: 'drop from own rest voltage',
            t: (mv, f, i, rest) => 1 - clamp(((rest && rest[i] != null ? rest[i] : mv) - mv) / 300, 0, 1),
            lo: () => '−300 mV', hi: () => '0 mV from rest' },
  };

  window.PackLayout = { bodies, stats, SCALES, clamp, TRAY };
})();
