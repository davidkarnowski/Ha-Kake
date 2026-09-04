/* SPDX-FileCopyrightText: 2026 David D. Karnowski */
/* SPDX-License-Identifier: AGPL-3.0-or-later */

// tiles.js — the dashboard's four styled tiles: vehicle & shifter, tires,
// body, climate. Lifted out of index.html's updateDash() so a second page
// (the simulator cockpit) can paint the same markup from the same record.
//
// Every renderer takes `root` — document, or any element that contains the
// tile's markup (the partials in web/templates/tiles/) — and a record in the
// decoder vocabulary the dashboard polls from /api/status. It paints whatever
// the record carries and returns true iff the record carried the tile's
// gating keys, so a host page can drop a tile the record cannot drive.
//
// Behaviour and pixel output are the dashboard's, unchanged: every colour
// constant, hinge table, format and gate is the one that was inline; the
// only edit is that every element lookup goes through `root` (see q below).
(function () {
  'use strict';

  const q = (root, id) => root.querySelector('#' + id);

  // House temperature format: °F first, °C alongside — "93°F / 34°C". `f` may
  // be null (derived from `c`). The °F is always whole degrees; the °C is the
  // raw value as the decoder emitted it unless cDec asks for fixed decimals —
  // that one detail is where the dashboard's six inline sites differed.
  function fmtTempParts(c, f, { cDec = null } = {}) {
    if (f == null) f = c * 9 / 5 + 32;
    return { f: f.toFixed(0), c: cDec == null ? `${c}` : c.toFixed(cDec) };
  }
  function fmtTemp(c, f, { cDec = null } = {}) {
    const p = fmtTempParts(c, f, { cDec });
    return `${p.f}°F / ${p.c}°C`;
  }

  function tempColor(t) {
    if (t < 10) return 'var(--blue)';
    if (t < 30) return 'var(--green)';
    if (t < 40) return 'var(--orange)';
    return 'var(--red)';
  }

  function socColor(soc) {
    if (soc > 60) return 'var(--green)';
    if (soc > 30) return 'var(--yellow)';
    if (soc > 15) return 'var(--orange)';
    return 'var(--red)';
  }

  // ── Tires: one wheel profile ──
  function drawWheel(pos, psi, root = document) {
    const wrap = root.querySelector(`.wheel[data-w="${pos}"]`);
    if (!wrap) return;
    const svg = wrap.querySelector('svg');
    const col = psi == null ? 'var(--dim)' : psi < 30 ? 'var(--red)' : psi < 33 ? 'var(--yellow)' : psi > 42 ? 'var(--orange)' : 'var(--green)';
    // side profile: tyre ring, rim, hub, five spokes; tread ticks coloured by pressure
    let treads = '';
    for (let a = 0; a < 360; a += 18) { const r1 = 44, r2 = 49, x1 = 50 + r1 * Math.cos(a * Math.PI / 180), y1 = 50 + r1 * Math.sin(a * Math.PI / 180), x2 = 50 + r2 * Math.cos(a * Math.PI / 180), y2 = 50 + r2 * Math.sin(a * Math.PI / 180); treads += `<line class="tire-tread" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`; }
    let spokes = '';
    for (let a = 0; a < 360; a += 72) { const x = 50 + 26 * Math.cos(a * Math.PI / 180), y = 50 + 26 * Math.sin(a * Math.PI / 180); spokes += `<line x1="50" y1="50" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="var(--card2)" stroke-width="5" stroke-linecap="round"/>`; }
    svg.innerHTML = `<circle cx="50" cy="50" r="46" fill="#141a26" stroke="${col}" stroke-width="5"/>${treads}<circle cx="50" cy="50" r="30" fill="var(--card2)" stroke="var(--border)" stroke-width="2"/>${spokes}<circle cx="50" cy="50" r="9" fill="var(--border)"/>`;
    const p = wrap.querySelector('.wheel-psi');
    p.innerHTML = psi == null ? '--' : `${psi.toFixed(1)}<small>psi</small>`;
    p.style.color = col;
  }

  // ── Shifter ──
  function setShifter(gear, root = document) {
    const on = (id, yes, extra) => { const el = q(root, id); el.setAttribute('class', el.getAttribute('class').split(' ')[0] + (yes ? ' on' : '') + (yes && extra ? ' ' + extra : '')); };
    on('sh-R', gear === 'R', 'rev');
    on('sh-N', gear === 'N');
    on('sh-D', gear === 'D' || gear === 'Eco');
    on('sh-eco', gear === 'Eco');
    on('sh-P', gear === 'P');
    on('sh-Ptxt', gear === 'P');
    const ghost = q(root, 'sh-ghost');
    const y = { R: 48, N: 100, D: 158, Eco: 158 }[gear];
    if (y) { ghost.setAttribute('cy', y); ghost.setAttribute('class', 'ghost on'); }
    else ghost.setAttribute('class', 'ghost');
  }

  // Keys whose presence means the record is driving the tile (the gate each
  // renderer reports). Body and tires keep the exact gate updateDash() used.
  const VEHICLE_KEYS = ['gear', 'start_state_name', 'speed_mph', 'odometer_mi', 'range_mi', 'soh_dash_pct', 'handbrake'];
  const CLIMATE_KEYS = ['cabin_temp_c', 'hvac_target_f', 'hvac_ac_on', 'hvac_heater_level', 'hvac_on', 'hvac_blower_v'];

  // ── Vehicle tile ──
  function renderVehicle(root, data) {
    if (data.gear != null) {
      const g = q(root, 'veh-gear');
      g.textContent = data.gear;
      g.className = 'gear-big' + (data.gear === 'Eco' ? ' eco' : data.gear === 'R' ? ' r' : '');
      setShifter(data.gear, root);
    }
    if (data.start_state_name) q(root, 'veh-state').textContent = data.start_state_name.toUpperCase();
    if (data.speed_mph != null) q(root, 'veh-speed').innerHTML = `${data.speed_mph.toFixed(0)}<small>mph</small>`;
    if (data.odometer_mi != null) q(root, 'veh-odo').innerHTML = `${data.odometer_mi.toLocaleString()}<small>mi</small>`;
    if (data.range_mi != null) q(root, 'veh-range').innerHTML = `${data.range_mi.toFixed(0)}<small>mi · scale tbc</small>`;
    if (data.soh_dash_pct != null) q(root, 'veh-soh').innerHTML = `${data.soh_dash_pct}<small>%</small>`;
    if (data.handbrake != null) q(root, 'veh-brake').textContent = data.handbrake ? 'SET' : 'released';
    if (data.doors_raw != null) {
      const open = [];
      if (data.door_front) open.push('front');
      if (data.door_rear) open.push('rear');
      if (data.door_trunk) open.push('hatch');
      q(root, 'veh-doors').textContent = open.length ? open.join(', ') + ' open' : 'all closed';
    }
    if (data.headlights != null) q(root, 'veh-lights').textContent = data.headlights ? 'ON' : 'off';
    if (data.turn_signal) {
      const t = q(root, 'veh-turn');
      t.textContent = data.turn_signal === 'left' ? '◀ LEFT' : data.turn_signal === 'right' ? 'RIGHT ▶' : data.turn_signal === 'hazards' ? '◀ HAZARDS ▶' : 'off';
      t.style.color = data.turn_signal === 'off' ? '' : 'var(--yellow)';
    }
    return VEHICLE_KEYS.some(k => data[k] != null);
  }

  // ── Tires tile — four wheel profiles ──
  function renderTires(root, data) {
    if (!(data.tpms_psi && data.tpms_psi.length === 4)) return false;
    const asleep = data.tpms_psi.every(p => p < 5);   // sensors sleep when parked
    const [fl, fr, rr, rl] = data.tpms_psi;           // 0x385 order: FL FR RR RL
    const byPos = { fl, fr, rr, rl };
    for (const pos of ['fl', 'fr', 'rl', 'rr']) drawWheel(pos, asleep ? null : byPos[pos], root);
    if (asleep) {
      q(root, 'tire-note').textContent = 'sensors asleep — drive to wake TPMS';
    } else {
      const live = data.tpms_psi.filter(p => p >= 5);
      const mn = Math.min(...live), mx = Math.max(...live);
      q(root, 'tire-note').textContent = `spread ${(mx - mn).toFixed(2)} psi` + (data.tpms_kpa ? ` · ${data.tpms_kpa.map(k => k.toFixed(0)).join(' / ')} kPa` : '');
    }
    return true;
  }

  // ── Body tile — doors / hatch / all lights / per-door locks ──
  function renderBody(root, data) {
    if (!(data.doors_raw != null || data.turn_signal != null)) return false;
    // doors: hinge at the front edge, swing outward (rotate about the hinge point)
    const HINGE = { 'door-dl': [45, 112, 52], 'door-dr': [155, 112, -52], 'door-rl': [45, 164, 52], 'door-rr': [155, 164, -52] };
    const setDoor = (id, open) => {
      const el = q(root, id); if (!el) return;
      el.classList.toggle('open', !!open);
      if (id === 'door-hatch') { el.setAttribute('transform', open ? 'translate(0,10)' : ''); return; }
      const [hx, hy, deg] = HINGE[id];
      el.setAttribute('transform', open ? `rotate(${deg} ${hx} ${hy})` : '');
    };
    setDoor('door-dl', data.door_driver); setDoor('door-dr', data.door_pass);
    setDoor('door-rl', data.door_rl); setDoor('door-rr', data.door_rr);
    setDoor('door-hatch', data.door_hatch);

    // lamps: setLamp(id, on, colour, {glow, opacity, blink})
    const setLamp = (id, on, col, o = {}) => {
      const el = q(root, id); if (!el) return;
      const flashing = !!(on && o.blink);
      el.style.fill = on ? col : '';
      el.style.filter = on ? `drop-shadow(0 0 ${o.glow || 6}px ${col})` : '';
      el.classList.toggle('blink', flashing);
      el.style.opacity = flashing ? '' : (on ? String(o.opacity == null ? 1 : o.opacity) : '');
    };
    const LOW = '#eef2ff', HIGH = '#5c9bff', FOG = '#fff2a0', PARK = '#ffb84d', AMBER = '#ff9a2e', TAIL = '#b02a2a', BRAKE = '#ff2020', REV = '#eef4ff';
    const head = !!data.headlights, high = !!data.high_beam, park = !!data.parking_lights || head, fog = !!data.fog_lights;
    // headlights: low = soft white small glow; high = bright blue, much larger glow (clear difference)
    setLamp('lamp-head-l', head, high ? HIGH : LOW, { glow: high ? 12 : 4 });
    setLamp('lamp-head-r', head, high ? HIGH : LOW, { glow: high ? 12 : 4 });
    setLamp('lamp-park-l', park, PARK, { glow: 4 }); setLamp('lamp-park-r', park, PARK, { glow: 4 });
    setLamp('lamp-fog-l', fog, FOG, { glow: 9 }); setLamp('lamp-fog-r', fog, FOG, { glow: 9 });
    // turn signals + hazards → front, rear and side repeaters on the active side(s)
    const t = data.turn_signal, L = (t === 'left' || t === 'hazards'), R = (t === 'right' || t === 'hazards');
    setLamp('lamp-turn-fl', L, AMBER, { blink: true }); setLamp('lamp-turn-rl', L, AMBER, { blink: true }); setLamp('lamp-side-l', L, AMBER, { blink: true });
    setLamp('lamp-turn-fr', R, AMBER, { blink: true }); setLamp('lamp-turn-rr', R, AMBER, { blink: true }); setLamp('lamp-side-r', R, AMBER, { blink: true });
    // rear tail/brake: dim red tail when the lights are on, bright red when braking (brake wins)
    const brake = !!data.brake_on, rev = (data.gear === 'R'), rearOn = brake || park;
    setLamp('lamp-brake-l', rearOn, brake ? BRAKE : TAIL, { glow: brake ? 10 : 3, opacity: brake ? 1 : 0.7 });
    setLamp('lamp-brake-r', rearOn, brake ? BRAKE : TAIL, { glow: brake ? 10 : 3, opacity: brake ? 1 : 0.7 });
    setLamp('lamp-rev-l', rev, REV, { glow: 6 }); setLamp('lamp-rev-r', rev, REV, { glow: 6 });

    // per-door padlocks: red closed when locked, grey open when unlocked
    const LOCKPOS = { 'lock-dl': [64, 130], 'lock-dr': [136, 130], 'lock-rl': [64, 184], 'lock-rr': [136, 184] };
    const locked = !!data.locked;
    for (const id of Object.keys(LOCKPOS)) {
      const g = q(root, id); if (!g) continue;
      const [x, y] = LOCKPOS[id], col = locked ? '#ef5350' : 'var(--dim)';
      const shackle = locked
        ? `<path class="lock-shackle" d="M${x - 3} ${y - 1} v-3 a3 3 0 0 1 6 0 v3" fill="none" stroke="${col}" stroke-width="1.6"/>`
        : `<path class="lock-shackle" d="M${x - 3} ${y - 1} v-3 a3 3 0 0 1 6 0" fill="none" stroke="${col}" stroke-width="1.6"/>`;
      g.innerHTML = `${shackle}<rect class="lock-body" x="${x - 4}" y="${y - 1}" width="8" height="7" rx="1.5" fill="${col}"/>`;
    }

    // legend
    const names = { door_driver: 'driver', door_pass: 'passenger', door_rl: 'rear-L', door_rr: 'rear-R' };
    const openList = Object.keys(names).filter(k => data[k]).map(k => names[k]);
    const set = (id, txt, col) => { const e = q(root, id); if (e) { e.textContent = txt; e.style.color = col; } };
    set('body-doors', openList.length ? openList.join(', ') + ' open' : 'all closed', openList.length ? 'var(--orange)' : 'var(--green)');
    set('body-hatch', data.door_hatch ? 'open' : 'closed', data.door_hatch ? 'var(--orange)' : 'var(--green)');
    const parts = [];
    if (high) parts.push('high beam'); else if (head) parts.push('headlights');
    if (park && !head) parts.push('parking');
    if (fog) parts.push('fog');
    set('body-lights', parts.length ? parts.join(' + ') : 'off', parts.length ? 'var(--yellow)' : 'var(--dim)');
    set('body-turn', t && t !== 'off' ? t : 'off', (t && t !== 'off') ? 'var(--orange)' : 'var(--dim)');
    set('body-brake', brake ? 'ON' : rev ? 'reverse' : 'off', brake ? 'var(--red)' : rev ? 'var(--accent)' : 'var(--dim)');
    set('body-locktxt', locked ? 'locked' : 'unlocked', locked ? 'var(--red)' : 'var(--dim)');
    return true;
  }

  // ── Climate tile ──
  function renderClimate(root, data) {
    if (data.cabin_temp_c != null) {
      const el = q(root, 'hvac-cabin');
      const cabin = fmtTempParts(data.cabin_temp_c, data.cabin_temp_f);
      el.innerHTML = `${cabin.f}°F<small>${cabin.c}°C</small>`;
      el.style.color = tempColor(data.cabin_temp_c);
      q(root, 'hvac-amb').textContent = fmtTemp(data.hvac_ambient_c, data.hvac_ambient_f);
      q(root, 'hvac-evap').textContent = fmtTemp(data.hvac_evap_c, data.hvac_evap_f);
      q(root, 'hvac-sun').textContent = data.hvac_sunload;
      q(root, 'hvac-tent').style.display = data.hvac_decode === 'tentative' ? '' : 'none';
    }
    if (data.temp_avg_f != null) q(root, 'hvac-pack').textContent = fmtTemp(data.temp_avg_c, data.temp_avg_f);
    if (data.hvac_target_f != null) q(root, 'hvac-set').textContent = fmtTemp(data.hvac_target_c, data.hvac_target_f, { cDec: 0 });
    if (data.hvac_ac_on != null) {
      const el = q(root, 'hvac-ac');
      el.textContent = data.hvac_ac_on ? `ON · ${data.hvac_compressor_rpm || 0} rpm` : 'off';
      el.style.color = data.hvac_ac_on ? 'var(--accent)' : '';
    }
    if (data.hvac_heater_level != null) {
      const el = q(root, 'hvac-heat');
      el.textContent = data.hvac_heater_level ? `${data.hvac_heater_level}` : 'off';
      el.style.color = data.hvac_heater_level ? 'var(--orange)' : '';
    }
    if (data.hvac_on != null) q(root, 'hvac-sys').textContent = data.hvac_on ? 'ON' : 'OFF';
    if (data.hvac_blower_v != null) {
      const on = !!data.hvac_fan_on, v = data.hvac_blower_v, lvl = data.hvac_fan_speed || 0;
      const rotor = q(root, 'fan-rotor');
      rotor.classList.toggle('on', on);
      rotor.style.animationDuration = on ? `${(2.4 / Math.max(1, v)).toFixed(2)}s` : '2s';   // 4 V → 0.6 s/rev, 12 V → 0.2 s/rev
      rotor.style.opacity = on ? '0.95' : '0.35';
      // the amp reports 11 V for both speed 6 and 7 (fan walk: identical every sample) — say so
      const lvlTxt = (on && v >= 11 && lvl === 6) ? '6–7' : String(lvl);
      q(root, 'hvac-fan').innerHTML = on ? `${lvlTxt}<small style="color:var(--dim);font-weight:400;margin-left:4px">/ 7 · ${v} V</small>` : 'off';
      root.querySelectorAll('#fan-bars i').forEach((b, i) => { b.classList.toggle('on', on && i < lvl); b.style.opacity = (on && i === 6 && v >= 11 && lvl === 6) ? '0.45' : ''; if (on && i === 6 && v >= 11 && lvl === 6) b.classList.add('on'); });
    }
    return CLIMATE_KEYS.some(k => data[k] != null);
  }

  window.Tiles = { fmtTemp, fmtTempParts, tempColor, socColor, drawWheel, setShifter, renderVehicle, renderTires, renderBody, renderClimate };
})();
