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
// case, module outlines, terminal studs and the four temperature sensors give it
// the shape of the real pack. Labels are DOM elements tracked by CSS2DRenderer,
// so they use the dashboard's own fonts.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const VIEWS = { iso: [1500, 1300, 1700], top: [1, 2600, 1], rear: [-2100, 700, 0], driver: [200, 650, -2300] };
const DEFAULT_OPTS = { scale: 'dev', labels: 'minmax', case: 0.14, view: 'iso', spin: false };

const state = {
  built: false, opts: Object.assign({}, DEFAULT_OPTS), rest: null, playback: false,
  hover: -1, pinned: -1, last: null, host: null, note: null,
};
let renderer, labelRenderer, scene, camera, controls, slabs, caseMat, hiBox, pairLabels, bodies, ro;
const colorCache = new Map();

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
  state.host = host; state.note = root.querySelector('#pack3d-note');

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  host.prepend(renderer.domElement);
  labelRenderer = new CSS2DRenderer({ element: host.querySelector('.pack3d-labels') });
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(38, 1, 10, 20000);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08; controls.target.set(0, 80, 0);
  controls.autoRotateSpeed = 0.6;

  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x1a2234, 1.1));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(900, 1400, 700); scene.add(sun);
  const fill = new THREE.DirectionalLight(0x9fc8ff, 0.5); fill.position.set(-1200, 500, -900); scene.add(fill);

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

  // 96 half-slabs, one instanced mesh, per-instance colour
  slabs = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1),
                                  new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.6, metalness: 0.05 }), bodies.length);
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

  // labels: one per pair (toggled), the car axes, the temperature sensors
  const mkLabel = (text, cls) => { const d = document.createElement('div'); d.className = 'pack3d-lbl' + (cls ? ' ' + cls : ''); d.textContent = text; return new CSS2DObject(d); };
  pairLabels = bodies.map(b => { const l = mkLabel('', ''); l.position.set(b.cx, b.cy + b.sy / 2, b.cz); l.visible = false; scene.add(l); return l; });
  for (const [txt, x, z] of [['front', C.L / 2 + 120, 0], ['rear', -C.L / 2 - 120, 0], ['driver side', 0, -C.W / 2 - 140], ['passenger side', 0, C.W / 2 + 140]]) {
    const l = mkLabel(txt, 'axis'); l.position.set(x, 8, z); scene.add(l);
  }
  {
    const g = new THREE.SphereGeometry(9, 14, 10), m = new THREE.MeshStandardMaterial({ color: 0xffb74d, emissive: 0x442800 });
    for (const s of PACK.sensors || []) {
      const sp = new THREE.Mesh(g, m); sp.position.set(s.x, s.y, s.z); scene.add(sp);
      const l = mkLabel(s.n, 'axis'); l.position.set(s.x, s.y + 14, s.z); scene.add(l);
    }
  }

  // legend gradient from the shared colour function
  const bar = host.querySelector('.pack3d-legend i');
  if (bar) bar.style.background = `linear-gradient(90deg, ${[0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1].map(v => Tiles.cellColor(v, 0, 1)).join(',')})`;

  hookPointer(host);
  ro = new ResizeObserver(resize); ro.observe(host);
  resize(); applyView();
  state.built = true;
  loop();
}

// ── painting ──────────────────────────────────────────────────────────────
function colorFor(t) {
  const q = Math.round(t * 100);
  let c = colorCache.get(q);
  if (!c) { c = new THREE.Color().setStyle(Tiles.cellColor(q, 0, 100)); colorCache.set(q, c); }
  return c;
}
function paint() {
  const data = state.last; if (!state.built || !data) return;
  const cells = data.cells, f = PackLayout.stats(cells), sc = PackLayout.SCALES[state.opts.scale] || PackLayout.SCALES.dev;
  for (let i = 0; i < bodies.length; i++) slabs.setColorAt(i, colorFor(sc.t(cells[i], f, i, state.rest)));
  slabs.instanceColor.needsUpdate = true;
  const mode = state.opts.labels;
  for (let i = 0; i < bodies.length; i++) {
    const show = mode === 'all' || (mode === 'minmax' && (i === f.imin || i === f.imax)) || i === state.pinned || i === state.hover;
    const l = pairLabels[i]; l.visible = show;
    if (show) { l.element.textContent = `${i} · ${cells[i]}`; l.element.classList.toggle('hot', i === f.imin); }
  }
  const ends = state.host.querySelectorAll('.pack3d-legend span');
  if (ends.length === 2) { ends[0].textContent = sc.lo(f); ends[1].textContent = sc.hi(f); }
  readout(state.hover >= 0 ? state.hover : state.pinned, cells, f);
}
function readout(i, cells, f) {
  const note = state.note; if (!note) return;
  if (i < 0 || !bodies[i]) {
    hiBox.visible = false;
    note.innerHTML = `spread <b>${(f.max - f.min).toFixed(0)} mV</b> · mean <b>${f.mean.toFixed(0)} mV</b> · lowest pair <b>${f.imin}</b> · highest <b>${f.imax}</b> · hover a pair`;
    return;
  }
  const b = bodies[i], dev = cells[i] - f.mean, drop = state.rest ? cells[i] - state.rest[i] : null;
  const sign = v => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(0);
  note.innerHTML = `pair <b>${i}</b> (LeafSpy ${i + 1}) · <b>${cells[i]} mV</b> · ${sign(dev)} mV vs mean` +
    (drop == null ? '' : ` · ${sign(drop)} mV from rest`) + ` · module ${b.m + 1} of ${bodies.length / 2} · ${b.loc}` +
    (b.verify ? ` <span class="verify" title="${b.verify}">(position unverified)</span>` : '') +
    (state.pinned === i ? ' · pinned' : '');
  hiBox.visible = true; hiBox.position.set(b.cx, b.cy, b.cz); hiBox.scale.set(b.sx + 4, b.sy + 4, b.sz + 4);
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
  renderer.domElement.addEventListener('click', e => { const i = pick(e); state.pinned = (i === state.pinned) ? -1 : i; paint(); });
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
    box.innerHTML = `<h5>3D pack</h5>
      <div class="row"><label>Colour by</label>${sel('scale', Object.entries(PackLayout.SCALES).map(([k, s]) => [k, s.label]))}</div>
      <div class="row"><label>Values</label>${sel('labels', [['minmax', 'lowest and highest pair'], ['hover', 'hover only'], ['all', 'every pair']])}</div>
      <div class="row"><label>Case</label><input type="range" data-k="case" min="0" max="60" value="${Math.round((o.case ?? DEFAULT_OPTS.case) * 100)}"> <span style="color:var(--dim)">opacity</span></div>
      <div class="row seg">${Object.keys(VIEWS).map(v => `<button data-view="${v}" class="${(o.view || DEFAULT_OPTS.view) === v ? 'on' : ''}">${v}</button>`).join('')}<span style="color:var(--dim)">view</span></div>
      <div class="row"><label style="min-width:0"><input type="checkbox" data-k="spin" ${o.spin ? 'checked' : ''}> auto-rotate</label></div>`;
    box.querySelectorAll('select[data-k]').forEach(s => s.addEventListener('change', () => { o[s.dataset.k] = s.value; commit(); }));
    box.querySelector('input[data-k="case"]').addEventListener('input', e => { o.case = +e.target.value / 100; setOpts(o); });
    box.querySelector('input[data-k="case"]').addEventListener('change', commit);
    box.querySelector('input[data-k="spin"]').addEventListener('change', e => { o.spin = e.target.checked; commit(); });
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
