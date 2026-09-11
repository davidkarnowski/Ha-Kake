// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The raw output console tile (docs/CONSOLE.md).
//
// A terminal for whatever the transport is saying: broadcast frames, UDS answers
// grouped with the request that asked, adapter replies, text signals as they
// change, and reader events. It polls GET /api/console from the cursor it was
// last handed, so it never asks for everything and never asks twice.
//
// This is a *window*, not a capture tool — record_session.py and the MQTT bridge
// are the capture tools. The pane says so, and it says what it is dropping.
//
// Shape: the pure half (tokens, the changed-byte mask, the copy text) is exported
// on window.RawConsole and node-tested; the DOM half only runs in a browser.
(function () {
  'use strict';

  var POLL_MS = 1000;          // a few hertz is plenty: the reader has already decimated
  var MAX_LINES = 1500;        // lines kept in the DOM; the ring behind it is bounded too
  var KIND_LABEL = { frame: 'frames', uds: 'UDS', adapter: 'adapter', text: 'values', event: 'events' };

  // ── the pure half ───────────────────────────────────────────────────

  // "421 08 00 00" → ["421", "08", "00", "00"]. Anything that is not a frame
  // (an event, a UDS line with an arrow in it) tokenises harmlessly and simply
  // never matches the previous line's tokens.
  function tokens(text) {
    return String(text == null ? '' : text).trim().split(/\s+/).filter(Boolean);
  }

  // Which data bytes differ from that id's previous frame. Returns one boolean
  // per byte (the id is not a byte, so it is not in the mask). A first sighting
  // highlights nothing: everything would be "changed", which says nothing.
  function diffMask(prev, cur) {
    var a = tokens(prev).slice(1), b = tokens(cur).slice(1);
    if (!prev) return b.map(function () { return false; });
    return b.map(function (x, i) { return i >= a.length || a[i] !== x; });
  }

  // What a click puts on the clipboard: the line exactly as the transport gave
  // it, which for a frame is `ID B0 B1 …` — paste-able into a fixture.
  function copyText(entry) {
    return String((entry && entry.text) || '');
  }

  // The query the tile asks with. `since` is opaque: it came from the server.
  function query(state) {
    var p = ['since=' + encodeURIComponent(state.cursor || 0), 'limit=' + (state.limit || 300)];
    var kinds = Object.keys(state.kinds || {}).filter(function (k) { return state.kinds[k]; });
    if (kinds.length && kinds.length < Object.keys(state.kinds).length) p.push('kind=' + kinds.join(','));
    var ids = idList(state.idFilter);
    if (ids.length) p.push('ids=' + ids.join(','));
    return '/api/console?' + p.join('&');
  }

  function idList(raw) {
    return String(raw || '').split(/[\s,]+/).map(function (s) { return s.trim().toUpperCase(); })
      .filter(Boolean);
  }

  // "12 of 5,201 frames shown — 5,189 dropped (1DB 3,200 · 421 1,100)"
  function statsLine(stats) {
    if (!stats || !stats.on) return 'off';
    var bits = [];
    bits.push(num(stats.kept || 0) + ' shown');
    if (stats.dropped) {
      var by = Object.keys(stats.dropped_by_id || {}).slice(0, 3).map(function (id) {
        return id + ' ' + num(stats.dropped_by_id[id]);
      }).join(' · ');
      bits.push(num(stats.dropped) + ' dropped' + (by ? ' (' + by + ')' : ''));
    }
    if (stats.everything) bits.push('every id — lossy');
    else if (stats.rate_cap) bits.push('max ' + stats.rate_cap + '/s per id');
    return bits.join(' — ');
  }

  function num(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }

  function clock(wall) {
    var d = new Date((wall || 0) * 1000);
    return isNaN(d.getTime()) ? '--:--:--' :
      [d.getHours(), d.getMinutes(), d.getSeconds()].map(function (x) {
        return String(x).padStart(2, '0');
      }).join(':') + '.' + String(d.getMilliseconds()).padStart(3, '0');
  }

  var API = { tokens: tokens, diffMask: diffMask, copyText: copyText, query: query,
              idList: idList, statsLine: statsLine, clock: clock, KINDS: Object.keys(KIND_LABEL) };
  if (typeof window !== 'undefined') window.RawConsole = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;

  // ── the DOM half ────────────────────────────────────────────────────

  if (typeof document === 'undefined') return;

  var state = { cursor: 0, paused: false, kinds: {}, idFilter: '', known: false,
                limit: 300, last: {}, knownIds: null, timer: null };
  var el = {};

  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    el.card = document.querySelector('.card[data-tile="console"]');
    if (!el.card) return;                       // a page without the tile (the cockpit)
    el.out = document.getElementById('console-out');
    el.empty = document.getElementById('console-empty');
    el.pause = document.getElementById('console-pause');
    el.kinds = document.getElementById('console-kinds');
    el.ids = document.getElementById('console-ids');
    el.known = document.getElementById('console-known');
    el.stats = document.getElementById('console-stats');
    el.partial = document.getElementById('console-partial');
    el.scope = document.getElementById('console-scope');

    API.KINDS.forEach(function (k) {
      state.kinds[k] = true;
      var lab = document.createElement('label');
      lab.innerHTML = '<input type="checkbox" checked data-kind="' + k + '"> ' + KIND_LABEL[k];
      lab.querySelector('input').addEventListener('change', function (e) {
        // The kind filter goes to the server — it is the one filter that changes
        // how much travels — so the window is re-read from the start of the file
        // rather than left half full of what the old filter happened to fetch.
        state.kinds[k] = e.target.checked;
        rows = [];
        state.cursor = 0;
        poll();
      });
      el.kinds.appendChild(lab);
    });

    el.pause.addEventListener('click', function () {
      state.paused = !state.paused;
      el.pause.textContent = state.paused ? 'Resume' : 'Pause';
      el.pause.classList.toggle('on', state.paused);
      if (!state.paused) { scrollToEnd(); poll(); }
    });
    el.ids.addEventListener('input', function () { state.idFilter = el.ids.value; redraw(); });
    el.known.addEventListener('change', function () { state.known = el.known.checked; redraw(); });
    el.out.addEventListener('click', function (e) {
      // A drag-select ends in a click, and copying the whole line then would
      // throw away the selection the person just made by hand. Click-to-copy is
      // the convenience; selecting text with the mouse still works normally.
      var sel = window.getSelection && window.getSelection();
      if (sel && !sel.isCollapsed && String(sel)) return;
      var line = e.target.closest ? e.target.closest('.console-line') : null;
      if (line) copy(line);
    });

    // The known-id list is the profile's own: every item's id on the wire, which
    // /api/signals reports as `can_id`. Nothing here knows what a Leaf is.
    fetch('/api/signals').then(function (r) { return r.json(); }).then(function (reg) {
      state.knownIds = {};
      Object.keys(reg.items || {}).forEach(function (k) {
        var id = (reg.items[k] || {}).can_id;
        if (id) state.knownIds[String(id).toUpperCase()] = true;
      });
    }).catch(function () { state.knownIds = {}; });

    document.addEventListener('tiles:applied', schedule);
    schedule();
  });

  function on() {
    return !window.TileStudio || window.TileStudio.enabled('console');
  }

  function schedule() {
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(poll, POLL_MS);
    poll();
  }

  var rows = [];            // everything fetched this session, newest last

  function poll() {
    if (!on() || state.paused) return;          // a disabled tile asks for nothing
    // The id filter is deliberately NOT sent: typing one should not refetch, and
    // the lines already on screen are the ones being looked through.
    fetch(query({ cursor: state.cursor, limit: state.limit, kinds: state.kinds, idFilter: '' }))
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (body.cursor) state.cursor = body.cursor;
        (body.entries || []).forEach(function (e) { rows.push(e); });
        if (rows.length > MAX_LINES) rows = rows.slice(-MAX_LINES);
        paint(body.stats || {});
      })
      .catch(function () { /* the dashboard's own status dot reports the outage */ });
  }

  function visible(e) {
    if (!state.kinds[e.kind]) return false;     // in case one is still in flight
    var ids = idList(state.idFilter);
    if (ids.length && ids.indexOf(String(e.id || '').toUpperCase()) < 0) return false;
    if (state.known && state.knownIds && e.kind === 'frame'
        && !state.knownIds[String(e.id || '').toUpperCase()]) return false;
    return true;
  }

  function redraw() { paint(null); }

  function paint(stats) {
    if (!el.out) return;
    var atEnd = el.out.scrollTop + el.out.clientHeight >= el.out.scrollHeight - 24;
    var shown = rows.filter(visible);
    el.out.innerHTML = '';
    var prev = {};
    shown.forEach(function (e) {
      el.out.appendChild(lineOf(e, prev));
      if (e.kind === 'frame' && e.id) prev[e.id] = e.text;
    });
    if (el.empty) el.empty.hidden = shown.length > 0;
    if (!shown.length) el.out.appendChild(el.empty || document.createTextNode(''));
    if (stats) {
      if (el.stats) el.stats.textContent = statsLine(stats);
      if (el.scope) el.scope.textContent = stats.on
        ? (stats.everything ? 'every id on the bus' : (stats.ids || []).length + ' polled ids')
        : 'the reader has not armed it yet';
      if (el.partial) {
        el.partial.textContent = stats.partial || '';
        el.partial.hidden = !stats.partial;
      }
    }
    if (!state.paused && atEnd) scrollToEnd();
  }

  function lineOf(e, prev) {
    var div = document.createElement('div');
    div.className = 'console-line k-' + e.kind;
    div.dataset.copy = copyText(e);
    var t = document.createElement('span');
    t.className = 'c-t';
    t.textContent = clock(e.wall);
    var k = document.createElement('span');
    k.className = 'c-k';
    k.textContent = e.kind;
    var body = document.createElement('span');
    body.className = 'c-b';
    if (e.kind === 'frame' && e.id) {
      var mask = diffMask(prev[e.id], e.text), parts = tokens(e.text);
      var id = document.createElement('b');
      id.textContent = parts[0];
      body.appendChild(id);
      parts.slice(1).forEach(function (byte, i) {
        var s = document.createElement('i');
        s.className = mask[i] ? 'chg' : '';
        s.textContent = ' ' + byte;
        body.appendChild(s);
      });
    } else {
      body.textContent = e.text;
    }
    div.append(t, k, body);
    return div;
  }

  function scrollToEnd() { if (el.out) el.out.scrollTop = el.out.scrollHeight; }

  function copy(line) {
    var text = line.dataset.copy || line.textContent;
    var done = function () {
      line.classList.add('copied');
      setTimeout(function () { line.classList.remove('copied'); }, 700);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
    } else {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (err) { /* nothing to do */ }
      document.body.removeChild(ta);
    }
  }
})();
