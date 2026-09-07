/* SPDX-FileCopyrightText: 2026 David D. Karnowski */
/* SPDX-License-Identifier: AGPL-3.0-or-later */

// alerts.js — audible threshold alerts: a tone generator and a rule engine.
//
// The tone comes from the browser's own Web Audio oscillator — no sound file,
// no library. Every browser boots an AudioContext suspended until the page
// has seen one user gesture, so `tone.unlock()` is bound to the first
// pointerdown / keydown and the UI shows "click to enable sound" until
// `tone.ready()` is true.
//
// The engine is pure: `createEngine().evaluate(rules, data, now, ctx)` takes
// the rules Tile Studio flattens out of every enabled tile's `opts.alerts`,
// one /api/status record, a clock and the signal registry, and returns which
// rules fired this tick and which are currently breached. It keeps only the
// per-rule breach state in memory; the rules themselves live in the tile
// config. Nothing here touches the DOM or the network, so the tests drive it
// from node exactly like tiles.js.
(function () {
  'use strict';

  // ── tone patterns: [hz, ms] per note, hz 0 is a rest ────────────────
  const PATTERNS = {
    low:    { label: 'low (two notes down)',  wave: 'square',   notes: [[660, 140], [0, 40], [440, 220]] },
    high:   { label: 'high (two notes up)',   wave: 'square',   notes: [[660, 140], [0, 40], [880, 220]] },
    chirp:  { label: 'chirp',                 wave: 'sine',     notes: [[1200, 60]] },
    triple: { label: 'triple beep',           wave: 'triangle', notes: [[880, 90], [0, 60], [880, 90], [0, 60], [880, 90]] },
  };
  const GAIN = 0.25, ATTACK = 0.008, RELEASE = 0.02;

  let ctx = null;
  function context() {
    if (ctx) return ctx;
    const AC = typeof window !== 'undefined' && (window.AudioContext || window.webkitAudioContext);
    if (!AC) return null;
    try { ctx = new AC(); } catch (e) { ctx = null; }
    return ctx;
  }
  const tone = {
    unlock() {
      const c = context();
      if (c && c.state !== 'running') { try { c.resume(); } catch (e) {} }
    },
    ready() { return !!ctx && ctx.state === 'running'; },
    play(name) {
      const p = PATTERNS[name] || PATTERNS.low;
      const c = context(); if (!c) return false;
      if (c.state !== 'running') { tone.unlock(); if (c.state !== 'running') return false; }
      let t = c.currentTime + 0.01;
      p.notes.forEach(([hz, ms]) => {
        const dur = ms / 1000;
        if (hz > 0) {
          const osc = c.createOscillator(), g = c.createGain();
          osc.type = p.wave; osc.frequency.value = hz;
          g.gain.setValueAtTime(0, t);
          g.gain.linearRampToValueAtTime(GAIN, t + ATTACK);
          g.gain.setValueAtTime(GAIN, t + Math.max(ATTACK, dur - RELEASE));
          g.gain.linearRampToValueAtTime(0, t + dur);
          osc.connect(g); g.connect(c.destination);
          osc.start(t); osc.stop(t + dur + 0.005);
        }
        t += dur;
      });
      return true;
    },
  };
  if (typeof document !== 'undefined') {
    ['pointerdown', 'keydown'].forEach(ev => document.addEventListener(ev, tone.unlock, { passive: true }));
  }

  // ── global mute (per browser; the dashboard and the cockpit share it) ──
  const MUTE_KEY = 'hakake-alerts-muted';
  function muted() { try { return localStorage.getItem(MUTE_KEY) === '1'; } catch (e) { return false; } }
  function setMuted(b) { try { if (b) localStorage.setItem(MUTE_KEY, '1'); else localStorage.removeItem(MUTE_KEY); } catch (e) {} }

  // ── rule engine ─────────────────────────────────────────────────────
  const STALE_AFTER = 90;   // seconds; an item older than max(this, 3 × period) freezes its rules
  // How often a breached rule sounds (and its card flashes) — a 1–60 s slider
  // in the menu. A rule saved without one gets the default; the engine itself
  // still treats 0 as "once", so a hand-written rule can ask for that.
  const REPEAT = { min: 1, max: 60, dflt: 10 };
  function repeatSeconds(v) {
    const n = Math.round(+v);
    if (!isFinite(n) || v == null || v === '') return REPEAT.dflt;
    return Math.min(REPEAT.max, Math.max(REPEAT.min, n));
  }
  function getVal(data, key) {
    if (!data || !key) return null;
    if (key.includes('.')) { const [b, i] = key.split('.'); const seq = data[b]; return Array.isArray(seq) ? seq[+i] : null; }
    return data[key];
  }
  // Re-arm band: 1 % of the signal's registry range, so a value hovering on
  // the threshold does not chatter. Clamped so two close thresholds keep a
  // gap between their bands; 0 when the registry gives no range.
  function hysteresis(sig, rule) {
    if (!sig || sig.min == null || sig.max == null) return 0;
    let h = Math.abs(sig.max - sig.min) * 0.01;
    if (rule && rule.min != null && rule.max != null) h = Math.min(h, Math.max(0, (rule.max - rule.min) / 2));
    return h;
  }
  function isStale(data, sig, ctx) {
    if (!data || !data.item_age) return false;                    // the cockpit's record has no ages
    const age = sig && sig.item != null ? data.item_age[sig.item] : null;
    if (age == null) return true;
    const period = (ctx.items && ctx.items[sig.item] && ctx.items[sig.item].period) || 0;
    return age > Math.max(ctx.staleAfter != null ? ctx.staleAfter : STALE_AFTER, 3 * period);
  }
  function createEngine() {
    const state = new Map();   // rule id -> { breached, lastFired }
    return {
      evaluate(rules, data, now, ctx) {
        ctx = ctx || {};
        const fired = [], breached = [];
        const sigs = ctx.signals || {};
        const frozen = !!(data && data.status != null && data.status !== 'ok');
        for (const r of rules || []) {
          if (!r || !r.id || r.enabled === false) continue;
          const st = state.get(r.id) || { breached: false, lastFired: 0 };
          const s = sigs[r.signal];
          const v = getVal(data, r.signal);
          let skip = frozen || v == null || isStale(data, s, ctx);
          const isBool = !!(s && s.kind === 'bool');
          if (!skip && !isBool && !isFinite(+v)) skip = true;
          if (!skip) {
            let breach, rearm;
            if (isBool) {
              if (r.when !== 'on' && r.when !== 'off') { state.set(r.id, st); continue; }
              breach = (!!v) === (r.when === 'on'); rearm = !breach;
            } else {
              const x = +v, h = hysteresis(s, r);
              breach = (r.min != null && x < r.min) || (r.max != null && x > r.max);
              rearm = (r.min == null || x >= r.min + h) && (r.max == null || x <= r.max - h);
            }
            if (!st.breached && breach) { st.breached = true; st.lastFired = now; fired.push(r); }
            else if (st.breached && rearm) { st.breached = false; }
            else if (st.breached && breach && r.repeat > 0 && now - st.lastFired >= r.repeat * 1000) { st.lastFired = now; fired.push(r); }   // nag only while actually beyond the threshold, not in the re-arm band
          }
          state.set(r.id, st);
          if (st.breached) breached.push(r.id);
        }
        return { fired, breached };
      },
      reset(id) { state.delete(id); },
      clear() { state.clear(); },
      isBreached(id) { const st = state.get(id); return !!(st && st.breached); },
    };
  }

  window.Alerts = { PATTERNS, tone, createEngine, muted, setMuted, hysteresis, getVal, STALE_AFTER, REPEAT, repeatSeconds };
})();
