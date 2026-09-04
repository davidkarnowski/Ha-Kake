// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later

/* Tile Studio — user-configurable tiles for the Ha-Kake dashboard.
 *
 * Owns: tile config (order / enabled / span / type / colour options), the Tiles
 * menu, the per-tile ⋯ menu, "Add tile", drag-to-reorder, and the generic
 * renderers for any scalar signal in /api/signals. Built-in custom tiles
 * (shifter, tires, cells …) are static markup in the template belonging to the
 * vehicle profile that declares them; they keep their own renderers but get
 * size + menu here, and apply() hides any the active profile never asked for.
 *
 * Hooks from the page: TileStudio.update(data) after each /api/status,
 * TileStudio.history(rows) after each /api/history.
 */
(function () {
  // Parameterised so a second page (the simulator cockpit) can run the same
  // engine against its own grid element, API routes and storage key. Nothing
  // here runs until TileStudio.init() is called; the dashboard calls it with
  // no arguments and gets exactly the behaviour it always had.
  const DEFAULTS = {
    gridId: 'tiles',
    api: { signals: '/api/signals', tiles: '/api/tiles', layouts: '/api/layouts' },
    lsKey: 'hakake-tiles-v2', lsKeyOld: 'leaf-tiles-v2',
    discover: false,   // add any .card[data-tile] in the grid that the config does not mention
  };
  let OPTS = DEFAULTS, API = DEFAULTS.api;
  let grid = null;
  let REG = { signals: {}, colors: {}, types: {}, items: {} };
  let cfg = [];                      // tile entries in display order
  let lastData = null, lastHist = [];
  const SPANS = [2, 3, 4, 6, 8, 12];
  const GRAPH_TYPES = new Set(['line', 'area', 'bars']);

  // ── colour scales ───────────────────────────────────────────────────
  function hex2rgb(h) { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function mix(a, b, t) { return a.map((x, i) => Math.round(x + (b[i] - x) * t)); }
  function colorFor(v, scaleName, min, max, invert) {
    const sc = REG.colors[scaleName] || REG.colors.mono;
    if (v == null || !sc || !isFinite(v)) return 'var(--dim)';
    let t = (v - min) / ((max - min) || 1);
    t = Math.min(1, Math.max(0, t));
    if (invert) t = 1 - t;
    const stops = sc.stops;
    for (let i = 1; i < stops.length; i++) {
      if (t <= stops[i][0]) {
        const [p0, c0] = stops[i - 1], [p1, c1] = stops[i];
        const u = (t - p0) / ((p1 - p0) || 1);
        const [r, g, b] = mix(hex2rgb(c0), hex2rgb(c1), u);
        return `rgb(${r},${g},${b})`;
      }
    }
    return stops[stops.length - 1][1];
  }
  function gradientCss(scaleName) {
    const sc = REG.colors[scaleName]; if (!sc) return '';
    return `linear-gradient(to right, ${sc.stops.map(s => `${s[1]} ${Math.round(s[0] * 100)}%`).join(', ')})`;
  }

  // ── value access ────────────────────────────────────────────────────
  function getVal(data, key) {
    if (!data || !key) return null;
    if (key.includes('.')) { const [b, i] = key.split('.'); const seq = data[b]; return Array.isArray(seq) ? seq[+i] : null; }
    return data[key];
  }
  function fmt(v, dec) { return v == null ? '--' : Number(v).toFixed(dec == null ? 1 : dec); }
  function sigOf(t) { return REG.signals[t.signal] || null; }
  function effMin(t, s) { const o = t.opts || {}; return o.min != null && o.min !== '' ? +o.min : (s ? s.min : 0); }
  function effMax(t, s) { const o = t.opts || {}; return o.max != null && o.max !== '' ? +o.max : (s ? s.max : 100); }
  function effColor(t, s) { return (t.opts && t.opts.color) || (s && s.color) || 'mono'; }

  // ── renderers ───────────────────────────────────────────────────────
  const R = {};
  R.number = (t, s, v, col, alt) => `<div class="gt-num" style="color:${col}">${fmt(v, s.dec)}<small>${s.unit || ''}</small></div>${alt ? `<div class="gt-alt">${alt}</div>` : ''}`;
  R.text = (t, s, v) => `<div class="gt-text" style="color:var(--accent)">${v == null ? '--' : String(v).toUpperCase()}</div>`;
  R.lamp = (t, s, v) => `<div class="gt-lamp ${v ? 'on' : ''}"></div><div class="gt-sub">${v ? 'ON' : 'off'}</div>`;
  R.bar = (t, s, v, col, alt, pct) => `<div class="gt-val" style="color:${col}">${fmt(v, s.dec)}<small>${s.unit || ''}</small></div>
      <div class="gt-track"><div class="gt-fill" style="width:${(pct * 100).toFixed(1)}%;background:${col}"></div></div>
      <div class="gt-sub">${effMin(t, s)} – ${effMax(t, s)} ${s.unit || ''}${alt ? ' · ' + alt : ''}</div>`;
  R.thermo = (t, s, v, col, alt, pct) => `<div class="gt-thermo"><div class="tube"><div style="height:${(pct * 100).toFixed(1)}%;background:${col}"></div></div>
      <div><div class="gt-val" style="color:${col}">${fmt(v, s.dec)}<small>${s.unit || ''}</small></div><div class="gt-sub">${alt || ''}</div></div></div>`;
  R.ring = (t, s, v, col, alt, pct) => {
    const C = 2 * Math.PI * 42;
    return `<svg viewBox="0 0 100 100" style="max-width:170px">
      <circle cx="50" cy="50" r="42" fill="none" stroke="var(--border)" stroke-width="9"/>
      <circle cx="50" cy="50" r="42" fill="none" stroke="${col}" stroke-width="9" stroke-linecap="round"
        stroke-dasharray="${C}" stroke-dashoffset="${(C * (1 - pct)).toFixed(1)}" transform="rotate(-90 50 50)" style="transition:stroke-dashoffset .4s"/>
      <text x="50" y="50" text-anchor="middle" dominant-baseline="central" fill="${col}" font-size="19" font-weight="700">${fmt(v, s.dec)}</text>
      <text x="50" y="66" text-anchor="middle" fill="var(--dim)" font-size="8">${s.unit || ''}${alt ? ' · ' + alt : ''}</text></svg>`;
  };
  function polar(cx, cy, r, deg) { const a = (deg - 90) * Math.PI / 180; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; }
  function arcPath(cx, cy, r, a0, a1) { const [x0, y0] = polar(cx, cy, r, a0), [x1, y1] = polar(cx, cy, r, a1); const large = a1 - a0 > 180 ? 1 : 0; return `M${x0.toFixed(1)},${y0.toFixed(1)} A${r},${r} 0 ${large} 1 ${x1.toFixed(1)},${y1.toFixed(1)}`; }
  R.arc = (t, s, v, col, alt, pct) => {
    const a0 = -135, a1 = 135, a = a0 + (a1 - a0) * pct;
    const [nx, ny] = polar(60, 62, 40, a);
    let ticks = '';
    for (let i = 0; i <= 10; i++) { const [x1, y1] = polar(60, 62, 50, a0 + (a1 - a0) * i / 10), [x2, y2] = polar(60, 62, 55, a0 + (a1 - a0) * i / 10); ticks += `<line class="gt-tick" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`; }
    return `<svg viewBox="0 0 120 110" style="max-width:220px">
      <path d="${arcPath(60, 62, 46, a0, a1)}" fill="none" stroke="var(--border)" stroke-width="9" stroke-linecap="round"/>
      <path d="${arcPath(60, 62, 46, a0, Math.max(a0 + 0.1, a))}" fill="none" stroke="${col}" stroke-width="9" stroke-linecap="round"/>
      ${ticks}
      <line x1="60" y1="62" x2="${nx.toFixed(1)}" y2="${ny.toFixed(1)}" stroke="var(--text)" stroke-width="2.5" stroke-linecap="round"/>
      <circle cx="60" cy="62" r="4" fill="var(--text)"/>
      <text x="60" y="92" text-anchor="middle" fill="${col}" font-size="15" font-weight="700">${fmt(v, s.dec)}<tspan fill="var(--dim)" font-size="8" font-weight="400"> ${s.unit || ''}</tspan></text>
      <text x="60" y="104" text-anchor="middle" fill="var(--dim)" font-size="7">${alt || ''}</text></svg>`;
  };
  R.dial = (t, s, v, col, alt, pct) => {
    const a0 = -150, a1 = 150, a = a0 + (a1 - a0) * pct;
    const [nx, ny] = polar(60, 60, 38, a);
    let ticks = '';
    for (let i = 0; i <= 20; i++) { const big = i % 5 === 0; const ang = a0 + (a1 - a0) * i / 20; const [x1, y1] = polar(60, 60, big ? 42 : 46, ang), [x2, y2] = polar(60, 60, 50, ang); ticks += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${big ? 'var(--text)' : 'var(--border)'}" stroke-width="${big ? 2 : 1}"/>`; }
    const lo = effMin(t, s), hi = effMax(t, s);
    return `<svg viewBox="0 0 120 120" style="max-width:220px">
      <circle cx="60" cy="60" r="54" fill="var(--card2)" stroke="var(--border)" stroke-width="2"/>
      <path d="${arcPath(60, 60, 50, a0, a1)}" fill="none" stroke="${col}" stroke-width="3" opacity="0.35"/>
      ${ticks}
      <text class="gt-axis" x="26" y="104" text-anchor="middle">${lo}</text><text class="gt-axis" x="94" y="104" text-anchor="middle">${hi}</text>
      <line x1="60" y1="60" x2="${nx.toFixed(1)}" y2="${ny.toFixed(1)}" stroke="${col}" stroke-width="3" stroke-linecap="round"/>
      <circle cx="60" cy="60" r="5" fill="var(--text)"/>
      <text x="60" y="84" text-anchor="middle" fill="${col}" font-size="13" font-weight="700">${fmt(v, s.dec)}<tspan fill="var(--dim)" font-size="7" font-weight="400"> ${s.unit || ''}</tspan></text></svg>`;
  };
  R.battery = (t, s, v, col, alt, pct) => `<svg viewBox="0 0 120 60" style="max-width:220px">
      <rect x="4" y="10" width="100" height="40" rx="7" fill="var(--card2)" stroke="var(--border)" stroke-width="3"/>
      <rect x="106" y="22" width="9" height="16" rx="3" fill="var(--border)"/>
      <rect x="9" y="15" width="${(90 * pct).toFixed(1)}" height="30" rx="4" fill="${col}" style="transition:width .4s"/>
      <text x="54" y="35" text-anchor="middle" dominant-baseline="central" fill="var(--text)" font-size="14" font-weight="700" style="paint-order:stroke;stroke:var(--card);stroke-width:3px">${fmt(v, s.dec)} ${s.unit || ''}</text></svg>${alt ? `<div class="gt-sub">${alt}</div>` : ''}`;
  function graph(t, s, v, col, alt, kind) {
    const key = s.hist;
    if (!key) return `<div class="gt-sub">no history for ${s.label}</div>`;
    const range = +(t.opts && t.opts.range) || 60;
    const cutoff = range ? Date.now() - range * 60000 : 0;
    const rows = lastHist.filter(h => h[key] != null && (!cutoff || new Date(h.t) >= cutoff));
    if (rows.length < 2) return `<div class="gt-val" style="color:${col}">${fmt(v, s.dec)}<small>${s.unit || ''}</small></div><div class="gt-sub">collecting history…</div>`;
    const W = 300, H = 80, vals = rows.map(r => r[key]);
    const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
    const X = i => (i / (rows.length - 1)) * W, Y = y => H - ((y - lo) / span) * (H - 6) - 3;
    let body = '';
    if (kind === 'bars') {
      const bw = Math.max(1, W / rows.length - 1);
      body = vals.map((y, i) => `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${Y(y).toFixed(1)}" width="${bw.toFixed(1)}" height="${(H - Y(y)).toFixed(1)}" fill="${colorFor(y, effColor(t, s), effMin(t, s), effMax(t, s), t.opts && t.opts.invert)}" opacity="0.85"/>`).join('');
    } else {
      const d = vals.map((y, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(y).toFixed(1)).join('');
      if (kind === 'area') body += `<path d="${d}L${W},${H}L0,${H}Z" fill="${col}" opacity="0.18"/>`;
      body += `<path d="${d}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>`;
    }
    return `<div class="gt-val" style="color:${col}">${fmt(v, s.dec)}<small>${s.unit || ''}</small></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="max-width:100%;height:80px">${body}</svg>
      <div class="gt-sub" style="display:flex;justify-content:space-between;width:100%"><span>${fmt(lo, s.dec)}</span><span>${range ? range >= 1440 ? (range / 1440) + 'd' : range >= 60 ? (range / 60) + 'h' : range + 'm' : 'all'} · ${rows.length} pts</span><span>${fmt(hi, s.dec)}</span></div>`;
  }
  R.line = (t, s, v, col, alt) => graph(t, s, v, col, alt, 'line');
  R.area = (t, s, v, col, alt) => graph(t, s, v, col, alt, 'area');
  R.bars = (t, s, v, col, alt) => graph(t, s, v, col, alt, 'bars');

  function renderSignalTile(t) {
    const el = document.getElementById('gt-' + t.id); if (!el) return;
    const s = sigOf(t); if (!s) { el.innerHTML = '<div class="gt-sub">unknown signal</div>'; return; }
    const v = getVal(lastData, s.key);
    const min = effMin(t, s), max = effMax(t, s);
    const pct = v == null ? 0 : Math.min(1, Math.max(0, (v - min) / ((max - min) || 1)));
    const col = s.kind === 'number' ? colorFor(v, effColor(t, s), min, max, t.opts && t.opts.invert) : 'var(--accent)';
    const altV = s.alt ? getVal(lastData, s.alt) : null;
    const alt = altV != null ? `${fmt(altV, s.dec)} ${s.alt_unit || ''}` : '';
    let type = t.type || 'number';
    if (s.kind === 'text') type = 'text'; else if (s.kind === 'bool') type = 'lamp';
    el.innerHTML = (R[type] || R.number)(t, s, v, col, alt, pct);
  }

  // ── Layout: gridstack.js (vendored, MIT). Tiles carry x / y / span(w) / h ──
  const COLS = 12, CELL = 40, MARGIN = 8, MIN_W = 2, MIN_H = 2;
  let gs = null;                   // GridStack instance
  let applying = false;            // suppress 'change' persistence while we drive the grid
  function ensureGrid() {
    if (!grid) return null;                  // before init()
    if (gs || typeof GridStack === 'undefined') return gs;
    grid.classList.add('grid-stack');
    gs = GridStack.init({
      column: COLS, cellHeight: CELL, margin: MARGIN, float: false, animate: true,
      minRow: 1,
      handle: '.card-title, .history-header',
      draggable: { handle: '.card-title, .history-header', cancel: 'button, a, input, select, .tile-menu' },
      resizable: { handles: 'se' },
      columnOpts: { breakpoints: [{ w: 720, c: 1 }] },
    }, grid);
    gs.on('change', (ev, items) => {
      if (applying || !items) return;
      items.forEach(n => { const t = cfg.find(x => x.id === n.id); if (t) { t.x = n.x; t.y = n.y; t.span = n.w; t.h = n.h; } });
      save(); renderPanel();
    });
    gs.on('resizestop', (ev, el) => { const t = cfg.find(x => x.id === el.gridstackNode.id); if (t && t.kind === 'signal') renderSignalTile(t); });
    return gs;
  }
  function live() { return cfg.filter(t => t.enabled); }
  function wrapperOf(t) { return grid.querySelector(`.grid-stack-item[gs-id="${t.id}"]`); }
  function cardOf(t) { const w = wrapperOf(t); return w ? w.querySelector('.card') : grid.querySelector(`.card[data-tile="${t.id}"]`); }
  function measureRows(t, card) {
    // content height at the tile's current width → rows (used when a tile has no h yet, or "auto")
    const px = card.scrollHeight + 2;
    return Math.max(MIN_H, Math.ceil((px + MARGIN) / CELL));
  }

  function ensureCard(t) {
    let card = cardOf(t);
    if (!card && t.kind === 'signal') {
      const s = sigOf(t);
      card = document.createElement('div');
      card.className = 'card';
      card.dataset.tile = t.id;
      card.innerHTML = `<div class="card-title"><span class="ttl">${t.title || (s ? s.label : t.id)}</span> <span class="tile-age" data-age-for="${s ? s.item : ''}"></span></div><div class="gtile" id="gt-${t.id}"></div>`;
      grid.appendChild(card);
    }
    if (!card) return null;
    card.removeAttribute('draggable');
    if (!card.querySelector('.tile-menu-btn')) {
      const b = document.createElement('button');
      b.className = 'tile-menu-btn'; b.title = 'Tile options'; b.textContent = '⋯';
      b.addEventListener('click', e => { e.stopPropagation(); if (menuFor(t.id)) closeMenus(); else openTileMenu(t.id, card); });
      card.appendChild(b);
    }
    // wrap into a gridstack item once
    let w = card.closest('.grid-stack-item');
    if (!w) {
      w = document.createElement('div');
      w.className = 'grid-stack-item';
      w.setAttribute('gs-id', t.id);
      card.parentNode.insertBefore(w, card);
      w.appendChild(card);
      card.classList.add('grid-stack-item-content');
    }
    if (t.kind === 'signal') {
      const ttl = card.querySelector('.ttl'); const s = sigOf(t);
      if (ttl) ttl.textContent = t.title || (s ? s.label : t.id);
      const age = card.querySelector('.tile-age'); if (age && s) age.dataset.ageFor = s.item;
    }
    return card;
  }

  function apply() {
    const g = ensureGrid();
    cfg.forEach(t => ensureCard(t));
    // drop wrappers of deleted user tiles
    grid.querySelectorAll('.grid-stack-item').forEach(w => {
      if (!cfg.find(t => t.id === w.getAttribute('gs-id')) && w.querySelector('.gtile')) { if (g) g.removeWidget(w, true); else w.remove(); }
    });
    // Built-in tiles are static markup for whichever profile declares them
    // (today: the Leaf's SVG cards). A profile that does not — the Lancer has
    // TILES = [] — never gets them in its config, so hide them outright
    // instead of leaving eleven cards that will never take a value.
    if (cfg.length) grid.querySelectorAll('.card[data-tile]').forEach(c => {
      if (cfg.find(t => t.id === c.dataset.tile)) return;
      const w = c.closest('.grid-stack-item');
      if (w) { if (g && w.gridstackNode) g.removeWidget(w, false); w.style.display = 'none'; }
      else c.style.display = 'none';
    });
    if (!g) { cfg.forEach(t => { const c = cardOf(t); if (c) c.closest('.grid-stack-item').style.display = t.enabled ? '' : 'none'; }); renderPanel(); return; }
    applying = true;
    g.batchUpdate();
    cfg.forEach(t => {
      const w = wrapperOf(t); if (!w) return;
      t.span = Math.min(COLS, Math.max(MIN_W, +t.span || 4));
      const isWidget = !!w.gridstackNode;
      if (!t.enabled) {
        if (isWidget) g.removeWidget(w, false);
        w.style.display = 'none';
        return;
      }
      w.style.display = '';
      const opts = { id: t.id, w: t.span, h: t.h || MIN_H, minW: MIN_W, minH: MIN_H };
      if (t.x != null && t.y != null) { opts.x = t.x; opts.y = t.y; } else { opts.autoPosition = true; }
      if (!isWidget) g.makeWidget(w, opts); else g.update(w, opts);
    });
    g.batchUpdate(false);
    // measure tiles that never had a height, then read back positions gridstack chose
    cfg.forEach(t => {
      if (!t.enabled || t.h) return;
      const w = wrapperOf(t), c = cardOf(t); if (!w || !c) return;
      t.h = measureRows(t, c);
      g.update(w, { h: t.h });
    });
    cfg.forEach(t => { const w = wrapperOf(t); const n = w && w.gridstackNode; if (n) { t.x = n.x; t.y = n.y; t.span = n.w; t.h = n.h; } });
    applying = false;
    cfg.forEach(t => { if (t.kind === 'signal' && t.enabled) renderSignalTile(t); });
    renderPanel();
  }
  function setSize(t, w, h) {
    const g = ensureGrid(), el = wrapperOf(t); if (!g || !el) return;
    const o = {}; if (w != null) o.w = w; if (h != null) o.h = h;
    g.update(el, o);
  }

  // ── persistence ─────────────────────────────────────────────────────
  // web/tiles.json is the real store; localStorage is only the fallback for a
  // backend that did not answer. The key was renamed off 'leaf-' when the
  // project went multi-vehicle; the old key is read once so nobody's cached
  // layout disappears the first time they load after the rename.
  let LS_KEY = DEFAULTS.lsKey, LS_KEY_OLD = DEFAULTS.lsKeyOld;
  async function save() {
    const body = JSON.stringify({ tiles: cfg });
    try { localStorage.setItem(LS_KEY, body); localStorage.removeItem(LS_KEY_OLD); } catch (e) {}
    try { await fetch(API.tiles, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body }); } catch (e) {}
  }
  function cached() {
    const raw = localStorage.getItem(LS_KEY) || localStorage.getItem(LS_KEY_OLD);
    return JSON.parse(raw).tiles;
  }
  async function load() {
    if (!grid) return;                       // init() has not run on this page
    try { REG = await (await fetch(API.signals)).json(); } catch (e) {}
    try { cfg = (await (await fetch(API.tiles)).json()).tiles; }
    catch (e) { try { cfg = cached(); } catch (e2) { cfg = []; } }
    if (!Array.isArray(cfg)) cfg = [];
    if (OPTS.discover) discoverCards();
    apply();
    if (API.layouts) refreshLayouts();
  }
  // A page whose built-in cards are not known to the server (the simulator
  // cockpit) needs them placed anyway: apply() only ever hides orphans, it
  // never invents config entries. Append one per unmentioned card, in DOM
  // order, so gridstack auto-positions and measures them.
  function discoverCards() {
    grid.querySelectorAll('.card[data-tile]').forEach(c => {
      const id = c.dataset.tile;
      if (cfg.find(t => t.id === id)) return;
      cfg.push({ id, kind: 'builtin', enabled: true, span: +c.dataset.span || 4 });
    });
  }
  function resetTile(t) {
    // back to defaults: size from the registry, no position (auto-place), fresh height, plain look
    const d = (REG.tile_defaults || {})[t.id];
    if (t.kind === 'signal') { t.span = 3; t.type = 'number'; delete t.title; } else { t.span = d || 4; }
    delete t.x; delete t.y; delete t.h; t.opts = {}; t.enabled = true;
    const w = wrapperOf(t); const g = ensureGrid();
    if (g && w && w.gridstackNode) g.removeWidget(w, false);   // so apply() re-adds with autoPosition + measured height
    apply(); save();
  }

  // ── named layouts (web/layouts.json on the backend) ─────────────────
  async function refreshLayouts() {
    const sel = document.getElementById('layout-sel'); if (!sel) return;
    let names = [];
    try { names = (await (await fetch(API.layouts)).json()).layouts; } catch (e) {}
    sel.innerHTML = names.length ? names.map(l => `<option value="${l.name.replace(/"/g, '&quot;')}">${l.name} · ${l.tiles} tiles</option>`).join('') : '<option value="">(no saved layouts)</option>';
  }
  const $ = id => document.getElementById(id);
  if ($('layout-save')) $('layout-save').addEventListener('click', async () => {
    const name = ($('layout-name').value || '').trim();
    if (!name) { $('layout-name').focus(); return; }
    await save();                                       // make sure the backend has the latest positions
    const r = await fetch(API.layouts + '/' + encodeURIComponent(name), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (r.ok) { $('layout-name').value = ''; await refreshLayouts(); $('layout-sel').value = name; }
  });
  if ($('layout-load')) $('layout-load').addEventListener('click', async () => {
    const name = $('layout-sel').value; if (!name) return;
    const r = await fetch(API.layouts + '/' + encodeURIComponent(name) + '/load', { method: 'POST' });
    if (r.ok) await reloadLayout();
  });
  if ($('layout-del')) $('layout-del').addEventListener('click', async () => {
    const name = $('layout-sel').value; if (!name || !confirm(`Delete saved layout "${name}"?`)) return;
    await fetch(API.layouts + '/' + encodeURIComponent(name), { method: 'DELETE' });
    await refreshLayouts();
  });
  async function reloadLayout() {
    if (!grid) return;                       // before init()
    // tear every widget out of the grid so apply() re-places from the config, not from the DOM
    const g = ensureGrid();
    if (g) { applying = true; g.batchUpdate(); grid.querySelectorAll('.grid-stack-item').forEach(w => { if (w.gridstackNode) g.removeWidget(w, false); }); g.batchUpdate(false); applying = false; }
    grid.querySelectorAll('.grid-stack-item').forEach(w => { if (w.querySelector('.gtile')) w.remove(); });
    await load();
  }

  // ── Tiles panel (header menu) ───────────────────────────────────────
  function renderPanel() {
    const list = document.getElementById('tiles-list'); if (!list) return;
    list.innerHTML = '';
    cfg.slice().sort((a, b) => (a.y - b.y) || (a.x - b.x)).forEach(t => {
      const row = document.createElement('div');
      row.className = 'row';
      const s = sigOf(t);
      const name = t.kind === 'signal' ? (t.title || (s ? s.label : t.id)) + ` <span class="items">(${REG.types[t.type] || t.type})</span>` : t.name || t.id;
      row.innerHTML = `<input type="checkbox" ${t.enabled ? 'checked' : ''}><label>${name}<div class="items">${(t.items || (s ? [s.item] : [])).join(', ')} · ${t.span}×${t.h || '?'}</div></label><span class="items">⋯</span>`;
      row.querySelector('input').addEventListener('change', e => { t.enabled = e.target.checked; if (t.enabled) { delete t.x; delete t.y; } apply(); save(); });
      row.querySelector('label').addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); const el = grid.querySelector(`[data-tile="${t.id}"]`); if (el && t.enabled) { if (panel) panel.classList.remove('open'); openTileMenu(t.id, el); } });
      list.appendChild(row);
    });
    const add = document.createElement('div');
    add.className = 'row';
    add.style.marginTop = '8px';
    const sigs = Object.entries(REG.signals).sort((a, b) => a[1].label.localeCompare(b[1].label));
    add.innerHTML = `<select id="add-sig" style="flex:1;min-width:0;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px">${sigs.map(([k, s]) => `<option value="${k}">${s.label}</option>`).join('')}</select>
      <select id="add-type" style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px">${Object.entries(REG.types).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select>
      <button id="add-tile" title="add tile" style="width:auto;padding:0 8px">＋ add</button>`;
    add.querySelector('#add-tile').addEventListener('click', () => {
      const id = 'u' + Date.now().toString(36);
      cfg.push({ id, kind: 'signal', signal: add.querySelector('#add-sig').value, type: add.querySelector('#add-type').value, span: 3, enabled: true, opts: {} });
      apply(); save();
    });
    list.appendChild(add);
  }

  // ── per-tile menu ───────────────────────────────────────────────────
  // The menu lives on document.body, position: fixed, anchored to the tile's
  // ⋯ button — never inside the card, whose overflow: auto clipped it as soon
  // as the tile was narrower or shorter than the menu. placeMenu() keeps it
  // inside the viewport (MENU_MARGIN from every edge; below the button, or
  // above it when there is no room below, or pinned to the bottom edge when
  // neither fits — the CSS max-height then scrolls it internally). While a
  // menu is open, trackMenu() re-places it every animation frame, which
  // covers page scroll, window resize and gridstack's 0.3 s move/resize
  // animation after a "wide"/"tall" click; it closes the menu if its tile
  // disappears (hidden, removed, layout reloaded).
  const MENU_GAP = 6, MENU_MARGIN = 8;
  function menuFor(id) { return document.querySelector(`.tile-menu[data-for="${id}"]`); }
  function closeMenus() { document.querySelectorAll('.tile-menu').forEach(m => m.remove()); }
  function placeMenu(m, btn) {
    const r = btn.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const mw = m.offsetWidth, mh = m.offsetHeight;
    let left = r.right - mw;                                   // right edge under the ⋯ button
    left = Math.max(MENU_MARGIN, Math.min(left, vw - MENU_MARGIN - mw));
    let top = r.bottom + MENU_GAP;                             // below the button …
    if (top + mh > vh - MENU_MARGIN) {                         // … or above it, or pinned to the bottom edge
      const above = r.top - MENU_GAP - mh;
      top = above >= MENU_MARGIN ? above : Math.max(MENU_MARGIN, vh - MENU_MARGIN - mh);
    }
    m.style.left = Math.round(left) + 'px';
    m.style.top = Math.round(top) + 'px';
  }
  function trackMenu(m, btn) {
    let last = '';
    const tick = () => {
      if (!m.isConnected) return;                              // closed — stop tracking
      const r = btn.getBoundingClientRect();
      if (!btn.isConnected || (r.width === 0 && r.height === 0)) { m.remove(); return; }
      const key = [r.left, r.top, r.width, window.innerWidth, window.innerHeight, m.offsetHeight].join();
      if (key !== last) { last = key; placeMenu(m, btn); }
      requestAnimationFrame(tick);
    };
    tick();
  }
  function openTileMenu(id, card) {
    closeMenus();
    const t = cfg.find(x => x.id === id); if (!t) return;
    const s = sigOf(t); const isSig = t.kind === 'signal';
    const o = t.opts = t.opts || {};
    const btn = card.querySelector('.tile-menu-btn') || card;
    const m = document.createElement('div');
    m.className = 'tile-menu';
    m.dataset.for = id;
    const colorOpts = Object.entries(REG.colors).map(([k, v]) => `<option value="${k}" ${effColor(t, s) === k ? 'selected' : ''}>${v.label}</option>`).join('');
    const typeOpts = Object.entries(REG.types).filter(([k]) => !s || s.kind === 'number' ? true : (s.kind === 'text' ? k === 'text' : k === 'lamp')).map(([k, v]) => `<option value="${k}" ${(t.type || 'number') === k ? 'selected' : ''}>${v}</option>`).join('');
    const sigOpts = Object.entries(REG.signals).sort((a, b) => a[1].label.localeCompare(b[1].label)).map(([k, v]) => `<option value="${k}" ${t.signal === k ? 'selected' : ''}>${v.label}</option>`).join('');
    m.innerHTML = `
      <h5>Size — or drag the corner</h5>
      <div class="row seg">${SPANS.map(n => `<button data-span="${n}" class="${t.span == n ? 'on' : ''}">${n}</button>`).join('')}<span style="color:var(--dim)">wide</span></div>
      <div class="row seg" style="margin-top:4px"><button data-h="auto">auto</button>${[4, 6, 8, 10, 14].map(n => `<button data-h="${n}" class="${t.h == n ? 'on' : ''}">${n}</button>`).join('')}<span style="color:var(--dim)">tall</span></div>
      ${isSig ? `<h5>Signal</h5><div class="row"><select id="tm-sig">${sigOpts}</select></div>
      <h5>Display</h5><div class="row"><select id="tm-type">${typeOpts}</select></div>
      <div class="row"><label>Title</label><input type="text" id="tm-title" value="${(t.title || '').replace(/"/g, '&quot;')}" placeholder="${s ? s.label : ''}"></div>` : ''}
      ${!s || s.kind === 'number' ? `<h5>Colour</h5><div class="row"><select id="tm-color">${colorOpts}</select><label style="min-width:0"><input type="checkbox" id="tm-invert" ${o.invert ? 'checked' : ''}> invert</label></div>
      <span class="swatch" id="tm-swatch" style="background:${gradientCss(effColor(t, s))}"></span>
      <div class="row" style="margin-top:6px"><label>Range</label><input type="number" id="tm-min" value="${o.min != null ? o.min : ''}" placeholder="${s ? s.min : ''}"> – <input type="number" id="tm-max" value="${o.max != null ? o.max : ''}" placeholder="${s ? s.max : ''}"></div>` : ''}
      ${isSig ? `<div class="row" id="tm-graph-row" style="${GRAPH_TYPES.has(t.type) ? '' : 'display:none'}"><label>History</label><select id="tm-range">${[[5, '5 min'], [15, '15 min'], [60, '1 h'], [360, '6 h'], [1440, '24 h'], [10080, '7 d'], [0, 'all']].map(([v, l]) => `<option value="${v}" ${(+o.range || 60) === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>` : ''}
      <div class="foot"><button id="tm-hide">${t.enabled ? 'hide tile' : 'show tile'}</button>
      <button id="tm-reset" title="default size, style, colours and position">reset tile</button>
      ${isSig ? '<button id="tm-remove" class="danger">remove</button>' : ''}
      <button id="tm-done" class="primary" title="changes apply as you make them — this just closes the menu">Done</button></div>`;
    document.body.appendChild(m);
    m.addEventListener('click', e => e.stopPropagation());
    m.addEventListener('pointerdown', e => e.stopPropagation());
    placeMenu(m, btn);
    trackMenu(m, btn);
    m.querySelectorAll('[data-span]').forEach(b => b.addEventListener('click', () => { m.querySelectorAll('[data-span]').forEach(x => x.classList.toggle('on', x === b)); setSize(t, +b.dataset.span, null); if (isSig) renderSignalTile(t); }));
    m.querySelectorAll('[data-h]').forEach(b => b.addEventListener('click', () => { m.querySelectorAll('[data-h]').forEach(x => x.classList.toggle('on', x === b)); const h = b.dataset.h === 'auto' ? measureRows(t, card) : +b.dataset.h; setSize(t, null, h); }));
    const on = (sel, ev, fn) => { const el = m.querySelector(sel); if (el) el.addEventListener(ev, fn); };
    on('#tm-sig', 'change', e => { t.signal = e.target.value; const ns = sigOf(t); if (ns && ns.kind !== 'number') t.type = ns.kind === 'text' ? 'text' : 'lamp'; delete o.min; delete o.max; delete o.color; apply(); save(); openTileMenu(id, card); });
    on('#tm-type', 'change', e => { t.type = e.target.value; const gr = m.querySelector('#tm-graph-row'); if (gr) gr.style.display = GRAPH_TYPES.has(t.type) ? '' : 'none'; renderSignalTile(t); save(); });
    on('#tm-title', 'input', e => { t.title = e.target.value; ensureCard(t); save(); });
    on('#tm-color', 'change', e => { o.color = e.target.value; m.querySelector('#tm-swatch').style.background = gradientCss(o.color); renderSignalTile(t); save(); });
    on('#tm-invert', 'change', e => { o.invert = e.target.checked; renderSignalTile(t); save(); });
    on('#tm-min', 'input', e => { o.min = e.target.value === '' ? undefined : +e.target.value; renderSignalTile(t); save(); });
    on('#tm-max', 'input', e => { o.max = e.target.value === '' ? undefined : +e.target.value; renderSignalTile(t); save(); });
    on('#tm-range', 'change', e => { o.range = +e.target.value; renderSignalTile(t); save(); });
    on('#tm-hide', 'click', () => { t.enabled = !t.enabled; if (t.enabled) { delete t.x; delete t.y; } closeMenus(); apply(); save(); });
    on('#tm-reset', 'click', () => { closeMenus(); resetTile(t); });
    on('#tm-remove', 'click', () => { cfg = cfg.filter(x => x.id !== id); closeMenus(); apply(); save(); });
    on('#tm-done', 'click', closeMenus);
  }
  document.addEventListener('click', e => { if (!e.target.closest('.tile-menu')) closeMenus(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.querySelector('.tile-menu')) { closeMenus(); e.preventDefault(); } });

  // ── header controls ─────────────────────────────────────────────────
  const btn = document.getElementById('tiles-btn'), panel = document.getElementById('tiles-panel');
  if (btn) btn.addEventListener('click', () => panel.classList.toggle('open'));
  if (panel) document.addEventListener('click', e => { if (!e.target.closest('.tiles-menu')) panel.classList.remove('open'); });
  const reset = document.getElementById('tiles-reset');
  if (reset) reset.addEventListener('click', async () => {
    if (!confirm('Reset to the default arrangement? Saved layouts are kept.')) return;
    await fetch(API.tiles, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tiles: [] }) });
    await reloadLayout();
  });

  // init(opts): bind to a grid element and API routes, then load. Call once
  // per page. Options merge over DEFAULTS; api.layouts = null hides the
  // saved-layouts UI and skips those routes entirely.
  function init(opts) {
    OPTS = Object.assign({}, DEFAULTS, opts || {});
    API = Object.assign({}, DEFAULTS.api, (opts && opts.api) || {});
    LS_KEY = OPTS.lsKey; LS_KEY_OLD = OPTS.lsKeyOld;
    grid = document.getElementById(OPTS.gridId);
    if (!grid) { console.warn('TileStudio.init: no element #' + OPTS.gridId); return; }
    return load();
  }
  window.TileStudio = {
    init,
    update(data) { lastData = data; cfg.forEach(t => { if (t.kind === 'signal' && t.enabled) renderSignalTile(t); }); },
    history(rows) { lastHist = rows || []; cfg.forEach(t => { if (t.kind === 'signal' && t.enabled && GRAPH_TYPES.has(t.type)) renderSignalTile(t); }); },
    reload: reloadLayout,
  };
})();
