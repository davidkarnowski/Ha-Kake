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
// Geometry: 48 modules as boxes from the profile's PACK_LAYOUT, each split into
// two half-slabs (one per measured cell pair) in a single InstancedMesh whose
// per-instance colour is the pair's voltage on the chosen scale. A translucent
// case, module outlines, terminal studs and the four temperature sensors (each
// coloured by its own reading) give it the shape of the real pack. Labels are
// DOM elements tracked by CSS2DRenderer, so they use the dashboard's own fonts.
// Clicking a pair pins it and opens a side pane with both pairs of its module.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const VIEWS = { iso: [1500, 1300, 1700], top: [1, 2600, 1], rear: [-2100, 700, 0], driver: [200, 650, -2300] };
// scale 'abs' is the cell grid's own colouring, so a pair reads the same colour side by side
const DEFAULT_OPTS = { scale: 'abs', labels: 'minmax', case: 0.14, view: 'iso', spin: false, flash: true };
const WHITE = new THREE.Color(0xffffff);

const state = {
  built: false, opts: Object.assign({}, DEFAULT_OPTS), rest: null, playback: false,
  hover: -1, pinned: -1, last: null, host: null, note: null, pane: null,
  flashIdx: -1, flashBase: new THREE.Color(), expanded: false, baseH: null,
};
let renderer, labelRenderer, scene, camera, controls, slabs, caseMat, hiBox, pairLabels, bodies, ro, sensors = [];
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
  const PACK = pack, M = PACK.module, C = PACK.case;
  ({ bodies } = PackLayout.bodies(PACK));
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

  // 96 half-slabs, one instanced mesh, per-instance colour; matte so the colour is the colour
  slabs = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1),
                                  new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 1, metalness: 0 }), bodies.length);
  {
    const mat = new THREE.Matrix4(), q = new THREE.Quaternion(), grey = new THREE.Color(0x6b7a99);
    for (const b of bodies) {
      mat.compose(new THREE.Vector3(b.cx, b.cy, b.cz), q, new THREE.Vector3(b.sx, b.sy, b.sz));
      slabs.setMatrixAt(b.i, mat); slabs.setColorAt(b.i, grey);
    }
  }
  scene.add(slabs);

  // module outlines and terminal studs (terminals up on the rear block, inboard on the flat stacks)
  const { modules } = PackLayout.bodies(PACK);
  const modEdge = new THREE.LineBasicMaterial({ color: 0x0a0e17, transparent: true, opacity: 0.55 });
  const terms = new THREE.InstancedMesh(new THREE.CylinderGeometry(6, 6, 10, 12),
                                        new THREE.MeshStandardMaterial({ color: 0xd8c27a, metalness: 0.8, roughness: 0.35 }), modules.length * 3);
  {
    const mat = new THREE.Matrix4(); let n = 0;
    for (const md of modules) {
      const e = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(md.sx, md.sy, md.sz)), modEdge);
      e.position.set(md.cx, md.cy, md.cz); scene.add(e);
      for (let k = -1; k <= 1; k++) {
        const q = new THREE.Quaternion(); let x, y, z;
        if (md.kind === 'edge') { x = md.cx + k * 110; y = md.cy + md.sy / 2 + 5; z = md.cz; }
        else { x = md.cx + k * 70; y = md.cy; z = md.cz - md.side * (md.sz / 2 + 5); q.setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2); }
        mat.compose(new THREE.Vector3(x, y, z), q, new THREE.Vector3(k === 0 ? 0.6 : 1, 1, k === 0 ? 0.6 : 1));
        terms.setMatrixAt(n++, mat);
      }
    }
  }
  scene.add(terms);

  // hover / pin highlight
  hiBox = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1)), new THREE.LineBasicMaterial({ color: 0xffffff }));
  hiBox.visible = false; scene.add(hiBox);

  // labels: one per pair (toggled), the car axes, the temperature sensors (each its own material)
  const mkLabel = (text, cls) => { const d = document.createElement('div'); d.className = 'pack3d-lbl' + (cls ? ' ' + cls : ''); d.textContent = text; return new CSS2DObject(d); };
  pairLabels = bodies.map(b => { const l = mkLabel('', ''); l.position.set(b.cx, b.cy + b.sy / 2, b.cz); l.visible = false; scene.add(l); return l; });
  for (const [txt, x, z] of [['front', C.L / 2 + 120, 0], ['rear', -C.L / 2 - 120, 0], ['driver side', 0, -C.W / 2 - 140], ['passenger side', 0, C.W / 2 + 140]]) {
    const l = mkLabel(txt, 'axis'); l.position.set(x, 8, z); scene.add(l);
  }
  {
    const g = new THREE.SphereGeometry(11, 16, 12);
    sensors = (PACK.sensors || []).map(s => {
      const m = new THREE.MeshStandardMaterial({ color: 0x8a94a8, emissive: 0x222222, roughness: 0.5 });
      const sp = new THREE.Mesh(g, m); sp.position.set(s.x, s.y, s.z); scene.add(sp);
      const l = mkLabel(s.n, 'sensor'); l.position.set(s.x, s.y + 16, s.z); scene.add(l);
      return { n: s.n, mesh: sp, label: l };
    });
  }

  // legend gradient from the shared colour function
  const bar = host.querySelector('.pack3d-legend i');
  if (bar) bar.style.background = `linear-gradient(90deg, ${[0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1].map(v => Tiles.cellColor(v, 0, 1)).join(',')})`;

  hookPointer(host);
  hookTools(host);
  ro = new ResizeObserver(resize); ro.observe(host);
  resize(); applyView();
  state.built = true;
  loop();
}

// ── painting ──────────────────────────────────────────────────────────────
// the cell grid's colour function, cached by its CSS string so both panels agree exactly
function cssColor(css) {
  let c = colorCache.get(css);
  if (!c) { c = new THREE.Color().setStyle(css); colorCache.set(css, c); }
  return c;
}
function pairColor(mv, f, i, sc) {
  return state.opts.scale === 'abs' ? cssColor(Tiles.cellColor(mv, f.min, f.max))
                                    : cssColor(Tiles.cellColor(sc.t(mv, f, i, state.rest), 0, 1));
}
function paint() {
  const data = state.last; if (!state.built || !data) return;
  const cells = data.cells, f = PackLayout.stats(cells), sc = PackLayout.SCALES[state.opts.scale] || PackLayout.SCALES.abs;
  for (let i = 0; i < bodies.length; i++) slabs.setColorAt(i, pairColor(cells[i], f, i, sc));
  slabs.instanceColor.needsUpdate = true;
  state.flashIdx = state.opts.flash ? f.imin : -1;
  if (state.flashIdx >= 0) state.flashBase.copy(pairColor(cells[f.imin], f, f.imin, sc));
  const mode = state.opts.labels;
  for (let i = 0; i < bodies.length; i++) {
    const show = mode === 'all' || (mode === 'minmax' && (i === f.imin || i === f.imax)) || i === state.pinned || i === state.hover;
    const l = pairLabels[i]; l.visible = show;
    if (show) { l.element.textContent = `${i} · ${cells[i]}`; l.element.classList.toggle('hot', i === f.imin); }
  }
  const ends = state.host.querySelectorAll('.pack3d-legend span');
  if (ends.length === 2) { ends[0].textContent = sc.lo(f); ends[1].textContent = sc.hi(f); }
  paintSensors(data);
  readout(state.hover >= 0 ? state.hover : state.pinned, cells, f);
  if (state.pinned >= 0) paintPane(state.pinned, cells, f); else state.pane.hidden = true;
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
    const c = cssColor(Tiles.cellColor(1 - t, 0, 1));            // cellColor: 0 red … 1 blue
    s.mesh.material.color.copy(c); s.mesh.material.emissive.copy(c).multiplyScalar(0.3);
    s.label.element.textContent = `${s.n} ${v.toFixed(1)} °F · ${((v - 32) * 5 / 9).toFixed(1)} °C`;
    s.label.element.classList.toggle('hot', vals.length > 1 && v === hi);
  });
}
function readout(i, cells, f) {
  const note = state.note; if (!note) return;
  if (i < 0 || !bodies[i]) {
    hiBox.visible = false;
    note.innerHTML = `spread <b>${(f.max - f.min).toFixed(0)} mV</b> · mean <b>${f.mean.toFixed(0)} mV</b> · lowest pair <b>${f.imin}</b> · highest <b>${f.imax}</b> · hover a pair, click to pin`;
    return;
  }
  const b = bodies[i], dev = cells[i] - f.mean, drop = state.rest ? cells[i] - state.rest[i] : null;
  note.innerHTML = `pair <b>${i}</b> (LeafSpy ${i + 1}) · <b>${cells[i]} mV</b> · ${sign(dev)} mV vs mean` +
    (drop == null ? '' : ` · ${sign(drop)} mV from rest`) + ` · module ${b.m + 1} of ${bodies.length / 2} · ${b.loc}` +
    (b.verify ? ` <span class="verify" title="${b.verify}">(stack order assumed)</span>` : '') +
    (state.pinned === i ? ' · pinned' : '');
  hiBox.visible = true; hiBox.position.set(b.cx, b.cy, b.cz); hiBox.scale.set(b.sx + 4, b.sy + 4, b.sz + 4);
}
const sign = v => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(0);
const ordinal = n => n + (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][Math.min(n % 10, 4) % 4] || 'th');
// the side pane: both pairs of the pinned pair's module, larger, with rank in the pack
function paintPane(i, cells, f) {
  const pane = state.pane; if (!pane) return;
  const b = bodies[i], sib = bodies[b.m * 2] === b ? bodies[b.m * 2 + 1] : bodies[b.m * 2];
  const order = cells.map((v, k) => [v, k]).sort((a, c) => a[0] - c[0]).map(x => x[1]);
  const pair = p => {
    const v = cells[p.i], dev = v - f.mean, drop = state.rest ? v - state.rest[p.i] : null;
    const rank = order.indexOf(p.i) + 1, bal = state.last.balancing && state.last.balancing[p.i];
    return `<div class="pack3d-pane-pair ${p.i === i ? 'on' : ''}">
      <div class="k">pair ${p.i} <small>LeafSpy ${p.i + 1}</small></div>
      <div class="v">${v}<small>mV</small></div>
      <div class="rows"><span>vs mean</span><b>${sign(dev)} mV</b>
        <span>from rest</span><b>${drop == null ? '—' : sign(drop) + ' mV'}</b>
        <span>rank</span><b>${ordinal(rank)} lowest${rank === 1 ? ' ⚑' : ''}</b>
        <span>balancing</span><b>${bal ? 'yes' : '—'}</b></div></div>`;
  };
  pane.innerHTML = `<div class="pack3d-pane-head"><b>Module ${b.m + 1} of ${bodies.length / 2}</b><span>${b.loc}</span>
      <button class="pack3d-pane-close" title="unpin">×</button></div>
    ${pair(b)}${pair(sib)}
    <div class="pack3d-pane-foot">pack ${f.min}–${f.max} mV · spread ${(f.max - f.min).toFixed(0)} · mean ${f.mean.toFixed(0)}` +
    (b.verify ? ` · <span class="verify" title="${b.verify}">stack order assumed</span>` : '') + `</div>`;
  pane.querySelector('.pack3d-pane-close').addEventListener('click', () => { state.pinned = -1; paint(); });
  pane.hidden = false;
}

// ── interaction ──────────────────────────────────────────────────────────
function hookPointer(host) {
  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
  const pick = e => {
    const r = renderer.domElement.getBoundingClientRect();
    ptr.set((e.clientX - r.left) / r.width * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, camera);
    const h = ray.intersectObject(slabs, false);
    return h.length ? h[0].instanceId : -1;
  };
  renderer.domElement.addEventListener('pointermove', e => { const i = pick(e); if (i !== state.hover) { state.hover = i; paint(); } });
  renderer.domElement.addEventListener('pointerleave', () => { state.hover = -1; paint(); });
  renderer.domElement.addEventListener('click', e => { const i = pick(e); if (i < 0) return; state.pinned = (i === state.pinned) ? -1 : i; paint(); });
}
// the corner tools: expand to double height (a real gridstack resize, persisted), and help
function hookTools(host) {
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
}
function resize() {
  const host = state.host; if (!host || !renderer) return;
  const w = host.clientWidth, h = host.clientHeight; if (!w || !h) return;
  renderer.setSize(w, h); labelRenderer.setSize(w, h);
  camera.aspect = w / h; camera.updateProjectionMatrix();
}
function loop() {
  requestAnimationFrame(loop);
  if (!state.built || document.hidden || !state.host.clientWidth) return;
  // the lowest pair breathes toward white so it can be found at a glance
  if (state.flashIdx >= 0) {
    const p = 0.5 + 0.5 * Math.sin(performance.now() / 1000 * 2 * Math.PI * 1.2);
    slabs.setColorAt(state.flashIdx, tmpColor.copy(state.flashBase).lerp(WHITE, 0.6 * p));
    slabs.instanceColor.needsUpdate = true;
  }
  controls.update(); renderer.render(scene, camera); labelRenderer.render(scene, camera);
}

// ── public surface ───────────────────────────────────────────────────────
export function render(root, data) {
  if (!data) return;
  if (!state.built) build(root);
  if (!state.built) { window.__pack3dPending = data; return; }
  if (!data.cells || data.cells.length !== bodies.length) return;
  // rest voltages: the first frame seen, again whenever a playback window starts
  const pb = !!data.playback;
  if (!state.rest || pb !== state.playback || data.playback_first) state.rest = data.cells.slice();
  state.playback = pb;
  state.last = data; paint();
}
export function setOpts(o) {
  const before = state.opts.view;
  Object.assign(state.opts, DEFAULT_OPTS, o || {});
  state.opts.case = PackLayout.clamp(+state.opts.case, 0, 0.6);
  if (caseMat) caseMat.opacity = state.opts.case;
  if (controls) controls.autoRotate = !!state.opts.spin;
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
if (window.TileStudio && TileStudio.menuExtra) {
  TileStudio.menuExtra('pack3d', (box, o, commit) => {
    const sel = (key, entries) => `<select data-k="${key}">${entries.map(([v, l]) => `<option value="${v}" ${(o[key] ?? DEFAULT_OPTS[key]) == v ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
    const on = key => (o[key] ?? DEFAULT_OPTS[key]) ? 'checked' : '';
    box.innerHTML = `<h5>3D pack</h5>
      <div class="row"><label>Colour by</label>${sel('scale', Object.entries(PackLayout.SCALES).map(([k, s]) => [k, s.label]))}</div>
      <div class="row"><label>Values</label>${sel('labels', [['minmax', 'lowest and highest pair'], ['hover', 'hover only'], ['all', 'every pair']])}</div>
      <div class="row"><label>Case</label><input type="range" data-k="case" min="0" max="60" value="${Math.round((o.case ?? DEFAULT_OPTS.case) * 100)}"> <span style="color:var(--dim)">opacity</span></div>
      <div class="row seg">${Object.keys(VIEWS).map(v => `<button data-view="${v}" class="${(o.view || DEFAULT_OPTS.view) === v ? 'on' : ''}">${v}</button>`).join('')}<span style="color:var(--dim)">view</span></div>
      <div class="row"><label style="min-width:0"><input type="checkbox" data-k="flash" ${on('flash')}> flash the lowest pair</label></div>
      <div class="row"><label style="min-width:0"><input type="checkbox" data-k="spin" ${on('spin')}> auto-rotate</label></div>`;
    box.querySelectorAll('select[data-k]').forEach(s => s.addEventListener('change', () => { o[s.dataset.k] = s.value; commit(); }));
    box.querySelector('input[data-k="case"]').addEventListener('input', e => { o.case = +e.target.value / 100; setOpts(o); });
    box.querySelector('input[data-k="case"]').addEventListener('change', commit);
    box.querySelectorAll('input[type="checkbox"][data-k]').forEach(c => c.addEventListener('change', e => { o[c.dataset.k] = e.target.checked; commit(); }));
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
