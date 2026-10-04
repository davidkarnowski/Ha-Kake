// SPDX-FileCopyrightText: 2026 David D. Karnowski
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Playback transport: a clock over a list of frame times. Pure — no DOM, no
// network — so node loads it the way tests/test_alerts.py loads alerts.js:
//   globalThis.window = globalThis; require('playback.js'); window.Playback
//
// The clock lives in the browser on purpose. The simulator's clock is Python
// (sim.step) and replay's is inside the replay adapter, because there the
// *reader* is the thing being run; in playback nothing is being run — the
// reader may be live on the car while two browsers scrub two different
// afternoons. What playback borrows from the simulator is its vocabulary: a
// speed multiplier shown as N×, and jumps of fixed seconds.
(function () {
  'use strict';
  const SPEEDS = [0.5, 1, 4, 10, 60];      // real-time multipliers offered by the page

  // Index of the last frame at or before t; 0 when t precedes the first frame.
  function frameIndex(times, t) {
    if (!times.length || t < times[0]) return 0;
    let lo = 0, hi = times.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (times[mid] <= t) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  // createTransport(times) → { seek, play, pause, toggle, setSpeed, step, jump, tick, on, state }
  // `times` are epoch seconds, ascending. Subscribers get (k, t) whenever the
  // current frame index changes — never twice for the same frame.
  function createTransport(times) {
    const s = { t: times.length ? times[0] : 0, playing: false, speed: 1, k: -1 };
    const subs = [];
    const last = () => times.length ? times[times.length - 1] : 0;
    const emit = () => {
      const k = frameIndex(times, s.t);
      if (k !== s.k) { s.k = k; subs.forEach(f => f(k, s.t)); }
    };
    const api = {
      seek(t) { s.t = Math.min(last(), Math.max(times[0] || 0, t)); emit(); },
      play() { if (!times.length) return; if (s.t >= last()) { s.t = times[0]; emit(); } s.playing = true; },
      pause() { s.playing = false; },
      toggle() { if (s.playing) api.pause(); else api.play(); },
      setSpeed(x) { if (x > 0) s.speed = x; },
      step(n) { if (!times.length) return; api.seek(times[Math.min(times.length - 1, Math.max(0, frameIndex(times, s.t) + n))]); },
      jump(sec) { api.seek(s.t + sec); },
      tick(realDt) {
        if (!s.playing || !(realDt > 0)) return;
        s.t += realDt * s.speed;
        if (s.t >= last()) { s.t = last(); s.playing = false; }
        emit();
      },
      on(f) { subs.push(f); },
      state() { return { t: s.t, playing: s.playing, speed: s.speed, k: s.k }; },
    };
    emit();
    return api;
  }

  // d hh:mm:ss, the simulator cockpit's format
  function fmtDur(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return (d ? d + 'd ' : '') + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  // How old item `item`'s value is in `data`, in seconds — "read at" for a tile.
  //   * A record carrying item_ts_epoch (docs/TIMING.md) is measured from the
  //     record's own moment: live, from `nowMs` (the age keeps growing between
  //     polls); playback, from the frame's timestamp — the age as it *was*,
  //     never as it is now, because a recorded frame is not stale.
  //   * Otherwise the reader's item_age (seconds at emission) is used as is.
  // null when the record knows nothing about the item.
  function itemAge(data, item, nowMs) {
    if (!data || !item) return null;
    const ts = data.item_ts_epoch ? data.item_ts_epoch[item] : null;
    if (typeof ts === 'number') {
      let ref;
      if (data.playback || nowMs == null) ref = Date.parse(data.timestamp || data.last_ok || '') / 1000;
      else ref = nowMs / 1000;
      if (ref === ref) return Math.max(0, ref - ts);     // NaN-safe; a row keeps whole seconds
    }
    const a = data.item_age ? data.item_age[item] : null;
    return typeof a === 'number' ? a : null;
  }

  // A short duration with the precision it deserves: "290 ms" under a second,
  // "1.4 s" under ten, "12 s" after that; '' for null. A native CAN adapter
  // reads the pack every ~0.4 s, and whole seconds hid that (2026-10-03).
  function fmtMs(sec) {
    if (sec == null || !(sec >= 0)) return '';
    const ms = Math.round(sec * 1000);                 // 0.9996 is "1.0 s", not "1000 ms"
    return ms < 1000 ? `${ms} ms` : sec < 9.95 ? `${sec.toFixed(1)} s` : `${Math.round(sec)} s`;
  }

  // "290 ms ago" / "3.4 s ago" / "42 s ago" / "4m ago" / "1.2h ago"; '' for null
  function fmtAge(sec) {
    if (sec == null || !(sec >= 0)) return '';
    return sec < 60 ? `${fmtMs(sec)} ago` : sec < 5400 ? `${Math.round(sec / 60)}m ago` : `${(sec / 3600).toFixed(1)}h ago`;
  }

  // How the item's last read went, from the reader's live-only item_dur /
  // item_gap: "read 290 ms · every 430 ms". '' when the record has neither
  // (a stored frame in playback, an older reader).
  function fmtRead(data, item) {
    if (!data || !item) return '';
    const d = data.item_dur ? data.item_dur[item] : null, g = data.item_gap ? data.item_gap[item] : null;
    const parts = [];
    if (typeof d === 'number') parts.push(`read ${fmtMs(d)}`);
    if (typeof g === 'number') parts.push(`every ${fmtMs(g)}`);
    return parts.join(' · ');
  }

  window.Playback = { SPEEDS, frameIndex, createTransport, fmtDur, itemAge, fmtAge, fmtMs, fmtRead };
})();
