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

  window.Playback = { SPEEDS, frameIndex, createTransport, fmtDur };
})();
