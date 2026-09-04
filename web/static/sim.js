/* SPDX-FileCopyrightText: 2026 David D. Karnowski */
/* SPDX-License-Identifier: AGPL-3.0-or-later */

// sim.js — the simulator cockpit (/sim). Drives hakake_sim.py's control API
// from a page that looks like the car, built on the dashboard's own tile
// engine (tilestudio.js) and its four styled tiles (tiles.js).
//
// Two rules, both tested (tests/test_sim_panel.py, tests/test_sim_page.py):
//
//   1. THERE IS NO KNOB LIST HERE. Every control is generated from
//      /sim/schema and grouped by the schema's own `category`. The Leaf's
//      sixty-odd knobs and the Lancer's twenty-eight render from the same
//      code; so will the next profile's. The few knob names this file does
//      spell out are the ones an interactive skin has to drive (a TEMP button
//      must know it moves the setpoint) or read, and they are hooked up only
//      when the schema actually carries them.
//
//   2. THE SKIN IS FEATURE-DETECTED, NEVER PROFILE-DETECTED. The cluster
//      draws when the record carries `lamps` and a state of charge; the head
//      unit when the record carries HVAC and the schema its knobs; each reused
//      tile stays only if its renderer found its keys. A core that cannot
//      speak the dashboard's vocabulary answers 501 to /sim/record, and then
//      only the knob cards, the time card and the raw state remain.
//
// The cluster and head unit are ORIGINAL DRAWINGS in plain SVG and CSS from
// published descriptions of the ZE0's layout — no photograph, scan or
// manufacturer mark. Every temperature is shown °F first with °C alongside
// (Tiles.fmtTemp), the house rule.
(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fmtTemp = (c, f, o) => window.Tiles.fmtTemp(c, f, o);
  const c2f = c => c * 9 / 5 + 32, f2c = f => (f - 32) * 5 / 9;
  const round1 = v => Math.round(v * 10) / 10;
  const fmtVal = v => v == null ? '—'
    : typeof v === 'number' ? String(Number.isInteger(v) ? v : Math.round(v * 100) / 100)
    : Array.isArray(v) ? (v.length > 6 ? v.length + ' values' : v.join(', '))
    : typeof v === 'object' ? JSON.stringify(v) : String(v);

  // ── state ──
  let CONTROL = null;          // base URL of the control API
  let SCHEMA = {};             // /sim/schema knobs — the only source of truth for controls
  let KNOBVAL = {};            // /sim/knobs
  let REC = {};                // /sim/record (dashboard vocabulary) or /sim/state when the core has no record()
  let INFO = {};               // /sim/info
  let HAVE_RECORD = true;
  let failures = 0;
  const touched = new Map();   // knob → ms of the last local edit; polling must not stomp it
  const EDIT_GRACE = 1500;
  const HOOKS = [];            // {name, sync(v)} — controls living inside the reused tiles
  const has = n => Object.prototype.hasOwnProperty.call(SCHEMA, n);
  const hasAll = (...n) => n.every(has);
  const grid = () => document.getElementById('simtiles');
  const cardOf = id => grid().querySelector(`.card[data-tile="${id}"]`);

  // Annotations, NOT a knob list: every knob still renders from the schema.
  // These only add an honest "not readable" badge where docs/SIGNALS.md
  // records that the dashboard cannot read what the knob simulates.
  const NOT_READABLE = {
    accel_pedal_pct: 'Simulated, but the dashboard has no tile for it: 0x180 (throttle) is not in the Leaf profile\'s ITEMS.',
    brake_pct: 'Simulated, but the dashboard has no tile for it: the brake signal is not in the Leaf profile\'s ITEMS.',
    heater_level: 'Group 10 byte 36 — position verified, scale unresolved (docs/SIGNALS.md).',
    sunload: 'Group 10 byte 3 — position verified, scale unresolved (docs/SIGNALS.md).',
  };
  const CATEGORY_ORDER = ['battery', 'load', 'engine', 'climate', 'body', 'diagnostics', 'rig', 'other', 'faults'];
  const CATEGORY_TITLE = {
    battery: 'Battery pack', load: 'Load & motion', engine: 'Engine', climate: 'Climate',
    body: 'Body & lights', diagnostics: 'Diagnostics', rig: 'Rig & environment',
    other: 'Other', faults: 'Faults — destructive states',
  };
  const CATEGORY_HELP = {
    faults: 'States the real car cannot be asked to produce on demand — a collapsed cell pair, a dead adapter, an ECU answering NRC. That is what the simulator is for. Turning one on changes what the decoders see.',
    rig: 'The rig itself, not the car: how jittery the sensors are, and how fast simulated time runs (also on the Simulated time card).',
  };

  // ── transport ──
  // GETs are "simple" cross-origin requests; a POST carrying JSON needs the
  // preflight the API answers (OPTIONS 204). Should that ever fail in a
  // browser, the same body is resent as text/plain — a simple request — which
  // the API parses as JSON regardless of the declared type.
  async function api(path, method, body) {
    const opt = { method: method || 'GET' };
    if (body !== undefined) { opt.headers = { 'content-type': 'application/json' }; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(CONTROL + path, opt); }
    catch (e) {
      if (body === undefined) throw e;
      opt.headers = { 'content-type': 'text/plain' };
      r = await fetch(CONTROL + path, opt);
    }
    let doc = {};
    try { doc = await r.json(); } catch (e) { doc = { error: 'the API returned no JSON' }; }
    if (!r.ok) { toast(path, doc); const err = new Error(doc.error || String(r.status)); err.status = r.status; throw err; }
    return doc;
  }

  // Errors are shown, never swallowed: a bad value must surface the API's own
  // near-match suggestion, which is the whole reason the API computes one.
  function toast(where, doc) {
    const box = el('div', 'toast');
    const x = el('button', null, '✕'); x.onclick = () => box.remove();
    box.appendChild(x);
    box.appendChild(el('h4', null, 'Rejected by ' + where));
    box.appendChild(el('p', null, (doc && doc.error) || 'unknown error'));
    const sug = doc && doc.suggestions;
    if (sug && Object.keys(sug).length) {
      const s = el('div', 'sug');
      for (const [bad, list] of Object.entries(sug)) {
        if (!list || !list.length) continue;
        s.appendChild(document.createTextNode(bad + ' → '));
        list.forEach((n, i) => {
          const b = el('b', null, n);
          b.onclick = () => flashKnob(n);
          s.appendChild(b);
          if (i < list.length - 1) s.appendChild(document.createTextNode(', '));
        });
        s.appendChild(el('br'));
      }
      box.appendChild(s);
    }
    $('#toasts').appendChild(box);
    setTimeout(() => box.remove(), 14000);
  }
  function flashKnob(name) {
    const f = document.getElementById('k:' + name); if (!f) return;
    f.scrollIntoView({ block: 'center', behavior: 'smooth' });
    f.classList.add('flash'); setTimeout(() => f.classList.remove('flash'), 1600);
  }

  async function setKnob(name, value) {
    touched.set(name, Date.now());
    try {
      const r = await api('/sim/knobs', 'POST', { [name]: value });
      Object.assign(KNOBVAL, r.applied || {});
      // a knob with side effects (SOH moves usable capacity; picking a charge
      // source re-seats the taper knobs) must move the other controls straight away
      for (const k of Object.keys(r.applied || {})) if (k !== name) touched.delete(k);
    } catch (e) { /* toasted */ }
    syncControls(); syncHooks();
    tick();
  }
  function bump(name, by) {
    const spec = SCHEMA[name]; if (!spec) return;
    let v = (typeof KNOBVAL[name] === 'number' ? KNOBVAL[name] : spec.default) + by;
    if (spec.min != null) v = Math.max(spec.min, v);
    if (spec.max != null) v = Math.min(spec.max, v);
    setKnob(name, v);
  }
  function cycle(name) {
    const spec = SCHEMA[name]; if (!spec || !Array.isArray(spec.choices) || !spec.choices.length) return;
    const i = spec.choices.indexOf(KNOBVAL[name]);
    setKnob(name, spec.choices[(i + 1) % spec.choices.length]);
  }

  // ── where the control API is ──
  // ?control= in the URL → what app.py knew at startup → what the reader
  // publishes in /api/status (the port it really bound, e.g. --sim-control 0).
  async function resolveControl() {
    const q = new URLSearchParams(location.search).get('control');
    if (q) return q.replace(/\/+$/, '');
    if (window.SIM_CONTROL_URL) return String(window.SIM_CONTROL_URL).replace(/\/+$/, '');
    for (let i = 0; i < 40; i++) {
      let st = null;
      try { st = await (await fetch('/api/status')).json(); } catch (e) { /* dashboard offline */ }
      if (st && st.sim_control_url) return String(st.sim_control_url).replace(/\/+$/, '');
      const simulated = !!(st && st.simulated);
      if (!simulated && i >= 1) return null;       // not a simulated run: say how to start one
      showNotice(simulated ? 'waiting' : null);   // simulated, port not published yet: keep asking
      await sleep(1000);
    }
    return null;
  }
  function showNotice(kind, text) {
    const n = $('#notice'); n.hidden = false;
    $('#notice-wait').hidden = kind !== 'waiting';
    if (text) $('#notice-text').textContent = text;
  }

  // ── live data ──
  async function fetchLive() {
    if (HAVE_RECORD) {
      const r = await fetch(CONTROL + '/sim/record');
      if (r.ok) { REC = (await r.json()).record || {}; return; }
      if (r.status !== 501) throw new Error('/sim/record ' + r.status);
      HAVE_RECORD = false;                          // this core cannot speak the dashboard's vocabulary
    }
    REC = (await api('/sim/state')).state || {};
  }
  async function tick() {
    try {
      const [, kn] = await Promise.all([fetchLive(), api('/sim/knobs')]);
      KNOBVAL = kn.knobs || {};
      failures = 0; setDot(true);
      paintTiles(); paintCluster(); paintHead(); paintTime(); paintState(); paintPower();
      syncControls(); syncHooks();
      if (window.TileStudio) TileStudio.update(REC);
    } catch (e) { failures++; if (failures > 1) setDot(false); }
  }
  async function refreshInfo() {
    try { INFO = await api('/sim/info'); renderFacts(); paintTime(); } catch (e) { /* toasted */ }
  }
  function setDot(ok) { const d = $('#dot'); if (d) d.className = ok ? 'ok' : 'err'; }

  // ── header facts ──
  function renderFacts() {
    const f = $('#facts'); f.textContent = '';
    const add = (k, v, cls) => { const d = el('div', 'fact' + (cls ? ' ' + cls : '')); d.appendChild(document.createTextNode(k + ' ')); d.appendChild(el('b', null, v)); f.appendChild(d); return d; };
    add('vehicle', INFO.vehicle || '—');
    add('scenario', INFO.scenario || 'none (free-running)');
    add('seed', INFO.seed == null ? '—' : String(INFO.seed));
    const ts = INFO.time_scale;
    const c = add('clock', ts == null ? 'unknown' : ts + '×', 'clock' + (ts != null && ts > 1 ? ' fast' : ''));
    c.title = 'Simulated seconds per real second. Source: ' + (INFO.time_scale_source || 'unknown') + '.';
    const d = el('div', 'fact'); const dot = el('span', failures > 1 ? 'err' : 'ok'); dot.id = 'dot';
    d.appendChild(dot); d.appendChild(document.createTextNode(' live')); f.appendChild(d);
  }

  // ── toolbar: scenario, reset ──
  function buildToolbar() {
    const sel = $('#scen');
    for (const n of INFO.scenarios || []) { const o = el('option', null, n); o.value = n; sel.appendChild(o); }
    sel.value = INFO.scenario || '';
    sel.onchange = async () => {
      try { await api('/sim/scenario', 'POST', { name: sel.value }); location.reload(); }   // a scenario may switch vehicles: rebuild everything
      catch (e) { sel.value = INFO.scenario || ''; }
    };
    $('#reset').onclick = async () => {
      try { await api('/sim/reset', 'POST', {}); touched.clear(); tick(); } catch (e) { /* toasted */ }
    };
    $('#toolbar').hidden = false;
  }

  // ── generated knob cards: the whole control surface, from the schema ──
  const isNumeric = t => t === 'float' || t === 'int' || t === 'number';
  const isText = t => t === 'text' || t === 'str' || t === 'string';
  const isTempUnit = u => u === '°C' || u === '°F' || u === 'C' || u === 'F';
  const labelOf = (name, spec) => (spec && spec.label) || name.replace(/^fault\./, '').replace(/_/g, ' ');

  function buildKnobCards() {
    const groups = new Map();
    for (const [name, spec] of Object.entries(SCHEMA)) {
      const cat = spec.category || 'other';
      if (!groups.has(cat)) groups.set(cat, []);
      groups.get(cat).push([name, spec]);
    }
    const ordered = [...groups.keys()].sort((a, b) => {
      const ia = CATEGORY_ORDER.indexOf(a), ib = CATEGORY_ORDER.indexOf(b);
      return (ia < 0 ? 90 : ia) - (ib < 0 ? 90 : ib) || a.localeCompare(b);
    });
    const tpl = document.getElementById('knob-card-tpl');
    for (const cat of ordered) {
      const card = tpl.content.firstElementChild.cloneNode(true);
      card.dataset.tile = 'knobs-' + cat;
      if (cat === 'faults') card.classList.add('faults');
      $('.ttl', card).textContent = CATEGORY_TITLE[cat] || cat;
      if (CATEGORY_HELP[cat]) { const h = $('.knob-help', card); h.textContent = CATEGORY_HELP[cat]; h.hidden = false; }
      const box = $('.knobs', card);
      for (const [name, spec] of groups.get(cat)) box.appendChild(buildKnob(name, spec));
      grid().appendChild(card);
    }
  }

  function buildKnob(name, spec) {
    const wrap = el('div', 'knob');
    wrap.id = 'k:' + name; wrap.dataset.knob = name;
    const row = el('div', 'row1');
    row.appendChild(el('span', 'lbl', labelOf(name, spec)));
    row.appendChild(el('span', 'nm', name));
    if (spec.unit && !isTempUnit(spec.unit)) row.appendChild(el('span', 'un', spec.unit));
    if (NOT_READABLE[name]) { const t = el('span', 'tag blind', 'not readable'); t.title = NOT_READABLE[name]; row.appendChild(t); }
    if (spec.help) row.title = spec.help;
    wrap.appendChild(row);
    const ctl = el('div', 'ctl');
    const t = spec.type;
    if (t === 'bool') {
      const sw = el('label', 'sw'); const cb = el('input'); cb.type = 'checkbox';
      cb.onchange = () => { wrap.classList.toggle('on', cb.checked); setKnob(name, cb.checked); };
      sw.appendChild(cb); sw.appendChild(el('span')); ctl.appendChild(sw);
      ctl.appendChild(el('span', 'un', 'off / on'));
      wrap._sync = v => { cb.checked = !!v; wrap.classList.toggle('on', !!v); };
      wrap._focus = () => cb === document.activeElement;
    } else if (isText(t) && Array.isArray(spec.choices) && spec.choices.length) {
      const sel = el('select');
      for (const c of spec.choices) { const o = el('option', null, c); o.value = c; sel.appendChild(o); }
      sel.onchange = () => setKnob(name, sel.value);
      ctl.appendChild(sel);
      wrap._sync = v => { sel.value = v; };
      wrap._focus = () => sel === document.activeElement;
    } else if (isNumeric(t) && isTempUnit(spec.unit)) {
      buildTempPair(wrap, ctl, name, spec);
    } else if (isNumeric(t)) {
      const bounded = spec.min != null && spec.max != null;
      const stepv = t === 'int' ? 1 : bounded ? Math.max((spec.max - spec.min) / 500, 0.001) : 'any';
      let range = null;
      if (bounded) { range = el('input'); range.type = 'range'; range.min = spec.min; range.max = spec.max; range.step = stepv; ctl.appendChild(range); }
      const num = el('input'); num.type = 'number';
      if (spec.min != null) num.min = spec.min;
      if (spec.max != null) num.max = spec.max;
      num.step = stepv; ctl.appendChild(num);
      if (range) {
        range.oninput = () => { num.value = range.value; touched.set(name, Date.now()); };
        range.onchange = () => setKnob(name, parseFloat(range.value));
        num.oninput = () => { range.value = num.value; touched.set(name, Date.now()); };
      }
      num.onchange = () => setKnob(name, num.value === '' ? spec.default : parseFloat(num.value));
      wrap._sync = v => { num.value = v; if (range) range.value = v; };
      wrap._focus = () => num === document.activeElement || range === document.activeElement;
    } else {
      const inp = el('input'); inp.type = 'text';
      inp.onchange = () => setKnob(name, inp.value);
      ctl.appendChild(inp);
      wrap._sync = v => { inp.value = v == null ? '' : v; };
      wrap._focus = () => inp === document.activeElement;
    }
    wrap.appendChild(ctl);
    return wrap;
  }

  // A temperature knob, whatever unit the model keeps it in, is edited in °F
  // with °C alongside: slider in °F, a number box for each, either box
  // converts and the POST goes out in the knob's native unit.
  function buildTempPair(wrap, ctl, name, spec) {
    const nativeF = spec.unit === '°F' || spec.unit === 'F';
    const toF = v => nativeF ? v : c2f(v), toC = v => nativeF ? f2c(v) : v;
    const fromF = f => nativeF ? f : f2c(f), fromC = c => nativeF ? c2f(c) : c;
    const bounded = spec.min != null && spec.max != null;
    let range = null;
    if (bounded) {
      range = el('input'); range.type = 'range';
      range.min = Math.round(toF(spec.min)); range.max = Math.round(toF(spec.max)); range.step = 1;
      ctl.appendChild(range);
    }
    const nf = el('input'); nf.type = 'number'; nf.step = 1; nf.title = 'degrees Fahrenheit';
    const nc = el('input'); nc.type = 'number'; nc.step = 0.5; nc.title = 'degrees Celsius';
    if (bounded) { nf.min = Math.round(toF(spec.min)); nf.max = Math.round(toF(spec.max)); nc.min = round1(toC(spec.min)); nc.max = round1(toC(spec.max)); }
    ctl.appendChild(nf); ctl.appendChild(el('span', 'tu', '°F'));
    ctl.appendChild(nc); ctl.appendChild(el('span', 'tu', '°C'));
    const show = (f, c) => { nf.value = Math.round(f); nc.value = round1(c); if (range) range.value = Math.round(f); };
    if (range) {
      range.oninput = () => { touched.set(name, Date.now()); const f = parseFloat(range.value); show(f, f2c(f)); };
      range.onchange = () => setKnob(name, round1(fromF(parseFloat(range.value))));
    }
    nf.oninput = () => touched.set(name, Date.now());
    nc.oninput = () => touched.set(name, Date.now());
    nf.onchange = () => { if (nf.value === '') return; const f = parseFloat(nf.value); show(f, f2c(f)); setKnob(name, round1(fromF(f))); };
    nc.onchange = () => { if (nc.value === '') return; const c = parseFloat(nc.value); show(c2f(c), c); setKnob(name, round1(fromC(c))); };
    wrap._sync = v => { if (typeof v !== 'number') return; show(toF(v), toC(v)); };
    wrap._focus = () => [nf, nc, range].includes(document.activeElement);
  }

  function syncControls() {
    const now = Date.now();
    for (const [name, spec] of Object.entries(SCHEMA)) {
      const wrap = document.getElementById('k:' + name);
      if (!wrap || !wrap._sync) continue;
      if ((touched.get(name) || 0) + EDIT_GRACE > now) continue;
      const v = KNOBVAL[name];
      if (v === undefined || wrap._focus()) continue;
      wrap._sync(v);
      wrap.classList.toggle('changed', JSON.stringify(v) !== JSON.stringify(spec.default));
    }
  }
  function syncHooks() {
    const now = Date.now();
    for (const h of HOOKS) {
      if ((touched.get(h.name) || 0) + EDIT_GRACE > now) continue;
      if (KNOBVAL[h.name] !== undefined) h.sync(KNOBVAL[h.name]);
    }
  }

  // ── the reused tiles ──
  // Paint each with the record; on the first pass drop any whose renderer
  // reports the record does not carry its keys. Ids in the partials are the
  // dashboard's; renderers are scoped to the card.
  const TILE_RENDER = { vehicle: 'renderVehicle', tires: 'renderTires', body: 'renderBody', climate: 'renderClimate' };
  function paintTiles(first) {
    for (const [id, fn] of Object.entries(TILE_RENDER)) {
      const card = cardOf(id); if (!card) continue;
      let ok = false;
      if (HAVE_RECORD) { try { ok = !!window.Tiles[fn](card, REC); } catch (e) { ok = false; } }
      if (first && !ok) card.remove();
    }
  }

  // Controls inside the reused tiles. Each installs only when the schema has
  // the knob it would move; the page never assumes one exists.
  function installHooks() {
    const hint = (card, txt) => { if (card) card.appendChild(el('div', 'sim-hint', txt)); };
    const clickable = (node, name, fn) => { if (!node || !has(name)) return false; node.style.cursor = 'pointer'; node.addEventListener('click', e => { e.stopPropagation(); fn(); }); const t = document.createElementNS(node.namespaceURI, 'title'); t.textContent = labelOf(name, SCHEMA[name]) + ' — click to change'; node.appendChild(t); return true; };
    const toggle = name => setKnob(name, !KNOBVAL[name]);

    // tires: one pressure control under each wheel, from whichever tpms_<corner> knobs exist
    const tires = cardOf('tires');
    if (tires) {
      let any = false;
      for (const w of $$('.wheel[data-w]', tires)) {
        const name = 'tpms_' + w.dataset.w; const spec = SCHEMA[name];
        if (!spec || !isNumeric(spec.type)) continue;
        any = true;
        const box = el('div', 'wheel-ctl');
        const range = el('input'); range.type = 'range'; range.min = spec.min; range.max = spec.max; range.step = 0.5;
        const num = el('input'); num.type = 'number'; num.min = spec.min; num.max = spec.max; num.step = 0.5; num.title = labelOf(name, spec) + ' (' + (spec.unit || '') + ')';
        range.oninput = () => { num.value = range.value; touched.set(name, Date.now()); };
        range.onchange = () => setKnob(name, parseFloat(range.value));
        num.onchange = () => setKnob(name, parseFloat(num.value));
        box.appendChild(range); box.appendChild(num); w.appendChild(box);
        HOOKS.push({ name, sync: v => { if (document.activeElement !== num) num.value = v; if (document.activeElement !== range) range.value = v; } });
      }
      if (any) hint(tires, 'drag a slider to set that tyre — the dashboard tile reads the result');
    }

    // body: each door, lamp and lock is a click target; a button strip covers the rest
    const body = cardOf('body');
    if (body) {
      const DOOR_EL = { driver: 'door-dl', pass: 'door-dr', rl: 'door-rl', rr: 'door-rr', hatch: 'door-hatch' };
      for (const name of Object.keys(SCHEMA)) {
        const m = /^door_(\w+)$/.exec(name); if (!m || SCHEMA[name].type !== 'bool') continue;
        clickable($('#' + DOOR_EL[m[1]], body), name, () => toggle(name));
      }
      for (const id of ['lamp-head-l', 'lamp-head-r']) clickable($('#' + id, body), 'headlights', () => toggle('headlights'));
      for (const id of ['lamp-fog-l', 'lamp-fog-r']) clickable($('#' + id, body), 'fog_lights', () => toggle('fog_lights'));
      for (const id of ['lamp-park-l', 'lamp-park-r']) clickable($('#' + id, body), 'parking_lights', () => toggle('parking_lights'));
      for (const id of ['lamp-turn-fl', 'lamp-turn-fr', 'lamp-turn-rl', 'lamp-turn-rr', 'lamp-side-l', 'lamp-side-r']) clickable($('#' + id, body), 'turn_signal', () => cycle('turn_signal'));
      for (const id of ['lamp-brake-l', 'lamp-brake-r']) clickable($('#' + id, body), 'brake_pct', () => setKnob('brake_pct', KNOBVAL.brake_pct > 0 ? 0 : 50));
      for (const id of ['lock-dl', 'lock-dr', 'lock-rl', 'lock-rr']) clickable($('#' + id, body), 'locked', () => toggle('locked'));
      const strip = el('div', 'body-ctl');
      const btn = (name, label, fn, isOn) => {
        if (!has(name)) return;
        const b = el('button', null, label || labelOf(name, SCHEMA[name])); b.onclick = fn; strip.appendChild(b);
        HOOKS.push({ name, sync: v => { b.classList.toggle('on', isOn(v)); if (!label) b.textContent = labelOf(name, SCHEMA[name]) + (typeof v === 'string' ? ': ' + v : ''); } });
      };
      for (const name of ['headlights', 'high_beam', 'parking_lights', 'fog_lights', 'locked']) btn(name, null, () => toggle(name), v => !!v);
      btn('turn_signal', null, () => cycle('turn_signal'), v => v && v !== 'off');
      btn('brake_pct', null, () => setKnob('brake_pct', KNOBVAL.brake_pct > 0 ? 0 : 50), v => v > 0);
      if (strip.children.length) body.appendChild(strip);
      hint(body, 'click a door, lamp or lock to change it');
    }

    // vehicle: the shifter slots select the gear; state and parking brake cycle on click
    const veh = cardOf('vehicle');
    if (veh) {
      const gearSpec = SCHEMA.gear;
      if (gearSpec && Array.isArray(gearSpec.choices)) {
        const SLOT = { 'sh-R': 'R', 'sh-N': 'N', 'sh-D': 'D', 'sh-eco': 'Eco', 'sh-P': 'P', 'sh-Ptxt': 'P' };
        for (const [id, g] of Object.entries(SLOT)) if (gearSpec.choices.includes(g)) clickable($('#' + id, veh), 'gear', () => setKnob('gear', g));
      }
      const st = $('#veh-state', veh);
      if (st && has('start_state') && Array.isArray(SCHEMA.start_state.choices)) { st.style.cursor = 'pointer'; st.title = labelOf('start_state', SCHEMA.start_state) + ' — click to cycle'; st.onclick = () => cycle('start_state'); }
      const hb = $('#veh-brake', veh);
      if (hb && has('handbrake')) { hb.style.cursor = 'pointer'; hb.title = labelOf('handbrake', SCHEMA.handbrake) + ' — click to toggle'; hb.onclick = () => toggle('handbrake'); }
      hint(veh, 'click a shifter slot, the state or the parking brake to change it');
    }
  }

  // ── the cluster: an original drawing of the ZE0's combination meter ──
  // Lit from record.lamps. Keyed on the lamp names the model publishes; any
  // lamp it publishes that is not styled here still draws, as its name.
  const P = {   // small original glyphs, 24×24
    arrowL: '<path d="M14 5 4 12l10 7v-4h6V9h-6z"/>',
    arrowR: '<path d="M10 5l10 7-10 7v-4H4V9h6z"/>',
    tri: '<path d="M12 3 2 21h20z"/><path d="M12 9v5M12 17v1"/>',
    beamLo: '<path d="M9 5a7 7 0 0 0 0 14"/><path d="M3 8l4 1M3 12h4M3 16l4-1"/>',
    beamHi: '<path d="M9 5a7 7 0 0 0 0 14"/><path d="M2 7h5M2 12h5M2 17h5"/>',
    bulb: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3 11v2h6v-2a6 6 0 0 0-3-11z"/>',
    fog: '<path d="M9 5a7 7 0 0 0 0 14"/><path d="M2 8l5 1M2 12h5M2 16l5-1"/><path d="M15 8c2 1 2 3 0 4s-2 3 0 4"/>',
    park: '<circle cx="12" cy="12" r="9"/><path d="M10 16V8h3a2 2 0 0 1 0 4h-3"/>',
    door: '<path d="M4 10l3-4h10l3 4v7H4z"/><path d="M4 17h16"/><path d="M20 10l3-3M1 10l-3-3" stroke-dasharray="1 1"/>',
    plug: '<path d="M8 3v5M16 3v5"/><path d="M5 8h14v3a7 7 0 0 1-14 0z"/><path d="M12 18v3"/>',
    batt: '<rect x="3" y="7" width="16" height="10" rx="2"/><path d="M19 10h2v4h-2M7 12h4M13 12h2"/>',
    battLow: '<rect x="3" y="7" width="16" height="10" rx="2"/><path d="M19 10h2v4h-2"/><rect x="5" y="9" width="3" height="6" fill="currentColor" stroke="none"/>',
    car: '<path d="M5 15l2-6h10l2 6"/><rect x="3" y="15" width="18" height="4" rx="1"/><path d="M12 10v3M12 3l-1 4h2z"/>',
    turtle: '<path d="M5 14a7 5 0 0 1 14 0z"/><path d="M3 14h18M7 14v3M17 14v3"/><circle cx="20" cy="11" r="2"/>',
    tire: '<path d="M6 5h12l1 14H5z"/><path d="M12 9v5M12 16v1"/>',
    lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  };
  const LAMP_DEFS = {
    ready: { label: 'READY', color: 'green' },
    turn_left: { icon: 'arrowL', color: 'green', blink: true, label: '' },
    turn_right: { icon: 'arrowR', color: 'green', blink: true, label: '' },
    hazards: { icon: 'tri', color: 'red', blink: true, label: '' },
    low_beam: { icon: 'beamLo', color: 'green', label: '' },
    high_beam: { icon: 'beamHi', color: 'blue', label: '' },
    position: { icon: 'bulb', color: 'green', label: '' },
    fog: { icon: 'fog', color: 'green', label: '' },
    parking_brake: { icon: 'park', color: 'red', label: 'BRAKE' },
    door_ajar: { icon: 'door', color: 'red', label: '' },
    plug_in: { icon: 'plug', color: 'green', blink: true, label: '' },
    charge_12v: { icon: 'batt', color: 'red', label: '12V' },
    ev_system: { icon: 'car', color: 'amber', label: 'EV' },
    power_limit: { icon: 'turtle', color: 'amber', label: '' },
    low_battery: { icon: 'battLow', color: 'amber', label: 'LOW' },
    tpms: { icon: 'tire', color: 'amber', label: '' },
    headlight_warning: { icon: 'bulb', color: 'amber', label: 'LEFT ON' },
    security: { icon: 'lock', color: 'red', blink: true, label: '' },
    master_red: { icon: 'tri', color: 'red', label: 'MASTER' },
    master_yellow: { icon: 'tri', color: 'amber', label: 'MASTER' },
    eco: { label: 'ECO', color: 'green', skip: true },   // the eyebrow lamp already shows it
  };
  const LAMP_ORDER = Object.keys(LAMP_DEFS);
  const UNMOD_LABEL = { abs: 'ABS', vsp: 'VSP', brake_yellow: 'BRAKE', brake_red: 'BRAKE', ps: 'PS', shift_control: 'SHIFT', vdc: 'VDC', vdc_off: 'VDC OFF', seatbelt: 'SEAT BELT', airbag: 'AIR BAG', passenger_airbag: 'PASS. AIR BAG' };
  const REGEN_BUBBLES = 5, POWER_BUBBLES = 13;

  function buildCluster(root) {
    const m = el('div', 'meter');
    m.innerHTML = `
      <div class="eyebrow">
        <span class="eco-lamp" id="cl-eco">ECO</span>
        <svg id="cl-trees" width="66" height="26" viewBox="0 0 66 26" aria-label="eco trees"></svg>
        <div class="ext"><span>OUTSIDE <b id="cl-ext">--</b></span><span>ODO <b id="cl-odo">--</b></span></div>
        <div class="ext"><span>SHIFT <b id="cl-gear">--</b></span><span>PACK <b id="cl-pack">--</b></span></div>
        <div class="spd"><span class="n" id="cl-spd">0</span><span class="u" id="cl-spdu">MPH</span></div>
      </div>
      <div class="lamps" id="cl-lamps"></div>
      <div class="dotmatrix empty" id="cl-msg">—</div>
      <div class="lcd"><svg viewBox="0 0 360 158" id="cl-lcd" role="img" aria-label="simulated lower display"></svg></div>`;
    root.appendChild(m);
    root.appendChild(el('div', 'cluster-note',
      'Drawn here from published descriptions of the 2011–2012 layout, in plain SVG and CSS — no photograph, screenshot or manufacturer mark. ' +
      'Ha-Kake is not affiliated with or endorsed by Nissan (see NOTICE). It is a face on the simulator, not a reproduction of an instrument, and everything it shows is generated. ' +
      'Dashed, dim indicators are real ones the model has no driver for — they never light here.'));
    // the strip, once: every lamp the model publishes, then the ones it says it cannot drive
    const strip = $('#cl-lamps', root);
    const lamps = REC.lamps || {}, unmod = REC.lamps_unmodelled || {};
    const keys = LAMP_ORDER.filter(k => k in lamps).concat(Object.keys(lamps).filter(k => !(k in LAMP_DEFS)));
    for (const k of keys) {
      const d = LAMP_DEFS[k] || {}; if (d.skip) continue;
      const b = el('span', 'lamp-i ' + (d.color || 'amber') + (d.blink ? ' blink' : ''));
      b.dataset.lamp = k; b.title = k.replace(/_/g, ' ');
      if (d.icon) b.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 24 24">${P[d.icon]}</svg>`);
      const label = d.label == null ? k.replace(/_/g, ' ') : d.label;
      if (label) b.appendChild(el('span', null, label));
      strip.appendChild(b);
    }
    for (const k of Object.keys(unmod)) {
      const b = el('span', 'lamp-i dim amber', UNMOD_LABEL[k] || k.replace(/_/g, ' ').toUpperCase());
      b.dataset.lamp = k; b.title = 'no driver in the model — this lamp never lights here';
      strip.appendChild(b);
    }
  }

  let msgIdx = 0;
  function paintCluster() {
    const svg = document.getElementById('cl-lcd'); if (!svg) return;
    const r = REC, lamps = r.lamps || {};
    const miles = r.units_miles !== false;
    const kw = typeof r.power_kw === 'number' ? r.power_kw : 0;   // house sign: negative = discharge
    const motor = Math.max(0, -kw), regen = Math.max(0, kw);
    const soc = typeof r.soc === 'number' ? r.soc : 0;
    const soh = typeof r.soh === 'number' ? r.soh : null;
    const pt = typeof r.temp_avg_c === 'number' ? r.temp_avg_c : 20;

    // available output: fewer bubbles when hot, cold or nearly empty — the
    // same curve the model's turtle lamp uses (LeafModel.output_avail)
    let avail = 1;
    if (pt > 45) avail = Math.max(0.35, 1 - (pt - 45) / 30);
    if (pt < 5) avail = Math.min(avail, Math.max(0.35, (pt + 15) / 20));
    if (soc < 15) avail = Math.min(avail, Math.max(0.3, soc / 15));
    const availN = Math.max(1, Math.round(POWER_BUBBLES * avail));
    const litP = Math.min(POWER_BUBBLES, Math.round(motor / 80 * POWER_BUBBLES));
    const litR = Math.min(REGEN_BUBBLES, Math.round(regen / 30 * REGEN_BUBBLES));

    const parts = [];
    const N = REGEN_BUBBLES + POWER_BUBBLES, x0 = 16, x1 = 268, cy = 40, sag = 13;
    const xs = [];
    for (let i = 0; i < N; i++) {
      const f = i / (N - 1), x = x0 + f * (x1 - x0), y = cy + sag * Math.pow(2 * f - 1, 2) - sag, rr = 4.2 + 4.6 * f;
      xs.push([x, y]);
      const inRegen = i < REGEN_BUBBLES, idx = inRegen ? REGEN_BUBBLES - i : i - REGEN_BUBBLES + 1;
      let cls = 'bub-off';
      if (inRegen && idx <= litR) cls = 'bub-regen';
      else if (!inRegen && idx <= litP) cls = 'bub-on';
      else if (!inRegen && idx > availN) cls = 'bub-cap';
      parts.push(`<circle class="${cls}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${rr.toFixed(1)}"/>`);
    }
    const zx = x0 + (REGEN_BUBBLES - 0.5) / (N - 1) * (x1 - x0);
    parts.push(`<line class="zero" x1="${zx.toFixed(1)}" y1="8" x2="${zx.toFixed(1)}" y2="52"/>`);
    // the dot: left of zero under the outermost lit regen bubble, right of it under the outermost lit power bubble
    const dotX = litR ? xs[REGEN_BUBBLES - litR][0] : litP ? xs[REGEN_BUBBLES + litP - 1][0] : zx;
    parts.push(`<circle class="dot" cx="${dotX.toFixed(1)}" cy="57" r="2.6"/>`);
    parts.push('<text class="lbl" x="16" y="70">CHARGE ◀</text><text class="lbl" x="200" y="70">▶ POWER</text>');
    parts.push(`<text class="lbl" x="292" y="14">OUTPUT</text><text class="sub" x="292" y="28">${Math.round(avail * 100)}%</text>`);
    parts.push(`<text class="lbl" x="292" y="46">kW</text><text class="sub" x="292" y="60">${(Math.round(kw * 10) / 10).toFixed(1)}</text>`);
    parts.push(`<text class="lbl" x="326" y="46">A</text><text class="sub" x="326" y="60">${typeof r.current_a === 'number' ? r.current_a.toFixed(0) : '--'}</text>`);

    // battery temperature gauge — 8 segments, -10 .. 60 °C
    const tsegs = 8, tf = Math.max(0, Math.min(1, (pt + 10) / 70)), ton = Math.max(1, Math.round(tf * tsegs));
    parts.push('<text class="lbl" x="16" y="86">PACK TEMP</text>');
    for (let i = 0; i < tsegs; i++) parts.push(`<rect class="${i < ton ? 'seg-on' : 'seg-off'}" x="${16 + i * 13}" y="92" width="10" height="12" rx="2"/>`);
    parts.push(`<text class="sub" x="122" y="102">${fmtTemp(pt, r.temp_avg_f, { cDec: 0 })}</text>`);

    // remaining energy — 12 bars
    parts.push('<text class="lbl" x="16" y="122">REMAINING ENERGY</text>');
    const eon = Math.round(soc / 100 * 12);
    for (let i = 0; i < 12; i++) parts.push(`<rect class="${i < eon ? 'seg-on' : 'seg-off'}" x="${16 + i * 13}" y="128" width="10" height="14" rx="2"/>`);
    parts.push(`<text class="sub" x="176" y="140">${soc.toFixed(1)} %</text>`);

    // capacity level gauge — the twelve bars, and NOT the same fact as above
    parts.push('<text class="lbl" x="228" y="86">CAPACITY</text>');
    if (soh != null) {
      const con = Math.max(0, Math.min(12, Math.round(soh / 100 * 12)));
      for (let i = 0; i < 12; i++) parts.push(`<rect class="${i < con ? 'seg-on' : 'seg-lost'}" x="${228 + (i % 6) * 12}" y="${92 + Math.floor(i / 6) * 9}" width="9" height="6" rx="1.5"/>`);
      parts.push(`<text class="sub" x="228" y="122">${soh.toFixed(1)} % SOH</text>`);
    }
    // distance to empty, in the dash's units
    const dte = miles ? r.range_mi : r.range_km;
    parts.push('<text class="lbl" x="228" y="136">TO EMPTY</text>');
    parts.push(`<text class="big" x="228" y="154">${typeof dte === 'number' ? Math.round(dte) : '---'}</text><text class="sub" x="272" y="154">${miles ? 'mi' : 'km'}</text>`);
    svg.innerHTML = parts.join('');

    // eyebrow
    const spd = miles ? r.speed_mph : r.speed_kmh;
    $('#cl-spd').textContent = Math.round(typeof spd === 'number' ? spd : 0);
    $('#cl-spdu').textContent = miles ? 'MPH' : 'KM/H';
    $('#cl-ext').textContent = typeof r.hvac_ambient_c === 'number' ? fmtTemp(r.hvac_ambient_c, r.hvac_ambient_f, { cDec: 0 }) : '--';
    $('#cl-pack').textContent = fmtTemp(pt, r.temp_avg_f, { cDec: 0 });
    const odo = miles ? r.odometer_mi : r.odometer_km;
    $('#cl-odo').textContent = typeof odo === 'number' ? Math.round(odo).toLocaleString() + (miles ? ' mi' : ' km') : '--';
    $('#cl-gear').textContent = r.gear || '--';
    $('#cl-eco').classList.toggle('on', !!lamps.eco);
    // eco trees grow when little is being drawn
    const grown = motor < 2 ? 3 : motor < 12 ? 2 : motor < 30 ? 1 : 0;
    let t = '';
    for (let i = 0; i < 3; i++) { const c = i < grown ? '#4caf50' : '#20323a', x = 6 + i * 21, h = 8 + i * 4; t += `<rect x="${x + 5}" y="18" width="2" height="4" fill="${c}"/><path d="M${x + 6} ${18 - h} L${x + 11} 18 L${x + 1} 18 Z" fill="${c}"/>`; }
    $('#cl-trees').innerHTML = t;
    // indicators
    for (const b of $$('#cl-lamps .lamp-i:not(.dim)')) b.classList.toggle('on', !!lamps[b.dataset.lamp]);
    // dot-matrix line: one message at a time, rotating
    const msgs = Array.isArray(r.messages) ? r.messages : [];
    const line = $('#cl-msg');
    if (!msgs.length) { line.textContent = '—'; line.classList.add('empty'); }
    else { line.classList.remove('empty'); line.textContent = msgs[Math.floor(msgIdx / 3) % msgs.length] + (msgs.length > 1 ? `  (${(Math.floor(msgIdx / 3) % msgs.length) + 1}/${msgs.length})` : ''); msgIdx++; }
  }

  // ── the climate head unit ──
  // Buttons that map to a knob work; controls the real car has but this
  // project cannot READ are drawn inert and say why (docs/SIGNALS.md).
  const INERT_HVAC = [
    ['AUTO', 'auto', 'Walked on 2026-08-24 with calibrate_input.py: nothing moved in HVAC groups 00/01/10/11 (group 00 stayed 80 01 80 00). Not readable from the HVAC amp on this car — docs/SIGNALS.md.'],
    ['MODE', 'mode', 'Vent mode: the 4-position cycle was walked twice and moved nothing anywhere. OVMS reads it from EV-CAN 0x54B, which needs the re-pinned cable. docs/SIGNALS.md.'],
    ['FRESH / RECIRC', 'recirc', 'Intake door: walked, nothing moved in groups 10/11/01. Not exposed by this ECU. docs/SIGNALS.md.'],
    ['DEFROST', 'defrost', 'Front defroster. Group 01 bytes 38/39 change after A/C or defrost but the encoding is unresolved, so this is not something the dashboard can claim to read. docs/SIGNALS.md.'],
    ['DEFOG', 'defog', 'Rear defogger: no signal for it is decoded on this car. docs/SIGNALS.md.'],
  ];
  const HI = {
    off: '<svg viewBox="0 0 24 24"><path d="M12 4v8"/><path d="M6.6 7A8 8 0 1 0 17.4 7"/></svg>',
    ac: '<svg viewBox="0 0 24 24"><path d="M12 3v18M4 7l16 10M20 7L4 17"/></svg>',
    up: '<svg viewBox="0 0 24 24"><path d="M12 19V6M6 12l6-6 6 6"/></svg>',
    dn: '<svg viewBox="0 0 24 24"><path d="M12 5v13M6 12l6 6 6-6"/></svg>',
    fan: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.2"/><path d="M12 10c0-4 1-6 3-6s2 3 0 4.6M14 12c4 0 6 1 6 3s-3 2-4.6 0M12 14c0 4-1 6-3 6s-2-3 0-4.6M10 12c-4 0-6-1-6-3s3-2 4.6 0"/></svg>',
    auto: '<svg viewBox="0 0 24 24"><path d="M4 18l5-12 5 12M5.6 14h6.8"/><path d="M17 6v8a4 4 0 0 0 4 0V6"/></svg>',
    mode: '<svg viewBox="0 0 24 24"><path d="M3 16c4-6 8-6 12 0"/><path d="M3 10c4-6 8-6 12 0"/><path d="M18 7l3 3-3 3"/></svg>',
    recirc: '<svg viewBox="0 0 24 24"><path d="M4 9h13a3 3 0 0 1 0 6h-3"/><path d="M7 12l-3-3 3-3"/><path d="M17 18l3-3-3-3"/></svg>',
    defrost: '<svg viewBox="0 0 24 24"><path d="M4 15c3-5 13-5 16 0"/><path d="M8 19v2M12 19v2M16 19v2"/></svg>',
    defog: '<svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="10" rx="2"/><path d="M7 19v2M12 19v2M17 19v2"/></svg>',
  };
  function buildHead(root) {
    const c = el('div', 'hvac');
    c.innerHTML = `
      <div class="disp">
        <div class="set" id="hd-set">--<small>°F</small></div>
        <div><div class="k">setpoint</div><div class="v" id="hd-setc">--</div></div>
        <div><div class="k">cabin</div><div class="v" id="hd-cabin">--</div></div>
        <div><div class="k">evap</div><div class="v" id="hd-evap">--</div></div>
        <div><div class="k">outside</div><div class="v" id="hd-amb">--</div></div>
        <div class="fanbars" id="hd-fan"></div>
      </div>
      <div class="grid" id="hd-grid"></div>
      <div class="kv-grid" id="hd-ro" style="margin-top:10px"></div>
      <div class="note" id="hd-note"></div>`;
    const grid = $('#hd-grid', c);
    const btn = (label, icon, fn, title, id) => { const b = el('button', 'hbtn'); b.insertAdjacentHTML('beforeend', icon); b.appendChild(el('span', null, label)); b.onclick = fn; b.title = title; if (id) b.id = id; grid.appendChild(b); return b; };
    btn('OFF', HI.off, () => setKnob('hvac_on', false), 'System off — hvac_on', 'hd-off');
    btn('TEMP ▲', HI.up, () => bump('hvac_setpoint_f', +1), 'Setpoint up 1 °F — hvac_setpoint_f');
    btn('TEMP ▼', HI.dn, () => bump('hvac_setpoint_f', -1), 'Setpoint down 1 °F — hvac_setpoint_f');
    btn('A/C', HI.ac, () => setKnob('hvac_ac_on', !KNOBVAL.hvac_ac_on), 'Compressor — hvac_ac_on', 'hd-ac');
    btn('FAN ▲', HI.fan, () => { if (!KNOBVAL.hvac_on) setKnob('hvac_on', true); bump('hvac_fan_speed', +1); }, 'Blower up — hvac_fan_speed (and hvac_on)', 'hd-fanup');
    btn('FAN ▼', HI.fan, () => bump('hvac_fan_speed', -1), 'Blower down — hvac_fan_speed');
    for (const [label, key, why] of INERT_HVAC) {
      const b = el('button', 'hbtn inert'); b.insertAdjacentHTML('beforeend', HI[key]);
      b.appendChild(el('span', null, label)); b.appendChild(el('span', 'blindmark', '⌀ not readable'));
      b.disabled = true; b.title = why; grid.appendChild(b);
    }
    $('#hd-note', c).textContent =
      'Dashed, dimmed buttons are real controls on the car that this project has walked and cannot read: AUTO, vent MODE and fresh/recirc moved nothing in HVAC groups 00/01/10/11 on 2026-08-24 (group 00 stayed 80 01 80 00), and the defrost/defog encodings are unresolved. They are drawn inert on purpose — hover for the entry, and see docs/SIGNALS.md. OVMS reads vent mode and intake from EV-CAN 0x54B, which needs the re-pinned cable.';
    root.appendChild(c);
  }
  function paintHead() {
    const set = document.getElementById('hd-set'); if (!set) return;
    const r = REC;
    const on = !!r.hvac_on;
    set.classList.toggle('off', !on);
    set.innerHTML = (typeof r.hvac_target_f === 'number' ? Math.round(r.hvac_target_f) : '--') + '<small>°F</small>';
    $('#hd-setc').textContent = typeof r.hvac_target_f === 'number' ? fmtTemp(r.hvac_target_c, r.hvac_target_f, { cDec: 0 }) : '--';
    $('#hd-cabin').textContent = typeof r.cabin_temp_c === 'number' ? fmtTemp(r.cabin_temp_c, r.cabin_temp_f, { cDec: 0 }) : '--';
    $('#hd-evap').textContent = typeof r.hvac_evap_c === 'number' ? fmtTemp(r.hvac_evap_c, r.hvac_evap_f, { cDec: 0 }) : '--';
    $('#hd-amb').textContent = typeof r.hvac_ambient_c === 'number' ? fmtTemp(r.hvac_ambient_c, r.hvac_ambient_f, { cDec: 0 }) : '--';
    const bars = $('#hd-fan'); bars.textContent = '';
    const n = Math.max(0, Math.min(7, Math.round(r.hvac_fan_speed || 0)));
    for (let i = 0; i < 7; i++) { const b = el('i', i < n && on ? 'on' : ''); b.style.height = (8 + i * 3) + 'px'; bars.appendChild(b); }
    $('#hd-off').classList.toggle('on', !on);
    $('#hd-ac').classList.toggle('on', !!r.hvac_ac_on);
    $('#hd-fanup').classList.toggle('on', on);
    const ro = $('#hd-ro'); ro.textContent = '';
    const kv = (k, v) => { const d = el('div', 'kv'); d.appendChild(el('div', 'kv-label', k)); d.appendChild(el('div', 'kv-val', v)); ro.appendChild(d); };
    if (r.hvac_heater_level != null) kv('Heater demand', r.hvac_heater_level ? String(r.hvac_heater_level) : 'off');
    if (r.hvac_compressor_rpm != null) kv('Compressor', r.hvac_ac_on ? r.hvac_compressor_rpm + ' RPM' : 'off');
    if (r.hvac_sunload != null) kv('Sunload', String(r.hvac_sunload));
    if (r.hvac_blower_v != null) kv('Blower', r.hvac_fan_on ? r.hvac_blower_v + ' V' : 'off');
    const w = r.loads_w || {};
    const hv = (w.ac || 0) + (w.ptc || 0) + (w.blower || 0);
    kv('HVAC draw', hv ? Math.round(hv) + ' W' : '0 W');
    // Where those watts come from. On the plug this is the whole point: the
    // climate system runs off the wall, so what reaches the pack is what is
    // left over.
    const p = r.power;
    if (p && p.charger_kw > 0) {
      kv('Of the charge', Math.round(p.charger_kw * 1000) + ' W in, '
        + Math.round(p.pack_kw * 1000) + ' W to the pack');
    } else if (p) {
      kv('Pack', fmtKw(p.pack_kw));
    }
  }

  // kW with the house sign spelled out: negative is out of the pack.
  function fmtKw(kw) {
    if (typeof kw !== 'number') return '—';
    const v = Math.round(kw * 100) / 100;
    return (v > 0 ? '+' : '') + v + ' kW';
  }

  // ── the power switch ──
  // A round push-button, drawn here in inline CSS (no new stylesheet, nothing
  // external) and wired to POST /sim/power. The model owns the rules — one
  // push cycles OFF -> ACC -> ON -> OFF, the brake is what reaches READY, a
  // latched charge connector refuses it — and this only reports what came
  // back. It is drawn only when the profile HAS an ignition knob, and it
  // removes itself if the control API answers 501: a car with a key has no
  // power switch to press.
  let BRAKE_HELD = false;
  const POWER_RING = {
    off: ['#2a3142', '#8892a6', 'OFF — one push for accessory'],
    acc: ['#c9922e', '#f0d9a8', 'ACC — accessory; push again for ON'],
    on: ['#3f8f4f', '#dff0e2', 'ON — push again for OFF, or hold the brake for READY'],
    ready: ['#48d16a', '#ffffff', 'READY — the car will move; push to switch off'],
  };
  function powerState() {
    return (HAVE_RECORD ? REC.start_state_name : REC.start_state) || KNOBVAL.start_state || 'off';
  }
  function buildPower(root) {
    root.hidden = false;
    root.setAttribute('style', 'display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 10px');
    const btn = el('button', null, '');
    btn.id = 'pw-btn';
    btn.type = 'button';
    btn.appendChild(el('b', null, 'POWER'));
    btn.appendChild(el('small', null, 'OFF'));
    btn.setAttribute('style',
      'width:86px;height:86px;border-radius:50%;cursor:pointer;display:flex;flex-direction:column;' +
      'align-items:center;justify-content:center;gap:2px;font:600 11px/1.1 system-ui,sans-serif;' +
      'letter-spacing:.08em;background:#141a26;color:#8892a6;border:4px solid #2a3142');
    btn.onclick = pressPower;
    root.appendChild(btn);
    const side = el('div');
    side.setAttribute('style', 'display:flex;flex-direction:column;gap:4px;font-size:12px');
    const lbl = el('label');
    lbl.setAttribute('style', 'display:flex;align-items:center;gap:6px;cursor:pointer');
    const box = el('input'); box.type = 'checkbox'; box.id = 'pw-brake';
    box.onchange = () => { BRAKE_HELD = box.checked; paintPower(); };
    lbl.appendChild(box);
    lbl.appendChild(el('span', null, 'hold the brake'));
    lbl.title = 'The brake pedal is what reaches READY on this car (Owner\'s Manual p. 5-8). ' +
      'With it up, a push only steps the switch through its positions.';
    side.appendChild(lbl);
    const msg = el('div', null, ''); msg.id = 'pw-msg';
    msg.setAttribute('style', 'opacity:.75;min-height:1.2em;max-width:22em');
    side.appendChild(msg);
    root.appendChild(side);
  }
  async function pressPower() {
    const btn = document.getElementById('pw-btn'); if (btn) btn.disabled = true;
    try {
      const out = await api('/sim/power', 'POST', { brake: BRAKE_HELD });
      const m = document.getElementById('pw-msg');
      if (m) { m.textContent = (out.accepted ? '' : 'refused — ') + (out.message || ''); }
      await tick();
    } catch (e) {
      if (e.status === 501) {           // a core with no power switch: take the button away
        const root = document.getElementById('power-switch');
        if (root) { root.textContent = ''; root.hidden = true; }
      }
    } finally { if (btn) btn.disabled = false; }
  }
  function paintPower() {
    const btn = document.getElementById('pw-btn'); if (!btn) return;
    const s = powerState();
    const [ring, ink, title] = POWER_RING[s] || POWER_RING.off;
    btn.style.borderColor = ring;
    btn.style.color = ink;
    btn.style.background = s === 'ready' ? '#173a22' : '#141a26';
    btn.style.boxShadow = s === 'ready' ? '0 0 14px rgba(72,209,106,.55)' : 'none';
    btn.querySelector('small').textContent = s.toUpperCase();
    const box = document.getElementById('pw-brake');
    if (box) box.checked = BRAKE_HELD;
    btn.title = title + (BRAKE_HELD ? ' · brake held: a push goes to READY' : '');
  }

  function buildSkin() {
    const cluster = HAVE_RECORD && REC.lamps && typeof REC.soc === 'number' && REC.speed_mph != null;
    const cc = cardOf('cluster');
    if (cc) { if (cluster) buildCluster($('#cluster-body')); else cc.remove(); }
    const pw = document.getElementById('power-switch');
    if (pw) { if (cluster && has('start_state')) buildPower(pw); else pw.remove(); }
    const head = HAVE_RECORD && REC.hvac_on != null && hasAll('hvac_on', 'hvac_ac_on', 'hvac_fan_speed', 'hvac_setpoint_f');
    const hc = cardOf('climate-head');
    if (hc) { if (head) buildHead($('#head-body')); else hc.remove(); }
  }

  // ── simulated time ──
  const SKIPS = [['10 s', 10], ['1 min', 60], ['10 min', 600], ['1 h', 3600], ['6 h', 21600]];
  const SCALES = [1, 10, 60, 600, 3600];
  function buildTimeCard() {
    const root = $('#time-body'); if (!root) return;
    root.innerHTML = `
      <div class="time-big" id="tm-clock">--<small>simulated s per real s</small></div>
      <div class="time-why" id="tm-src"></div>
      <div class="kv-label" style="margin-top:10px">Simulated time elapsed</div>
      <div class="kv-val" id="tm-elapsed">--</div>
      <div id="tm-scale-box" hidden>
        <div class="kv-label" style="margin-top:10px">Clock speed</div>
        <div class="time-row" id="tm-scale"></div>
        <div class="time-why locked" id="tm-lock" hidden></div>
      </div>
      <div class="kv-label" style="margin-top:10px">Skip ahead</div>
      <div class="time-row" id="tm-skip"></div>
      <div class="time-why">Advances the model instantly by this much simulated time, on top of the running clock.</div>`;
    const skip = $('#tm-skip', root);
    for (const [label, s] of SKIPS) {
      const b = el('button', null, label); b.title = `POST /sim/step {"sim_seconds": ${s}}`;
      b.onclick = async () => { try { await api('/sim/step', 'POST', { sim_seconds: s }); tick(); } catch (e) { /* toasted */ } };
      skip.appendChild(b);
    }
    // the clock-speed control exists only when the model has a clock knob
    const spec = SCHEMA.clock_scale;
    if (spec && isNumeric(spec.type)) {
      $('#tm-scale-box', root).hidden = false;
      const row = $('#tm-scale', root);
      for (const s of SCALES) {
        if (spec.min != null && s < spec.min) continue;
        if (spec.max != null && s > spec.max) continue;
        const b = el('button', null, s + '×'); b.dataset.scale = s; b.onclick = () => setKnob('clock_scale', s); row.appendChild(b);
      }
      const num = el('input'); num.type = 'number'; num.min = spec.min; num.max = spec.max; num.step = 'any'; num.title = labelOf('clock_scale', spec);
      num.onchange = () => setKnob('clock_scale', parseFloat(num.value));
      row.appendChild(num); row.appendChild(el('span', 'tu', '×'));
      HOOKS.push({ name: 'clock_scale', sync: v => { if (document.activeElement !== num) num.value = v; for (const b of $$('button[data-scale]', row)) b.classList.toggle('on', +b.dataset.scale === v); } });
    }
  }
  function paintTime() {
    const clock = document.getElementById('tm-clock'); if (!clock) return;
    const ts = INFO.time_scale;
    clock.innerHTML = (ts == null ? '--' : ts + '×') + '<small>simulated s per real s</small>';
    $('#tm-src').textContent = 'Source: ' + (INFO.time_scale_source || 'unknown') + (INFO.time_scale_max ? ` · clamped to ${INFO.time_scale_max}× at most` : '') + '.';
    const t = typeof REC.sim_t === 'number' ? REC.sim_t : (typeof REC.t === 'number' ? REC.t : null);
    $('#tm-elapsed').textContent = t == null ? '--' : fmtDur(t);
    const lock = document.getElementById('tm-lock');
    if (lock) {
      const locked = INFO.speed_override != null;
      lock.hidden = !locked;
      if (locked) lock.textContent = `Locked: this run was started with --speed ${INFO.speed_override}, which overrides the scenario's clock speed. Restart without it to change the clock here.`;
      for (const b of $$('#tm-scale button, #tm-scale input')) b.disabled = locked;
    }
  }
  function fmtDur(s) {
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), sec = Math.floor(s % 60);
    return (d ? d + 'd ' : '') + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
  }

  // ── live state ──
  const HIDE = new Set(['cells', 'temps', 'temps_c', 'temps_f', 'lamps', 'lamps_unmodelled', 'messages', 'loads_w', 'power', 'simulated', 'cell_groups', 'segment_deltas', 'balancing']);
  function paintState() {
    const root = $('#state-body'); if (!root) return;
    let kv = $('.kv-live', root), loads = $('.loads', root), raw = $('details.raw pre', root);
    if (!kv) {
      root.appendChild(el('div', 'kv-label', HAVE_RECORD ? 'record — what the dashboard decodes from this car' : 'state — this core has no record(); raw model state'));
      loads = el('div', 'loads'); root.appendChild(loads);
      kv = el('div', 'kv-live'); root.appendChild(kv);
      const det = el('details', 'raw'); det.appendChild(el('summary', null, 'raw JSON'));
      raw = el('pre'); det.appendChild(raw); root.appendChild(det);
      det.addEventListener('toggle', () => paintState());
    }
    const r = REC;
    const keys = Object.keys(r).filter(k => !HIDE.has(k)).sort();
    const shown = new Set();
    const frag = document.createDocumentFragment();
    for (const k of keys) {
      if (shown.has(k)) continue;
      let label = k, val;
      // a °C key with a °F twin is one temperature, shown the house way
      if (/_c$/.test(k) && typeof r[k] === 'number' && (k.slice(0, -2) + '_f') in r) { label = k.slice(0, -2); val = fmtTemp(r[k], r[k.slice(0, -2) + '_f'], { cDec: 1 }); shown.add(k.slice(0, -2) + '_f'); }
      else if (/_f$/.test(k) && (k.slice(0, -2) + '_c') in r) continue;
      else val = fmtVal(r[k]);
      const d = el('div'); d.appendChild(el('div', 'k', label.replace(/_/g, ' '))); d.appendChild(el('div', 'v', val)); d.title = k + ' = ' + fmtVal(r[k]);
      frag.appendChild(d);
    }
    kv.textContent = ''; kv.appendChild(frag);
    loads.textContent = '';
    // The budget, left to right, before the per-consumer chips: what the wall
    // meter sees, what the on-board unit puts out, what the car is spending,
    // and what is left for the pack. House sign: positive is INTO the pack.
    if (r.power && typeof r.power === 'object') {
      const p = r.power;
      const line = el('div', 'total');
      const part = (k, v, t) => { const s = el('span'); s.appendChild(document.createTextNode(k + ' ')); s.appendChild(el('b', null, v)); if (t) s.title = t; line.appendChild(s); };
      if (p.wall_kw) part('wall', fmtKw(p.wall_kw), 'What a meter at the wall would read: the DC power over the wall-to-pack efficiency.');
      if (p.charger_kw) part('→ charge', fmtKw(p.charger_kw), 'DC arriving in the car, after the SOC taper and any thermal derate.');
      part('→ loads', fmtKw(-p.loads_kw), 'Everything switched on right now — the chips below add up to this.');
      if (p.regen_kw) part('→ regen', fmtKw(p.regen_kw), 'Energy coming back in from the motor.');
      part('→ pack', fmtKw(p.pack_kw), 'What is left for the battery. Positive = into the pack, negative = out of it (the house sign rule).');
      loads.appendChild(line);
    }
    if (r.loads_w && typeof r.loads_w === 'object') {
      let total = 0;
      for (const [k, w] of Object.entries(r.loads_w)) { total += +w || 0; if (!w) continue; const s = el('span'); s.appendChild(document.createTextNode(k.replace(/_/g, ' ') + ' ')); s.appendChild(el('b', null, Math.round(w) + ' W')); loads.appendChild(s); }
      const tot = el('span', 'total'); tot.appendChild(document.createTextNode('loads ')); tot.appendChild(el('b', null, Math.round(total) + ' W')); loads.prepend(tot);
    }
    if (raw.closest('details').open) { const slim = {}; for (const k of Object.keys(r)) if (!HIDE.has(k) || k === 'lamps' || k === 'loads_w' || k === 'power' || k === 'messages') slim[k] = r[k]; raw.textContent = JSON.stringify(slim, null, 1); }
  }

  // ── boot ──
  async function boot() {
    CONTROL = await resolveControl();
    if (!CONTROL) { showNotice(null); return; }
    let s;
    try { s = await api('/sim/schema'); }
    catch (e) { showNotice(null, `A control API was expected at ${CONTROL} but did not answer (${e.message}). Start one:`); return; }
    SCHEMA = s.knobs || {};
    $('#notice').hidden = true;
    try { INFO = await api('/sim/info'); } catch (e) { INFO = {}; }
    try { KNOBVAL = (await api('/sim/knobs')).knobs || {}; } catch (e) { /* toasted */ }
    try { await fetchLive(); } catch (e) { REC = {}; }
    renderFacts(); buildToolbar();
    // cards must all exist — and the unusable ones be gone — before the grid measures them
    buildSkin();
    paintTiles(true);
    installHooks();
    buildKnobCards();
    buildTimeCard();
    paintCluster(); paintHead(); paintTime(); paintState(); paintPower();
    syncControls(); syncHooks();
    $('#simtiles').hidden = false;
    await TileStudio.init({
      gridId: 'simtiles',
      api: { signals: '/api/signals', tiles: '/api/sim/tiles', layouts: null },
      lsKey: 'hakake-sim-tiles-v1', lsKeyOld: 'hakake-sim-tiles-v0',
      discover: true,
    });
    TileStudio.update(REC);
    setInterval(tick, 1000);
    setInterval(refreshInfo, 5000);
  }
  boot().catch(e => toast('/sim', { error: String(e && e.message || e) }));
})();
