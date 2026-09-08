# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Playback mode: the pure transport (web/static/playback.js, run under node)
and the page wiring that drives every tile from stored frames."""
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

STATIC = os.path.join(ROOT, "web", "static")
PLAYBACK_JS = os.path.join(STATIC, "playback.js")
INDEX = os.path.join(ROOT, "web", "templates", "index.html")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def run_node(script):
    r = subprocess.run(["node", "-e", script, PLAYBACK_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


HARNESS = """
  globalThis.window = globalThis; require(process.argv[1]);
  const P = window.Playback, out = [];
  const times = [100, 105, 110, 120, 130];
"""


# ── the transport, from node ──

@needs_node
def test_playback_js_parses_and_is_pure():
    r = subprocess.run(["node", "--check", PLAYBACK_JS], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    src = read(PLAYBACK_JS)
    assert "fetch(" not in src and "/api/" not in src and "document" not in src
    assert "window.Playback = {" in src


@needs_node
def test_frame_index_is_the_last_frame_at_or_before_t():
    out = run_node(HARNESS + """
      for (const t of [0, 100, 104.9, 105, 119, 120, 130, 999]) out.push(P.frameIndex(times, t));
      out.push(P.frameIndex([], 5));
      console.log(JSON.stringify(out));""")
    assert out == [0, 0, 0, 1, 2, 3, 4, 4, 0]


@needs_node
def test_tick_advances_by_real_seconds_times_speed_and_pauses_at_the_end():
    out = run_node(HARNESS + """
      const tr = P.createTransport(times); const seen = []; tr.on((k, t) => seen.push(k));
      tr.play(); tr.tick(2); tr.tick(2); tr.tick(2);          // 100 → 106
      out.push(tr.state().k, +tr.state().t.toFixed(3), tr.state().playing);
      tr.setSpeed(10); tr.tick(1);                             // +10 → 116
      out.push(tr.state().k);
      tr.tick(5);                                              // +50 → past the end: clamp, pause
      out.push(tr.state().k, tr.state().t, tr.state().playing);
      tr.tick(1); out.push(tr.state().t);                      // paused: no movement
      tr.play(); out.push(tr.state().t, tr.state().k);         // play from the end restarts
      out.push(seen);
      console.log(JSON.stringify(out));""")
    assert out[:3] == [1, 106, True]
    assert out[3] == 2
    assert out[4:7] == [4, 130, False]
    assert out[7] == 130
    assert out[8:10] == [100, 0]
    assert out[10] == [1, 2, 4, 0]                              # one event per frame change, never twice


@needs_node
def test_seek_step_and_jump_clamp_and_emit_once():
    out = run_node(HARNESS + """
      const tr = P.createTransport(times); const seen = []; tr.on((k) => seen.push(k));
      tr.seek(112); tr.seek(112); tr.seek(-5); tr.seek(5000);
      out.push(seen.slice());
      tr.seek(100); tr.step(1); tr.step(1); tr.step(-5); tr.step(99);
      out.push(seen.slice(3));
      tr.seek(100); tr.jump(11); out.push(tr.state().k); tr.jump(-1000); out.push(tr.state().k, tr.state().t);
      tr.setSpeed(0); out.push(tr.state().speed);
      const empty = P.createTransport([]); empty.play(); empty.tick(1); empty.step(1); out.push(empty.state().k);
      console.log(JSON.stringify(out));""")
    assert out[0] == [2, 0, 4]                                  # 112 → frame 2; repeat is silent; clamps
    assert out[1] == [0, 1, 2, 0, 4]
    assert out[2] == 2 and out[3:5] == [0, 100]
    assert out[5] == 1                                          # a non-positive speed is ignored
    assert out[6] == 0


@needs_node
def test_fmt_dur_matches_the_cockpit_format():
    out = run_node(HARNESS + "out.push(P.fmtDur(0), P.fmtDur(61), P.fmtDur(3725), P.fmtDur(90061), P.fmtDur(-3)); console.log(JSON.stringify(out));")
    assert out == ["00:00:00", "00:01:01", "01:02:05", "1d 01:01:01", "00:00:00"]


# ── the page ──

def test_playback_js_loads_before_the_page_script():
    page = read(INDEX)
    assert page.index('src="/static/tiles.js"') < page.index('src="/static/playback.js"') < page.index("function updateDash(")


def test_timeline_lives_outside_the_tile_grid_and_is_hidden_by_default():
    page = read(INDEX)
    tl = page[page.index('<section class="card timeline" id="timeline"'):]
    tl = tl[:tl.index("</section>")]
    assert page.index('id="timeline"') < page.index('<main class="tiles" id="tiles">')
    assert "hidden>" in tl.split("\n")[0] and "data-tile" not in tl
    for anchor in ("tl-session", "tl-strip", "tl-scrub", "tl-play", "tl-stepb", "tl-stepf", "tl-speeds", "tl-alerts", "tl-live", "tl-whole"):
        assert f'id="{anchor}"' in tl, anchor


def test_header_carries_the_playback_badge_and_the_mode_button():
    page = read(INDEX)
    assert 'id="playback-badge"' in page and 'id="mode-btn"' in page
    assert page.index('id="sim-badge"') < page.index('id="playback-badge"') < page.index('id="mode-btn"')


def test_poll_is_gated_and_render_frame_reuses_its_sinks():
    page = read(INDEX)
    poll = page[page.index("async function poll()"):page.index("const SHOT =")]
    assert "if (isPlayback()) return;" in poll.split("\n")[1]
    rf = page[page.index("function renderFrame(k)"):page.index("function syncTransportUi()")]
    for sink in ("updateTrend(hist)", "updateDash(rec)", "updateSparkline(hist)", "TileStudio.update(rec)", "TileStudio.history(hist)"):
        assert sink in rf, sink
    assert "firePulse" not in rf
    assert "r.records[0].playback_first = true" in page          # the 3D pack resets its rest voltages
    assert "enterPlayback(+q.get('from') || 0, +q.get('to') || 0)" in page


def test_recorded_frames_are_never_stale_and_alerts_are_gated():
    page = read(INDEX)
    assert "const stale = !data.playback && s === 'ok'" in page
    assert "`Recorded ${new Date(lastOk).toLocaleString()}`" in page
    ts = read(os.path.join(STATIC, "tilestudio.js"))
    assert "if (data && data.playback && !window.__alertsInPlayback)" in ts
    assert "resetAlerts() { if (alertEngine) alertEngine.clear(); }" in ts
    assert len(re.findall(r"['\"]/api/", ts)) == 3               # still only the DEFAULTS routes


def test_timeline_css_lives_in_hakake_only():
    css = read(os.path.join(STATIC, "hakake.css"))
    for sel in (".timeline {", ".tl-transport button.on", "#tl-strip {", ".tiles-btn.on"):
        assert sel in css, sel
    assert ".tl-" not in read(os.path.join(STATIC, "tiles.css"))
