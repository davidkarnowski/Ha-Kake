# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP API tests — Flask's test client against a throwaway store.

Every route the dashboard calls, plus the two no-car modes:

  demo    — canned JSON from a directory; must never open a database
  replay  — the real reader wrote the state file; the payload must say so

The real web/leaf_battery.db is never opened here: `store()` is replaced with
one built in tmp_path, and any accidental fall-through to the real thing is
caught by test_demo_mode_never_opens_the_database.
"""
import datetime as dt
import json

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import app as webapp  # noqa: E402  (web/app.py)
import reader as rd  # noqa: E402
from store import Store  # noqa: E402


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Flask test client with every file and the database under tmp_path."""
    rd.set_vehicle("leaf_ze0")
    monkeypatch.setattr(webapp, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(webapp, "DEMO", None)
    for attr, name in (("STATE_FILE", "state.json"), ("TILES_FILE", "tiles.json"),
                       ("CALIB_FILE", "calibration.json"), ("LAYOUTS_FILE", "layouts.json"),
                       ("PAUSE_FILE", "reader.pause")):
        monkeypatch.setattr(rd, attr, str(tmp_path / name))
    store = Store(str(tmp_path / "api.db"))
    monkeypatch.setattr(webapp, "store", lambda: store)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        c.store = store
        c.tmp = tmp_path
        yield c
    store.close()
    rd.set_vehicle("leaf_ze0")


def write_state(client, **fields):
    st = {"status": "ok", "soc": 61.5, "timestamp": "2026-08-25T19:37:36Z"}
    st.update(fields)
    with open(client.tmp / "state.json", "w") as f:
        json.dump(st, f)
    return st


def seed(client, n=3):
    """A few readings, spaced so history() has something to downsample."""
    t = dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.timezone.utc)
    for i in range(n):
        client.store.insert_reading(
            {"soc": 60 + i, "pack_v": 380 + i, "current_a": -1.5, "power_kw": -0.5,
             "capacity_ah": 23.1, "soh": 35.1, "hx": 17.9, "temps": [34, 35, 34, 36],
             "temp_avg_c": 34.8, "cells": [3900 + i] * 96, "cell_min": 3900 + i,
             "cell_max": 3930 + i, "cell_spread": 30},
            ts=t + dt.timedelta(minutes=i), adapter="replay")


# ── /api/status ──────────────────────────────────────────────────────────

def test_status_waiting_before_the_reader_writes_anything(api):
    r = api.get("/api/status")
    assert r.status_code == 200
    assert r.get_json()["status"] == "waiting"


def test_status_serves_the_state_file(api):
    write_state(api, soc=77.0)
    body = api.get("/api/status").get_json()
    assert body["status"] == "ok" and body["soc"] == 77.0


def test_status_passes_the_replay_flag_through(api):
    """The reader stamps every replayed record; the API must not swallow it,
    or a page could show recorded data as live."""
    write_state(api, replay=True, replay_fixture="session_leaf_ze0.json",
                adapter_type="replay")
    body = api.get("/api/status").get_json()
    assert body["replay"] is True
    assert body["replay_fixture"] == "session_leaf_ze0.json"


def test_status_survives_a_truncated_state_file(api):
    (api.tmp / "state.json").write_text("{ not json")
    assert api.get("/api/status").get_json()["status"] == "waiting"


# ── /api/history, /api/health, /api/cells ────────────────────────────────

def test_history_returns_stored_readings(api):
    seed(api)
    rows = api.get("/api/history").get_json()
    assert len(rows) >= 1
    assert {"t", "soc", "pack_v"} <= set(rows[-1])


def test_history_honours_minutes_and_max(api):
    seed(api, n=5)
    assert api.get("/api/history?minutes=0").get_json() == api.get("/api/history").get_json()
    rows = api.get("/api/history?minutes=100000&max=2").get_json()
    assert len(rows) <= 2


def test_health_returns_daily_rows(api):
    seed(api)
    rows = api.get("/api/health").get_json()
    assert isinstance(rows, list)
    assert rows and "day" in rows[0]


def test_cells_returns_per_cell_series(api):
    seed(api)
    body = api.get("/api/cells?limit=2").get_json()
    assert set(body) == {"t", "cells"}
    assert len(body["cells"][-1]) == 96


def test_cells_limit_is_capped(api):
    seed(api)
    assert api.get("/api/cells?limit=99999").status_code == 200


# ── /api/signals ─────────────────────────────────────────────────────────

def test_signals_describes_the_active_profile(api):
    body = api.get("/api/signals").get_json()
    assert body["vehicle"]["name"] == "leaf_ze0"
    assert "soc" in body["signals"] and "lbc01" in body["items"]
    assert body["colors"] and body["types"]
    assert body["demo"] is False


def test_signals_follows_a_profile_switch(api):
    rd.set_vehicle("lancer_2009")
    body = api.get("/api/signals").get_json()
    assert body["vehicle"]["name"] == "lancer_2009"
    assert "rpm" in body["signals"] and "soc" not in body["signals"]


# ── /api/tiles ───────────────────────────────────────────────────────────

def test_tiles_get_returns_the_default_layout_with_names(api):
    tiles = api.get("/api/tiles").get_json()["tiles"]
    ids = [t["id"] for t in tiles]
    assert "soc" in ids
    assert all("name" in t and "items" in t for t in tiles)


def test_tiles_put_persists_and_reads_back(api):
    tiles = api.get("/api/tiles").get_json()["tiles"]
    body = {"tiles": [dict(t, enabled=(t["id"] == "soc")) for t in tiles]}
    put = api.put("/api/tiles", json=body).get_json()["tiles"]
    assert {t["id"]: t["enabled"] for t in put}["soc"] is True
    again = api.get("/api/tiles").get_json()["tiles"]
    assert [t["enabled"] for t in again] == [t["enabled"] for t in put]
    assert (api.tmp / "tiles.json").exists()


def test_tiles_put_drops_a_tile_that_names_no_known_signal(api):
    body = {"tiles": [{"id": "u_bogus", "kind": "signal", "signal": "not_a_signal"}]}
    ids = [t["id"] for t in api.put("/api/tiles", json=body).get_json()["tiles"]]
    assert "u_bogus" not in ids


def test_tiles_put_accepts_a_user_signal_tile(api):
    body = {"tiles": [{"id": "u_soc", "kind": "signal", "signal": "soc",
                       "type": "dial", "span": 4, "enabled": True}]}
    out = {t["id"]: t for t in api.put("/api/tiles", json=body).get_json()["tiles"]}
    assert out["u_soc"]["signal"] == "soc" and out["u_soc"]["span"] == 4
    assert out["u_soc"]["items"] == ["lbc01"]


def test_tiles_put_with_no_body_keeps_the_defaults(api):
    assert api.put("/api/tiles", json={}).get_json()["tiles"]


# ── /api/layouts ─────────────────────────────────────────────────────────

def test_layouts_save_list_load_delete(api):
    assert api.get("/api/layouts").get_json() == {"layouts": []}

    tiles = api.get("/api/tiles").get_json()["tiles"]
    saved = api.put("/api/layouts/night", json={"tiles": tiles}).get_json()
    assert saved["name"] == "night" and saved["tiles"] > 0

    listed = api.get("/api/layouts").get_json()["layouts"]
    assert [l["name"] for l in listed] == ["night"]

    loaded = api.post("/api/layouts/night/load").get_json()
    assert loaded["tiles"]

    assert api.delete("/api/layouts/night").get_json() == {"deleted": True}
    assert api.get("/api/layouts").get_json() == {"layouts": []}


def test_loading_an_unknown_layout_is_404(api):
    assert api.post("/api/layouts/nope/load").status_code == 404


def test_saving_a_layout_with_a_blank_name_is_400(api):
    assert api.put("/api/layouts/%20", json={"tiles": []}).status_code == 400


# ── /api/calibration ─────────────────────────────────────────────────────

def test_calibration_defaults_to_empty(api):
    assert api.get("/api/calibration").get_json() == {}


def test_calibration_put_sets_an_offset(api):
    body = api.put("/api/calibration", json={"current_offset_a": 1.234}).get_json()
    assert body["current_offset_a"] == 1.234
    assert api.get("/api/calibration").get_json()["current_offset_a"] == 1.234


def test_zero_current_without_a_reading_is_409(api):
    assert api.put("/api/calibration", json={"zero_current": True}).status_code == 409


def test_zero_current_uses_the_latest_raw_reading(api):
    write_state(api, current_raw_a=-0.87)
    body = api.put("/api/calibration", json={"zero_current": True}).get_json()
    assert body["current_offset_a"] == -0.87
    assert body["current_zeroed_at"] == "2026-08-25T19:37:36Z"


def test_calibration_delete_clears(api):
    api.put("/api/calibration", json={"current_offset_a": 2.0})
    assert api.delete("/api/calibration").get_json() == {}
    assert api.get("/api/calibration").get_json() == {}


# ── the page itself ──────────────────────────────────────────────────────

def test_index_renders_with_the_active_vehicle(api):
    html = api.get("/").get_data(as_text=True)
    assert "Leaf" in html


# ── demo mode ────────────────────────────────────────────────────────────

@pytest.fixture
def demo(api, monkeypatch):
    """Demo mode pointed at a directory the test owns."""
    d = api.tmp / "demo"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"status": "ok", "soc": 62.4, "timestamp": "old"}))
    (d / "history.json").write_text(json.dumps([{"t": "2026-08-25T17:37:36Z", "soc": 40.0}]))
    monkeypatch.setattr(webapp, "DEMO", str(d))

    def boom():
        raise AssertionError("demo mode must never open the database")

    monkeypatch.setattr(webapp, "store", boom)
    return api


def test_demo_status_is_canned_and_labelled(demo):
    body = demo.get("/api/status").get_json()
    assert body["soc"] == 62.4
    assert body["demo"] is True
    assert body["timestamp"] != "old"          # the clock is rewritten so it looks live


def test_demo_history_is_canned(demo):
    assert demo.get("/api/history").get_json() == [{"t": "2026-08-25T17:37:36Z", "soc": 40.0}]


def test_demo_mode_never_opens_the_database(demo):
    """Every route, including the ones that used to fall through to the real
    store — /api/health, /api/cells, /api/layouts, /api/calibration."""
    for path in ("/api/status", "/api/history", "/api/health", "/api/cells",
                 "/api/signals", "/api/tiles", "/api/layouts", "/api/calibration"):
        assert demo.get(path).status_code == 200, path


def test_demo_health_and_cells_are_empty_when_uncanned(demo):
    assert demo.get("/api/health").get_json() == []
    assert demo.get("/api/cells").get_json() == []


def test_demo_serves_canned_health_when_present(demo):
    (demo.tmp / "demo" / "health.json").write_text(json.dumps([{"day": "2026-08-25", "soh": 35.1}]))
    assert demo.get("/api/health").get_json()[0]["soh"] == 35.1


def test_demo_is_read_only(demo):
    assert demo.put("/api/calibration", json={"current_offset_a": 1}).status_code == 403
    assert demo.delete("/api/calibration").status_code == 403
    assert demo.put("/api/layouts/x", json={"tiles": []}).status_code == 403
    assert demo.delete("/api/layouts/x").status_code == 403
    assert demo.post("/api/layouts/x/load").status_code == 403


def test_demo_tiles_are_the_default_layout(demo):
    tiles = demo.get("/api/tiles").get_json()["tiles"]
    assert [t["id"] for t in tiles] == [t["id"] for t in rd.DEFAULT_TILES["tiles"]]
    assert not (demo.tmp / "tiles.json").exists()       # a PUT must not persist either
    demo.put("/api/tiles", json={"tiles": []})
    assert not (demo.tmp / "tiles.json").exists()


def test_demo_signals_says_it_is_demo(demo):
    assert demo.get("/api/signals").get_json()["demo"] is True


def test_demo_falls_back_when_the_directory_is_missing(api, monkeypatch):
    monkeypatch.setattr(webapp, "DEMO", str(api.tmp / "nope"))
    assert api.get("/api/status").get_json()["status"] == "waiting"
    assert api.get("/api/history").get_json() == []
