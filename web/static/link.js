// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Link health tile (docs/ARCHITECTURE.md "Link accounting").
//
// A reader that reaches its car over a CAN bus or a broker keeps an account of
// every request: which reply was taken as the answer and on what evidence, where
// the round trip went, and what became of every other reply that turned up. That
// account rides in each record as `link`, and the marks it puts on values as
// `quality`. This tile shows both, and it marks the values themselves wherever an
// element carries `data-key`.
//
// It asks the server for nothing: everything it draws is in the record the page
// already has. The bytes behind each line are in the reply ledger, GET /api/ledger.
//
// Shape: the pure half (rows, totals, marks, formatting) is exported on
// window.LinkTile and node-tested; the DOM half only runs in a browser.
(function () {
  'use strict';

  // What each count means, in the order worth reading. Anything the reader counts
  // that is not listed here is still shown, under its own name: nothing is hidden.
  var LABELS = [
    ['late', 'late replies'], ['late_credited', 'credited to their item'], ['foreign', 'foreign replies'],
    ['timeouts', 'requests given up on'], ['superseded', 'not run: overtaken'], ['duplicate', 'not run: repeated'],
    ['expired', 'expired on the bridge'], ['unproven', 'answers not proven'], ['negative', 'refused by the ECU'],
    ['fragments', 'fragments'], ['flagged', 'values out of limits'], ['mirror_mismatch', 'mirror disagreed'],
    ['late_acks', 'late acks without a reply'], ['late_superseded', 'late: overtaken'], ['late_failed', 'late: failed'],
  ];
  var QUIET = { sent: 1, ok: 1, mirrored_other: 1 };            // the ordinary business of a working link
  var STATE_TEXT = { ok: 'ok', late: 'late', timeout: 'no answer', negative: 'refused by ECU' };
  var MARK_TEXT = { 'late': 'arrived late', 'out-of-limits': 'outside its physical limits' };

  // ── the pure half ───────────────────────────────────────────────────

  function ms(v) {
    if (v == null || !isFinite(+v)) return '';
    var n = +v;
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + ' s' : Math.round(n) + ' ms';
  }

  // The newest entry per item, kept across cycles: a record carries only the
  // requests of its own cycle, and a table that forgot the rest would flicker.
  function merge(prev, items) {
    var out = {}, k;
    for (k in prev || {}) out[k] = prev[k];
    for (k in items || {}) out[k] = items[k];
    return out;
  }

  function rows(items) {
    return Object.keys(items || {}).sort().map(function (id) {
      var e = items[id] || {};
      var state = STATE_TEXT[e.state] || e.state || '';
      if (e.late_s != null) state = (e.state === 'late' ? 'late' : state + ', late') + ' ' + (+e.late_s).toFixed(1) + ' s';
      return { item: id, state: state, bad: !!e.state && e.state !== 'ok', rtt: ms(e.rtt_ms), net: ms(e.net_ms),
               queue: ms(e.queue_ms), bus: ms(e.bus_ms), via: e.via || '' };
    });
  }

  // "ok 5210 of 5214 · late replies 3 · requests given up on 4"
  function totals(link) {
    var t = (link && link.totals) || {}, out = [];
    if (t.sent) out.push({ key: 'ok', label: 'answered', n: (t.ok || 0) + ' of ' + t.sent });
    var seen = {};
    LABELS.forEach(function (p) { seen[p[0]] = 1; if (t[p[0]]) out.push({ key: p[0], label: p[1], n: String(t[p[0]]) }); });
    Object.keys(t).sort().forEach(function (k) {
      if (!seen[k] && !QUIET[k] && t[k]) out.push({ key: k, label: k.replace(/_/g, ' '), n: String(t[k]) });
    });
    return out;
  }

  function proof(link) {
    if (!link || !link.via) return '';
    return link.via === 'ack' ? 'replies identified by the bridge' : 'replies picked by their echo';
  }

  function marks(quality) {
    return Object.keys(quality || {}).sort().map(function (k) {
      return { key: k, mark: quality[k], text: MARK_TEXT[quality[k]] || quality[k] };
    });
  }

  // "soc" marks an element whose data-key is "soc"; "temps_f.0" is marked by "temps_f" as well.
  function markFor(quality, key) {
    if (!quality || !key) return null;
    return quality[key] || quality[String(key).split('.')[0]] || null;
  }

  // ── the DOM half ────────────────────────────────────────────────────

  var known = {};

  function cell(doc, tr, text, cls) {
    var td = doc.createElement('td');
    td.textContent = text;
    if (cls) td.className = cls;
    tr.appendChild(td);
  }

  function mark(doc, quality) {
    var els = doc.querySelectorAll('[data-key]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i], m = markFor(quality, el.getAttribute('data-key'));
      el.classList.toggle('q-late', m === 'late');
      el.classList.toggle('q-out-of-limits', m === 'out-of-limits');
      if (m) el.setAttribute('title', MARK_TEXT[m] || m);
      else if (el.classList.contains('q-marked')) el.removeAttribute('title');
      el.classList.toggle('q-marked', !!m);
    }
  }

  function render(doc, data) {
    data = data || {};
    mark(doc, data.quality);
    var card = doc.querySelector('[data-tile="link"]');
    if (!card) return;
    var link = data.link, has = !!(link && link.totals && link.totals.sent);
    var empty = doc.getElementById('link-empty'), table = doc.getElementById('link-items');
    empty.hidden = has;
    table.hidden = !has;
    doc.getElementById('link-proof').textContent = proof(link);
    if (has) {
      known = merge(known, link.items);
      var body = doc.getElementById('link-rows');
      body.textContent = '';
      rows(known).forEach(function (r) {
        var tr = doc.createElement('tr');
        cell(doc, tr, r.item);
        cell(doc, tr, r.state, r.bad ? 'link-bad' : '');
        cell(doc, tr, r.rtt); cell(doc, tr, r.net); cell(doc, tr, r.queue); cell(doc, tr, r.bus);
        body.appendChild(tr);
      });
    }
    var tot = doc.getElementById('link-totals');
    tot.textContent = '';
    totals(link).forEach(function (t) {
      var s = doc.createElement('span');
      s.className = 'link-total' + (t.key === 'ok' ? '' : ' link-total-note');
      s.textContent = t.label + ' ' + t.n;
      tot.appendChild(s);
    });
    var box = doc.getElementById('link-marks'), list = marks(data.quality);
    box.hidden = !list.length;
    box.textContent = list.length ? 'marked: ' + list.map(function (m) { return m.key + ' (' + m.text + ')'; }).join(', ') : '';
    var stall = doc.getElementById('link-stall'), ls = link && link.last_stall;
    stall.textContent = ls ? 'last late reply: ' + ls.item + ', ' + (+ls.late_s).toFixed(1) + ' s, at '
      + new Date(ls.t * 1000).toLocaleTimeString() : '';
  }

  window.LinkTile = { render: render, mark: mark, rows: rows, totals: totals, marks: marks, markFor: markFor,
                      merge: merge, proof: proof, ms: ms, reset: function () { known = {}; } };
})();
