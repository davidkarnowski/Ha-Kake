// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The 3D battery-pack tile. An ES module (three.js ships as modules only; the
// page's import map resolves 'three' to web/static/vendor/three/). Everything
// else on the page is a classic script, so this file talks to the page through
// globals: it reads `PACK` (the profile's layout, from vehicle_ctx), `PackLayout`
// (pure geometry, pack_layout.js), `Tiles.cellColor` and `TileStudio`, and it
// publishes `window.Pack3D = { render, setOpts, dispose }`. The page calls
// Pack3D.render(document, data) from updateDash on every poll; because module
// scripts are deferred, the first record may arrive before this file runs, and
// updateDash parks it in window.__pack3dPending for us to pick up.
//
// Geometry: 48 modules from the profile's PACK_LAYOUT, each split into two
// half-slabs (one per measured cell pair) with the rounded edges of the real
// module. A rounded box cannot be scaled per instance without distorting its
// corners, so there is one InstancedMesh per body size (the flat halves, the
// on-edge halves) and a slot table maps a pair index to (mesh, instance).
// Per-instance colour is the body's measured value (`body.v` into the mode's list —
// `cells` for the Leaf) on the chosen scale; a profile may declare several modes
// (voltages, per-module temperatures) and the ⋯ menu switches between them. A translucent
// case, module outlines, terminal studs and the four temperature sensors (each
// coloured by its own reading) give it the shape of the real pack. Labels are
// DOM elements tracked by CSS2DRenderer, so they use the dashboard's own fonts.
// Clicking a pair pins its module — a glowing box, a bobbing pin and a label —
// and opens a side pane with both pairs of that module and the module's own
// spread and average; clicking a sensor ball does the same for that sensor. Pairs are numbered 1–96
// on screen, the service manual's count; `cells[i]` and every index here stay 0-based.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

const VIEWS = { iso: [1500, 1300, 1700], top: [1, 2600, 1], rear: [-2100, 700, 0], driver: [200, 650, -2300] };
// scale 'abs' is the cell grid's own colouring, so a pair reads the same colour side by side
const DEFAULT_OPTS = { scale: 'abs', labels: 'minmax', case: 0.14, view: 'iso', spin: false, flash: true, flashBelow: '', flashAbove: '', mode: '' };
const WHITE = new THREE.Color(0xffffff), BLUE = new THREE.Color(0x42a5f5), ACCENT = 0x4fc3f7;
const EDGE_RADIUS = 6;                                   // mm, the real module's rounded edge

const state = {
  built: false, opts: Object.assign({}, DEFAULT_OPTS), rest: null, playback: false,
  hover: -1, pinned: -1, hoverSensor: -1, pinnedSensor: -1, last: null, host: null, note: null, pane: null,
  flashing: [],                        // [{i, base, to}] — the lowest → white, the highest → blue, thresholds likewise
  expanded: false, baseH: null,
};
let renderer, labelRenderer, scene, camera, controls, caseMat, hoverBox, pairLabels, bodies, modules, nValues = 0, ro, sensors = [];
let MODES = [PackLayout ? PackLayout.DEFAULT_MODE : null];   // the profile's value modes (PACK_MODES), or the Leaf default
let groups = [], slot = [];                              // groups[g] = {mesh, ids}; slot[i] = {g, k}
let selBox, selPin, selLabel;                            // the pinned module's marker
let needsRender = true, onScreen = true, lastTick = 0;    // render on demand: see loop()
const colorCache = new Map(), tmpColor = new THREE.Color();

// ── build once ────────────────────────────────────────────────────────────
function build(root) {
  const host = root.querySelector('#pack3d');
  // The page declares the layout with a top-level `const PACK`, which other scripts
  // see by name but which is NOT a window property; the page also assigns
  // window.PACK, and this reads whichever is there so a stale page still works.
  const pack = (typeof window.PACK !== 'undefined' && window.PACK) || (typeof PACK !== 'undefined' ? PACK : null);
  if (!host || !pack || !window.PackLayout || state.built) return;
  if (!host.clientWidth) return;                       // hidden tile: wait for tiles:applied
  const PACK = pack, C = PACK.case;
  ({ bodies, modules, values: nValues } = PackLayout.bodies(PACK));
  MODES = (PACK.modes && PACK.modes.length) ? PACK.modes.map(m => Object.assign({}, PackLayout.DEFAULT_MODE, m)) : [PackLayout.DEFAULT_MODE];
  state.host = host; state.note = root.querySelector('#pack3d-note'); state.pane = host.querySelector('.pack3d-pane');

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  host.prepend(renderer.domElement);
  labelRenderer = new CSS2DRenderer({ element: host.querySelector('.pack3d-labels') });
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(38, 1, 10, 20000);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08; controls.target.set(0, 80, 0);
  controls.autoRotateSpeed = 0.6;
  controls.addEventListener('change', () => { needsRender = true; });

  // Lit so a top face shows its plain colour: a white hemisphere at π (three.js's
  // physical units) gives the top ≈ albedo, a weak low sun shapes the sides. That
  // is what keeps a pair the same colour here as in the cell grid, which paints the
  // plain colour.
  scene.add(new THREE.HemisphereLight(0xffffff, 0x334466, Math.PI));
  const sun = new THREE.DirectionalLight(0xffffff, 0.25 * Math.PI); sun.position.set(-900, 450, 1400); scene.add(sun);

  // case: tray + rear hump, translucent, edged
  caseMat = new THREE.MeshPhysicalMaterial({ color: 0x7f97b8, transparent: true, opacity: state.opts.case, roughness: 0.35, metalness: 0.2, depthWrite: false, side: THREE.DoubleSide });
  const edgeMat = new THREE.LineBasicMaterial({ color: 0x3a4a6a, transparent: true, opacity: 0.9 });
  const box = (w, h, d, x, y, z) => {
    const g = new THREE.BoxGeometry(w, h, d);
    const m = new THREE.Mesh(g, caseMat); m.position.set(x, y, z); scene.add(m);
    const e = new THREE.LineSegments(new THREE.EdgesGeometry(g), edgeMat); e.position.copy(m.position); scene.add(e);
  };
  box(C.L, C.H, C.W, 0, C.H / 2, 0);
  if (C.hump) box(C.hump.L, C.hump.H, C.hump.W, -C.L / 2 + C.hump.L / 2, C.hump.H / 2, 0);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(2600, 2200), new THREE.MeshStandardMaterial({ color: 0x0d1320, roughness: 1 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = -2; scene.add(floor);
  const grid = new THREE.GridHelper(2600, 26, 0x1e2a42, 0x161f33); grid.position.y = -1; scene.add(grid);

  // 96 rounded half-slabs: one instanced mesh per body size, matte so the colour is the colour
  const slabMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 1, metalness: 0 });
  const bySize = new Map();
  bodies.forEach(b => { const key = `${b.sx}|${b.sy}|${b.sz}`; if (!bySize.has(key)) bySize.set(key, []); bySize.get(key).push(b.i); });
  groups = []; slot = new Array(bodies.length);
  const grey = new THREE.Color(0x6b7a99), mat = new THREE.Matrix4(), q = new THREE.Quaternion(), one = new THREE.Vector3(1, 1, 1);
  for (const ids of bySize.values()) {
    const b0 = bodies[ids[0]];
    const r = Math.min(EDGE_RADIUS, b0.sx / 2, b0.sy / 2, b0.sz / 2);
    const mesh = new THREE.InstancedMesh(new RoundedBoxGeometry(b0.sx, b0.sy, b0.sz, 2, r), slabMat, ids.length);
    ids.forEach((i, k) => {
      const b = bodies[i];
      mat.compose(new THREE.Vector3(b.cx, b.cy, b.cz), q, one);
      mesh.setMatrixAt(k, mat); mesh.setColorAt(k, grey);
      slot[i] = { g: groups.length, k };
    });
    scene.add(mesh);
    groups.push({ mesh, ids });
  }

  // module outlines and terminal studs (terminals up on the rear block, inboard on the flat stacks)
  const modEdge = new THREE.LineBasicMaterial({ color: 0x0a0e17, transparent: true, opacity: 0.45 });
  const terms = new THREE.InstancedMesh(new THREE.CylinderGeometry(6, 6, 10, 12),
                                        new THREE.MeshStandardMaterial({ color: 0xd8c27a, metalness: 0.8, roughness: 0.35 }), modules.length * 3);
  {
    let n = 0;
    for (const md of modules) {
      const e = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(md.sx, md.sy, md.sz)), modEdge);
      e.position.set(md.cx, md.cy, md.cz); scene.add(e);
      for (let k = -1; k <= 1; k++) {
        const qq = new THREE.Quaternion(); let x, y, z;
        if (md.kind === 'edge') { x = md.cx + k * 110; y = md.cy + md.sy / 2 + 5; z = md.cz; }
        else { x = md.cx + k * 70; y = md.cy; z = md.cz - md.side * (md.sz / 2 + 5); qq.setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2); }
        mat.compose(new THREE.Vector3(x, y, z), qq, new THREE.Vector3(k === 0 ? 0.6 : 1, 1, k === 0 ? 0.6 : 1));
        terms.setMatrixAt(n++, mat);
      }
    }
  }
  scene.add(terms);

  // hover: a thin white edge box round the pair; pinned: a glowing box round the whole
  // module, a bobbing pin above it and a label — strong enough to find from any angle
  hoverBox = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1)), new THREE.LineBasicMaterial({ color: 0xffffff }));
  hoverBox.visible = false; scene.add(hoverBox);
  selBox = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.3, depthWrite: false }));
  selBox.visible = false; scene.add(selBox);
  selPin = new THREE.Mesh(new THREE.ConeGeometry(16, 44, 16), new THREE.MeshBasicMaterial({ color: ACCENT }));
  selPin.rotation.x = Math.PI; selPin.visible = false; scene.add(selPin);

  // labels: one per pair (toggled), the car axes, the temperature sensors (each its own material)
  const mkLabel = (text, cls) => { const d = document.createElement('div'); d.className = 'pack3d-lbl' + (cls ? ' ' + cls : ''); d.textContent = text; return new CSS2DObject(d); };
  pairLabels = bodies.map(b => { const l = mkLabel('', ''); l.position.set(b.cx, b.cy + b.sy / 2, b.cz); l.visible = false; scene.add(l); return l; });
  selLabel = mkLabel('', 'pin'); selLabel.visible = false; scene.add(selLabel);
  for (const [txt, x, z] of [['front', C.L / 2 + 120, 0], ['rear', -C.L / 2 - 120, 0], ['driver side', 0, -C.W / 2 - 140], ['passenger side', 0, C.W / 2 + 140]]) {
    const l = mkLabel(txt, 'axis'); l.position.set(x, 8, z); scene.add(l);
  }
  {
    const g = new THREE.SphereGeometry(11, 16, 12);
    sensors = (PACK.sensors || []).map(s => {
      const m = new THREE.MeshStandardMaterial({ color: 0x8a94a8, emissive: 0x222222, roughness: 0.5 });
      const sp = new THREE.Mesh(g, m); sp.position.set(s.x, s.y, s.z); scene.add(sp);
      const l = mkLabel(s.n, 'sensor'); l.position.set(s.x, s.y + 16, s.z); scene.add(l);
      return { n: s.n, where: s.where || '', mesh: sp, label: l, f: null, c: null, css: null };
    });
  }

  // legend gradient from the shared colour function
  const bar = host.querySelector('.pack3d-legend i');
  if (bar) bar.style.background = `linear-gradient(90deg, ${[0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1].map(v => Tiles.cellColor(v, 0, 1)).join(',')})`;

  hookPointer(host);
  hookTools(host);
  ro = new ResizeObserver(resize); ro.observe(host);
  // scrolled out of view → no frames at all; the page's timers get the main thread back
  if (window.IntersectionObserver) new IntersectionObserver(es => { onScreen = es.some(e => e.isIntersecting); if (onScreen) needsRender = true; }).observe(host);
  resize(); applyView();
  state.built = true;
  loop();
}

// ── colours ───────────────────────────────────────────────────────────────
// the cell grid's colour function, as a CSS string (for text) and a THREE colour (for bodies)
function mode() { return MODES.find(m => m.id === state.opts.mode) || MODES[0]; }
function valuesOf(data) { const v = data && data[mode().key]; return Array.isArray(v) && v.length >= nValues ? v : null; }
function pairCss(val, f, i, sc) {
  const md = mode();
  if (state.opts.scale === 'abs' && !md.invert) return Tiles.cellColor(val, f.min, f.max);   // exactly the grid's call
  let t = sc.t(val, f, i, state.rest, md);
  if (md.invert) t = 1 - t;                                                                // temperatures: hot is red
  return Tiles.cellColor(t, 0, 1);
}
function cssColor(css) {
  let c = colorCache.get(css);
  if (!c) { c = new THREE.Color().setStyle(css); colorCache.set(css, c); }
  return c;
}
function setPairColor(i, c) { const s = slot[i]; groups[s.g].mesh.setColorAt(s.k, c); }
function flushColors() { for (const g of groups) g.mesh.instanceColor.needsUpdate = true; }

// ── painting ──────────────────────────────────────────────────────────────
function paint() {
  const data = state.last; if (!state.built || !data) return;
  const md = mode(), cells = valuesOf(data); if (!cells) return;
  const f = PackLayout.stats(cells), sc = PackLayout.SCALES[state.opts.scale] || PackLayout.SCALES.abs;
  for (const b of bodies) setPairColor(b.i, cssColor(pairCss(cells[b.v], f, b.v, sc)));
  flushColors();
  // what breathes (see loop): the lowest pair toward white and the highest toward blue,
  // plus every pair below / above the thresholds set in the ⋯ menu
  // flashing is per value; every body carrying that value breathes
  const flashing = [], below = +state.opts.flashBelow, above = +state.opts.flashAbove;
  const add = (v, to) => { for (const b of bodies) if (b.v === v && !flashing.some(x => x.i === b.i)) flashing.push({ i: b.i, to, base: cssColor(pairCss(cells[v], f, v, sc)).clone() }); };
  if (state.opts.flash) { add(f.imin, WHITE); add(f.imax, BLUE); }
  for (let v = 0; v < nValues; v++) {
    if (below > 0 && cells[v] < below) add(v, WHITE);
    if (above > 0 && cells[v] > above) add(v, BLUE);
  }
  state.flashing = flashing;
  state.thresholdNote = (below > 0 ? ` · ${cells.filter(v => v < below).length} below ${below} ${md.unit}` : '') +
                        (above > 0 ? ` · ${cells.filter(v => v > above).length} above ${above} ${md.unit}` : '');
  const lmode = state.opts.labels, labelled = new Set();
  for (const b of bodies) {
    const first = !labelled.has(b.v);                    // a shared value is labelled on its first body only
    const show = first && (lmode === 'all' || (lmode === 'minmax' && (b.v === f.imin || b.v === f.imax)) || b.i === state.pinned || b.i === state.hover);
    const l = pairLabels[b.i]; l.visible = show;
    if (show) { labelled.add(b.v); l.element.textContent = `${b.v + 1} · ${cells[b.v]}`; l.element.classList.toggle('hot', b.v === f.imin); l.element.classList.toggle('high', b.v === f.imax); }
  }
  const ends = state.host.querySelectorAll('.pack3d-legend span');
  if (ends.length === 2) { ends[0].textContent = sc.lo(f, md.unit, md); ends[1].textContent = sc.hi(f, md.unit, md); }
  paintSensors(data);
  readout(state.hover >= 0 ? state.hover : state.pinned, cells, f);
  paintSelection(cells, f, sc);
  needsRender = true;
}
// each sensor ball takes the colour of its own reading on the pack's own range
// (hottest red, coolest blue) and its label carries the value in °F and °C
function paintSensors(data) {
  if (!sensors.length) return;
  const tf = data.temps_f || (data.temps_c || data.temps || []).map(c => c == null ? null : c * 9 / 5 + 32);
  const vals = tf.filter(v => v != null);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  sensors.forEach((s, i) => {
    const v = tf[i];
    if (v == null) { s.label.element.textContent = s.n; s.mesh.material.color.set(0x8a94a8); return; }
    const t = hi - lo < 1 ? 0.5 : (v - lo) / (hi - lo);           // 1 = hottest
    const css = Tiles.cellColor(1 - t, 0, 1), c = cssColor(css);  // cellColor: 0 red … 1 blue
    s.mesh.material.color.copy(c); s.mesh.material.emissive.copy(c).multiplyScalar(0.3);
    s.f = v; s.c = (v - 32) * 5 / 9; s.css = css;
    s.label.element.textContent = `${s.n} ${v.toFixed(1)} °F · ${s.c.toFixed(1)} °C`;
    s.label.element.classList.toggle('hot', vals.length > 1 && v === hi);
  });
  state.tempMeanF = vals.length ? (state.last.temp_avg_f != null ? state.last.temp_avg_f : vals.reduce((a, b) => a + b, 0) / vals.length) : null;
}
function readout(i, cells, f) {
  const note = state.note; if (!note) return;
  const sj = state.hoverSensor >= 0 ? state.hoverSensor : (i < 0 ? state.pinnedSensor : -1);
  if (sj >= 0 && sensors[sj]) {
    const s = sensors[sj]; hoverBox.visible = false;
    note.innerHTML = s.f == null ? `sensor <b>${s.n}</b> · ${s.where} · no reading yet`
      : `sensor <b>${s.n}</b> · <b style="color:${s.css}">${s.f.toFixed(1)} °F</b> · ${s.c.toFixed(1)} °C` +
        (state.tempMeanF != null ? ` · ${sign(s.f - state.tempMeanF)} °F vs pack mean` : '') + ` · ${s.where}` + (state.pinnedSensor === sj ? ' · pinned' : '');
    return;
  }
  if (i < 0 || !bodies[i]) {
    hoverBox.visible = false;
    const md = mode();
    note.innerHTML = `spread <b>${(f.max - f.min).toFixed(0)} ${md.unit}</b> · mean <b>${f.mean.toFixed(0)} ${md.unit}</b> · lowest ${md.name} <b>${f.imin + 1}</b> · highest <b>${f.imax + 1}</b>${state.thresholdNote || ''} · hover a ${md.name}, click to pin`;
    return;
  }
  const md = mode(), b = bodies[i], v = b.v, dev = cells[v] - f.mean, drop = state.rest ? cells[v] - state.rest[v] : null;
  note.innerHTML = `${md.name} <b>${v + 1}</b> · <b>${cells[v]} ${md.unit}</b> · ${sign(dev)} ${md.unit} vs mean` +
    (drop == null || !md.scales.includes('drop') ? '' : ` · ${sign(drop)} ${md.unit} from rest`) + ` · module ${b.m + 1} of ${modules.length} · ${b.loc}` +
    (b.verify ? ` <span class="verify" title="${b.verify}">(stack order assumed)</span>` : '') +
    (state.pinned === i ? ' · pinned' : '');
  hoverBox.visible = state.hover === i;
  hoverBox.position.set(b.cx, b.cy, b.cz); hoverBox.scale.set(b.sx + 4, b.sy + 4, b.sz + 4);
}
const sign = (v, d = 0) => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(d);
const ordinal = n => n + (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][Math.min(n % 10, 4) % 4] || 'th');
// the pinned module: the marker in the scene and the side pane with both of its pairs
function paintSelection(cells, f, sc) {
  const i = state.pinned, sj = state.pinnedSensor;
  if (sj >= 0 && sensors[sj]) {
    const s = sensors[sj], pos = s.mesh.position;
    selBox.visible = selPin.visible = selLabel.visible = true;
    selBox.position.copy(pos); selBox.scale.set(44, 44, 44);
    selPin.position.set(pos.x, pos.y + 70, pos.z); selLabel.position.set(pos.x, pos.y + 106, pos.z);
    selLabel.element.textContent = s.f == null ? `sensor ${s.n}` : `sensor ${s.n} · ${s.f.toFixed(1)} °F`;
    paintSensorPane(sj); return;
  }
  if (i < 0 || !bodies[i]) { selBox.visible = selPin.visible = selLabel.visible = false; state.pane.hidden = true; return; }
  const b = bodies[i], md = modules[b.m];
  selBox.visible = selPin.visible = selLabel.visible = true;
  selBox.position.set(md.cx, md.cy, md.cz); selBox.scale.set(md.sx + 10, md.sy + 10, md.sz + 10);
  selPin.position.set(md.cx, md.cy + md.sy / 2 + 60, md.cz);
  selLabel.position.set(md.cx, md.cy + md.sy / 2 + 96, md.cz);
  const vs = valuesOfModule(md.m);
  selLabel.element.textContent = `module ${b.m + 1} · ${mode().name} ${vs.map(v => v + 1).join(' & ')}`;
  paintPane(i, cells, f, sc);
}
// the measured values a module carries (its slices), or the value it shares with its group
function valuesOfModule(m) { return [...new Set(bodies.filter(b => b.m === m).map(b => b.v))]; }
function paintPane(i, cells, f, sc) {
  const pane = state.pane; if (!pane) return;
  const md = mode(), b = bodies[i], vs = valuesOfModule(b.m), u = md.unit;
  const order = cells.map((v, k) => [v, k]).sort((a, c) => a[0] - c[0]).map(x => x[1]);
  const row = v => {
    const val = cells[v], dev = val - f.mean, drop = state.rest ? val - state.rest[v] : null;
    const rank = order.indexOf(v) + 1, bal = md.key === 'cells' && state.last.balancing && state.last.balancing[v];
    const css = pairCss(val, f, v, sc);                           // the value's own colour, as the grid paints it
    const sharedBy = b.shared ? modules.filter(x => valuesOfModule(x.m).includes(v)).map(x => x.m + 1) : null;
    return `<div class="pack3d-pane-pair ${v === b.v ? 'on' : ''}" style="border-left-color:${css}">
      <div class="k">${md.name} ${v + 1} <small>${sharedBy ? 'modules ' + sharedBy.join('–') : 'module ' + (b.m + 1)}</small></div>
      <div class="v" style="color:${css}">${val}<small>${u}</small></div>
      <div class="rows"><span>vs mean</span><b>${sign(dev)} ${u}</b>
        ${md.scales.includes('drop') ? `<span>from rest</span><b>${drop == null ? '—' : sign(drop) + ' ' + u}</b>` : ''}
        <span>rank</span><b>${ordinal(rank)} lowest${rank === 1 ? ' ⚑' : rank === cells.length ? ' ▲' : ''}</b>
        ${md.key === 'cells' ? `<span>balancing</span><b>${bal ? 'yes' : '—'}</b>` : ''}</div></div>`;
  };
  // the module as a whole, when it carries more than one value: spread and average, ranked among the modules
  let modHtml = '';
  if (vs.length > 1) {
    const mv = m => valuesOfModule(m).map(v => cells[v]);
    const avg = vs.reduce((a, v) => a + cells[v], 0) / vs.length, spread = Math.max(...vs.map(v => cells[v])) - Math.min(...vs.map(v => cells[v]));
    const modAvgs = modules.map(x => { const a = mv(x.m); return a.reduce((p, q) => p + q, 0) / a.length; });
    const modRank = modAvgs.map((v, k) => [v, k]).sort((a, c) => a[0] - c[0]).findIndex(x => x[1] === b.m) + 1;
    const modSpreads = modules.map(x => { const a = mv(x.m); return Math.max(...a) - Math.min(...a); });
    const spreadRank = modSpreads.map((v, k) => [v, k]).sort((a, c) => c[0] - a[0]).findIndex(x => x[1] === b.m) + 1;
    modHtml = `<div class="pack3d-pane-mod"><div class="k">module ${b.m + 1} — all ${vs.length} ${md.name}s</div>
      <div class="two"><div><div class="k">spread</div><div class="v">${spread}<small>${u}</small></div></div>
        <div><div class="k">average</div><div class="v" style="color:${pairCss(avg, f, b.v, sc)}">${avg.toFixed(0)}<small>${u}</small></div></div></div>
      <div class="rows"><span>average vs pack</span><b>${sign(avg - f.mean)} ${u}</b>
        <span>average rank</span><b>${ordinal(modRank)} lowest of ${modules.length}</b>
        <span>spread rank</span><b>${ordinal(spreadRank)} widest of ${modules.length}</b></div></div>`;
  }
  pane.innerHTML = `<div class="pack3d-pane-head"><b>Module ${b.m + 1} of ${modules.length}</b><span>${b.loc}</span>
      <button class="pack3d-pane-close" title="unpin">×</button></div>
    ${vs.map(row).join('')}${modHtml}
    <div class="pack3d-pane-foot">pack ${f.min}–${f.max} ${u} · spread ${(f.max - f.min).toFixed(0)} · mean ${f.mean.toFixed(0)}` +
    (b.verify ? ` · <span class="verify" title="${b.verify}">stack order assumed</span>` : '') + `</div>`;
  pane.querySelector('.pack3d-pane-close').addEventListener('click', () => { state.pinned = -1; paint(); });
  pane.hidden = false;
}
// a pinned temperature sensor: its reading, large and in its colour, against the other three
function paintSensorPane(sj) {
  const pane = state.pane, s = sensors[sj]; if (!pane || !s) return;
  const withF = sensors.filter(x => x.f != null), order = withF.slice().sort((a, b) => b.f - a.f);
  const rank = order.indexOf(s) + 1, mean = state.tempMeanF;
  const spreadF = withF.length > 1 ? Math.max(...withF.map(x => x.f)) - Math.min(...withF.map(x => x.f)) : 0;
  const main = s.f == null ? `<div class="pack3d-pane-pair on"><div class="k">no reading yet</div></div>`
    : `<div class="pack3d-pane-pair on" style="border-left-color:${s.css}">
        <div class="k">reading</div>
        <div class="v" style="color:${s.css}">${s.f.toFixed(1)}<small>°F</small></div>
        <div class="rows"><span>celsius</span><b>${s.c.toFixed(1)} °C</b>
          <span>vs pack mean</span><b>${mean == null ? '—' : sign(s.f - mean, 1) + ' °F'}</b>
          <span>rank</span><b>${rank === 1 ? 'hottest' : rank === withF.length ? 'coolest' : ordinal(rank) + ' hottest'} of ${withF.length}</b></div></div>`;
  const others = `<div class="pack3d-pane-mod"><div class="k">all four sensors</div><div class="rows">` +
    sensors.map(x => `<span>${x.n} <small>${x.where}</small></span><b style="color:${x.css || 'inherit'}">${x.f == null ? '—' : x.f.toFixed(1) + ' °F · ' + x.c.toFixed(1) + ' °C'}</b>`).join('') + `</div></div>`;
  pane.innerHTML = `<div class="pack3d-pane-head"><b>Sensor ${s.n}</b><span>${s.where}</span>
      <button class="pack3d-pane-close" title="unpin">×</button></div>${main}${others}
    <div class="pack3d-pane-foot">pack mean ${mean == null ? '—' : mean.toFixed(1) + ' °F · ' + ((mean - 32) * 5 / 9).toFixed(1) + ' °C'} · spread ${spreadF.toFixed(1)} °F</div>`;
  pane.querySelector('.pack3d-pane-close').addEventListener('click', () => { state.pinnedSensor = -1; paint(); });
  pane.hidden = false;
}

// ── interaction ──────────────────────────────────────────────────────────
function hookPointer(host) {
  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
  // returns {pair: i} or {sensor: j} or null — the nearest of the bodies and the sensor balls
  const pick = e => {
    const r = renderer.domElement.getBoundingClientRect();
    ptr.set((e.clientX - r.left) / r.width * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, camera);
    const h = ray.intersectObjects([...groups.map(g => g.mesh), ...sensors.map(s => s.mesh)], false);
    if (!h.length) return null;
    const sj = sensors.findIndex(s => s.mesh === h[0].object);
    if (sj >= 0) return { sensor: sj };
    const g = groups.find(x => x.mesh === h[0].object);
    return g ? { pair: g.ids[h[0].instanceId] } : null;
  };
  renderer.domElement.addEventListener('pointermove', e => {
    const p = pick(e), i = p && p.pair != null ? p.pair : -1, sj = p && p.sensor != null ? p.sensor : -1;
    if (i !== state.hover || sj !== state.hoverSensor) { state.hover = i; state.hoverSensor = sj; paint(); }
  });
  renderer.domElement.addEventListener('pointerleave', () => { state.hover = -1; state.hoverSensor = -1; paint(); });
  renderer.domElement.addEventListener('click', e => {
    const p = pick(e); if (!p) return;
    if (p.sensor != null) { state.pinnedSensor = (p.sensor === state.pinnedSensor) ? -1 : p.sensor; state.pinned = -1; }
    else { state.pinned = (p.pair === state.pinned) ? -1 : p.pair; state.pinnedSensor = -1; }
    paint();
  });
}
// the corner tools: auto-rotate, expand to double height (a real gridstack resize,
// persisted), and help
function hookTools(host) {
  const spin = host.querySelector('.pack3d-spin');
  if (spin) {
    spin.classList.toggle('on', !!state.opts.spin);
    spin.addEventListener('click', () => {
      const on = !state.opts.spin;
      state.opts.spin = on; controls.autoRotate = on; spin.classList.toggle('on', on);
      if (window.TileStudio && TileStudio.setOpt) TileStudio.setOpt('pack3d', 'spin', on);
    });
  }
  const ex = host.querySelector('.pack3d-expand');
  if (ex) ex.addEventListener('click', () => {
    if (!window.TileStudio || !TileStudio.size) return;
    const t = TileStudio.tile('pack3d') || {};
    if (!state.expanded) { state.baseH = t.h || 12; TileStudio.size('pack3d', null, state.baseH * 2); }
    else TileStudio.size('pack3d', null, state.baseH || 12);
    state.expanded = !state.expanded;
    ex.classList.toggle('on', state.expanded);
    ex.title = state.expanded ? 'back to the normal height' : 'double the height';
  });
}
function applyView() {
  if (!camera) return;
  const v = VIEWS[state.opts.view] || VIEWS.iso;
  camera.position.set(v[0], v[1], v[2]); controls.target.set(0, 80, 0);
  controls.autoRotate = !!state.opts.spin;
  const spin = state.host && state.host.querySelector('.pack3d-spin'); if (spin) spin.classList.toggle('on', !!state.opts.spin);
}
function resize() {
  const host = state.host; if (!host || !renderer) return;
  const w = host.clientWidth, h = host.clientHeight; if (!w || !h) return;
  renderer.setSize(w, h); labelRenderer.setSize(w, h);
  camera.aspect = w / h; camera.updateProjectionMatrix();
  needsRender = true;
}
// Render on demand, not sixty times a second: a frame is drawn when something changed
// (a new record, a hover, a resize, the camera moving or damping, auto-rotate), and the
// breathing of flashed values and the pinned marker tick at 20 fps. Nothing is drawn
// while the tile is scrolled out of view or the tab is hidden. The page's own timers —
// the 1 s poll, the alert repeats — keep their beat that way.
function loop() {
  requestAnimationFrame(loop);
  if (!state.built || document.hidden || !onScreen || !state.host.clientWidth) return;
  const nowMs = performance.now(), now = nowMs / 1000;
  let draw = needsRender || controls.update();            // update() is true while the camera moves
  if ((state.flashing.length || selBox.visible) && nowMs - lastTick >= 50) {
    lastTick = nowMs;
    // the lowest value breathes toward white and the highest toward blue, so both can be
    // found at a glance; the pinned marker's box pulses and its pin bobs
    const p = 0.5 + 0.5 * Math.sin(now * 2 * Math.PI * 1.2);
    let dirty = false;
    for (const x of state.flashing) { setPairColor(x.i, tmpColor.copy(x.base).lerp(x.to, 0.65 * p)); dirty = true; }
    if (dirty) flushColors();
    if (selBox.visible) { selBox.material.opacity = 0.18 + 0.22 * p; selPin.position.y += Math.sin(now * 2 * Math.PI * 0.8) * 0.6; }
    draw = true;
  }
  if (!draw) return;
  needsRender = false;
  renderer.render(scene, camera); labelRenderer.render(scene, camera);
}

// ── public surface ───────────────────────────────────────────────────────
export function render(root, data) {
  if (!data) return;
  if (!state.built) build(root);
  if (!state.built) { window.__pack3dPending = data; return; }
  const vals = valuesOf(data); if (!vals) return;
  // rest values: the first frame seen, again whenever a playback window starts
  const pb = !!data.playback;
  if (!state.rest || pb !== state.playback || data.playback_first) state.rest = vals.slice();
  state.playback = pb;
  state.last = data; paint();
}
export function setOpts(o) {
  const before = state.opts.view;
  const beforeMode = state.opts.mode;
  Object.assign(state.opts, DEFAULT_OPTS, o || {});
  if (state.opts.mode !== beforeMode) state.rest = null;
  if (!mode().scales.includes(state.opts.scale)) state.opts.scale = mode().scales[0];
  state.opts.case = PackLayout.clamp(+state.opts.case, 0, 0.6);
  if (caseMat) caseMat.opacity = state.opts.case;
  if (controls) controls.autoRotate = !!state.opts.spin;
  const spin = state.host && state.host.querySelector('.pack3d-spin'); if (spin) spin.classList.toggle('on', !!state.opts.spin);
  if (state.opts.view !== before) applyView();
  if (state.last) paint();
}
export function dispose() {
  if (ro) ro.disconnect();
  if (renderer) renderer.dispose();
  state.built = false;
}

window.Pack3D = { render, setOpts, dispose };

// ⋯ menu controls for this tile, persisted in its opts through Tile Studio
// (auto-rotate lives on the pane itself, not here)
if (window.TileStudio && TileStudio.menuExtra) {
  TileStudio.menuExtra('pack3d', (box, o, commit) => {
    const sel = (key, entries) => `<select data-k="${key}">${entries.map(([v, l]) => `<option value="${v}" ${(o[key] ?? DEFAULT_OPTS[key]) == v ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
    const on = key => (o[key] ?? DEFAULT_OPTS[key]) ? 'checked' : '';
    const md = mode();
    box.innerHTML = `<h5>3D pack</h5>
      ${MODES.length > 1 ? `<div class="row"><label>Show</label>${sel('mode', MODES.map(m => [m.id, `${m.name}s (${m.unit})`]))}</div>` : ''}
      <div class="row"><label>Colour by</label>${sel('scale', md.scales.map(k => [k, PackLayout.SCALES[k].label]))}</div>
      <div class="row"><label>Values</label>${sel('labels', [['minmax', 'lowest and highest pair'], ['hover', 'hover only'], ['all', 'every pair']])}</div>
      <div class="row"><label>Case</label><input type="range" data-k="case" min="0" max="60" value="${Math.round((o.case ?? DEFAULT_OPTS.case) * 100)}"> <span style="color:var(--dim)">opacity</span></div>
      <div class="row seg">${Object.keys(VIEWS).map(v => `<button data-view="${v}" class="${(o.view || DEFAULT_OPTS.view) === v ? 'on' : ''}">${v}</button>`).join('')}<span style="color:var(--dim)">view</span></div>
      <div class="row"><label style="min-width:0"><input type="checkbox" data-k="flash" ${on('flash')}> flash the lowest ${md.name} white and the highest blue</label></div>
      <div class="row"><label>Flash all below</label><input type="number" data-k="flashBelow" min="0" max="5000" step="1" value="${o.flashBelow ?? ''}" placeholder="${md.unit}" style="width:78px">
        <label style="min-width:0">above</label><input type="number" data-k="flashAbove" min="0" max="5000" step="1" value="${o.flashAbove ?? ''}" placeholder="${md.unit}" style="width:78px"></div>
      <div style="color:var(--dim);font-size:.75em;margin:-2px 0 6px">Every pair under the first value breathes white, every pair over the second breathes blue. Leave blank for none.</div>`;
    box.querySelectorAll('select[data-k]').forEach(s => s.addEventListener('change', () => { o[s.dataset.k] = s.value; commit(); }));
    box.querySelector('input[data-k="case"]').addEventListener('input', e => { o.case = +e.target.value / 100; setOpts(o); });
    box.querySelector('input[data-k="case"]').addEventListener('change', commit);
    box.querySelectorAll('input[type="checkbox"][data-k]').forEach(c => c.addEventListener('change', e => { o[c.dataset.k] = e.target.checked; commit(); }));
    box.querySelectorAll('input[type="number"][data-k]').forEach(n => n.addEventListener('change', e => { o[n.dataset.k] = e.target.value === '' ? '' : +e.target.value; commit(); }));
    box.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => {
      box.querySelectorAll('[data-view]').forEach(x => x.classList.toggle('on', x === b)); o.view = b.dataset.view; commit();
    }));
    if (window.cellLogMenu) cellLogMenu(box, o, commit);      // the same rows the cell grid offers
  });
}
// re-read opts (and build, if the tile just became visible) after every layout change
document.addEventListener('tiles:applied', () => {
  if (window.TileStudio) setOpts(TileStudio.opts('pack3d'));
  if (!state.built && window.__pack3dPending) render(document, window.__pack3dPending);
  else if (state.built) resize();
});
if (window.TileStudio && TileStudio.opts) setOpts(TileStudio.opts('pack3d'));
if (window.__pack3dPending) { const d = window.__pack3dPending; window.__pack3dPending = null; render(document, d); }
