// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// One escape helper for every script that builds markup from a string it did
// not write itself: a tile title, a layout name, a flag label, a signal label
// or unit, a value from a record. Pure — no DOM — so node loads it the way the
// tests load playback.js:
//   globalThis.window = globalThis; require('html.js'); window.Html
//
// Loaded first on both pages (index.html, sim.html), so the classic scripts and
// the 3D pack module (which runs after them) can all reach window.Html.
//
// Prefer textContent where a whole element is text. Use Html.esc when a string
// has to sit inside a template literal that becomes innerHTML; it is safe both
// in element content and inside a quoted attribute value.
(function () {
  'use strict';
  const MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => MAP[c]);
  }
  window.Html = { esc, attr: esc };
})();
