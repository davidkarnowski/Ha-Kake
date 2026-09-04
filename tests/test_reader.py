# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reader supervisor tests — fake adapter, no car, no BLE.

Scenarios:
  * transport dies mid-poll → status 'reconnecting', re-detect, resume, readings keep counting
  * car asleep (all NO DATA) → status 'asleep', no rows written
"""
import asyncio
import json
import os

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

import reader as rd  # noqa: E402
from store import Store  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "lbc_raw_20260824.json")
with open(FIXTURE) as f:
    RAW = json.load(f)["groups"]


class FakeELM:
    adapter_type = "fake"
    adapter_name = "FakeELM"
    adapter_port = "mem"

    def __init__(self, script):
        # script: list of behaviours per poll: "ok", "nodata", "die"
        self.script = list(script)
        self.closed = False
        self.polls = 0

    target = "79B"
    behaviour = "ok"

    async def send(self, cmd, wait=0, timeout=0):
        if cmd.upper().startswith("ATSH"):
            self.target = cmd.split()[-1].upper()
            return []
        if cmd == self.first_cmd and self.target == "79B":   # a new LBC poll cycle
            self.polls += 1
            self.behaviour = self.script.pop(0) if self.script else "ok"
        if cmd.upper() == "ATI":
            return [] if self.behaviour == "dropped" else ["ELM327 v1.5"]
        if self.behaviour == "die":
            raise ConnectionError("BLE dropped")
        if self.behaviour == "nodata":
            return ["NO DATA"]
        if self.behaviour == "dropped":
            return []                       # link down: no data at all
        return RAW.get(cmd, [])

    async def close(self):
        self.closed = True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(rd, "PAUSE_FILE", str(tmp_path / "reader.pause"))
    monkeypatch.setattr(rd, "TILES_FILE", str(tmp_path / "tiles.json"))
    monkeypatch.setattr(rd, "STORE_PERIOD", 0.0)
    monkeypatch.setattr(rd, "BACKOFF_MIN", 0.01)
    monkeypatch.setattr(rd, "BACKOFF_MAX", 0.02)
    monkeypatch.setattr(rd, "ASLEEP_INTERVAL", 0.01)
    store = Store(str(tmp_path / "t.db"))
    return tmp_path, store


def state(tmp_path):
    with open(tmp_path / "state.json") as f:
        return json.load(f)


async def run_for(reader, seconds):
    task = asyncio.ensure_future(reader.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_reconnect_after_transport_death(env, monkeypatch):
    tmp_path, store = env
    adapters = [FakeELM(["ok", "die"]), FakeELM(["ok", "ok"])]
    for a in adapters:
        a.first_cmd = "2101"
    statuses = []

    async def fake_detect(prefer=None, log=None):
        return adapters.pop(0) if adapters else FakeELM(["ok"])

    async def fake_configure(elm):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    orig_publish = rd.Reader.publish

    def spy(self, status, message=None, **fields):
        statuses.append(status)
        orig_publish(self, status, message, **fields)

    monkeypatch.setattr(rd.Reader, "publish", spy)

    reader = rd.Reader(interval=0.01, adapter_pref=None, store=store)
    asyncio.run(run_for(reader, 0.5))

    assert "reconnecting" in statuses
    assert statuses.index("reconnecting") > statuses.index("ok")
    assert statuses[-1] == "ok"
    assert reader.readings >= 3                 # 1 before the drop, ≥2 after
    assert store.count() == reader.readings     # every good poll persisted
    s = state(tmp_path)
    assert s["status"] == "ok" and s["adapter_type"] == "fake"
    assert s["temps_f"] == [93.2, 95.0, 93.2, 96.8]
    assert s["power_kw"] < 0
    sessions = store.conn.execute("SELECT COUNT(*) FROM sessions WHERE ended IS NOT NULL").fetchone()[0]
    assert sessions >= 1


def test_car_asleep_keeps_last_good(env, monkeypatch):
    tmp_path, store = env
    elm = FakeELM(["ok", "nodata", "nodata", "nodata", "ok"])
    elm.first_cmd = "2101"

    async def fake_detect(prefer=None, log=None):
        return elm

    async def fake_configure(e):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    seen = []
    orig = rd.Reader.publish

    def spy(self, status, message=None, **fields):
        seen.append(status)
        orig(self, status, message, **fields)

    monkeypatch.setattr(rd.Reader, "publish", spy)

    reader = rd.Reader(interval=0.01, adapter_pref=None, store=store)
    asyncio.run(run_for(reader, 0.4))

    assert "asleep" in seen
    assert store.count() == reader.readings >= 2   # NO DATA polls are not stored
    s = state(tmp_path)
    assert s["status"] == "ok" and "soc" in s


def test_pause_file_stops_reader_cleanly(env, monkeypatch):
    tmp_path, store = env
    elm = FakeELM(["ok", "ok", "ok"])
    elm.first_cmd = "2101"

    async def fake_detect(prefer=None, log=None):
        return elm

    async def fake_configure(e):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)

    reader = rd.Reader(interval=0.01, adapter_pref=None, store=store)

    async def scenario():
        task = asyncio.ensure_future(reader.run())
        await asyncio.sleep(0.15)                      # a few good polls
        (tmp_path / "reader.pause").touch()
        await asyncio.wait_for(task, timeout=2.0)     # run() must return on its own

    asyncio.run(scenario())
    assert reader.readings >= 1
    assert elm.closed                                 # adapter handed back
    assert state(tmp_path)["status"] == "paused"
    assert "soc" in state(tmp_path)                   # last good reading kept for the dashboard


def test_dropped_link_reconnects(env, monkeypatch):
    """A slept/dropped BLE link (adapter silent, ATI unanswered) must reconnect,
    not sit forever in 'asleep'."""
    tmp_path, store = env
    adapters = [FakeELM(["ok", "dropped"]), FakeELM(["ok", "ok"])]
    for a in adapters:
        a.first_cmd = "2101"
    statuses = []

    async def fake_detect(prefer=None, log=None):
        return adapters.pop(0) if adapters else FakeELM(["ok"])

    async def fake_configure(elm):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    orig = rd.Reader.publish

    def spy(self, status, message=None, **fields):
        statuses.append(status)
        orig(self, status, message, **fields)

    monkeypatch.setattr(rd.Reader, "publish", spy)

    reader = rd.Reader(interval=0.01, adapter_pref=None, store=store)
    asyncio.run(run_for(reader, 0.5))

    assert "reconnecting" in statuses           # the dropped link forced a reconnect
    assert "asleep" not in statuses             # it was NOT mistaken for car-asleep
    assert statuses[-1] == "ok"                 # and it recovered
    assert state(tmp_path)["status"] == "ok"


# ── vehicle-agnostic surfaces ────────────────────────────────────────────
# Regression guards for the 2026-09 sweep that took the last Leaf assumptions
# out of the generic layers.

def _with_vehicle(name, fn):
    try:
        rd.set_vehicle(name)
        return fn()
    finally:
        rd.set_vehicle("leaf_ze0")


def test_summary_is_profile_driven():
    """The per-cycle console line is derived from the profile's fast-lane
    signals. It used to hardcode SOC / power / gear, so any car without a
    traction battery printed 'SOC=?' and a dash forever."""
    leaf = _with_vehicle("leaf_ze0", lambda: rd.summary(
        {"soc": 63.25, "pack_v": 391.1, "current_a": -0.9, "power_kw": -0.352}))
    assert "State of charge 63.2%" in leaf
    assert "Pack voltage 391.1 V" in leaf
    assert "Power -0.35 kW" in leaf

    lancer = _with_vehicle("lancer_2009", lambda: rd.summary({"rpm": 812.0, "speed_mph": 0.0}))
    assert "Engine RPM 812 rpm" in lancer
    assert "Speed 0 mph" in lancer
    assert "SOC" not in lancer and "State of charge" not in lancer


def test_summary_missing_values_and_empty_registry():
    assert "?" in _with_vehicle("lancer_2009", lambda: rd.summary({}))


def test_summary_signals_are_fast_lane_only():
    def check():
        keys = rd.summary_signals()
        assert keys and len(keys) <= rd.SUMMARY_MAX
        assert all(rd.ITEMS[rd.signals.SIGNALS[k]["item"]]["period"] == 0 for k in keys)

    for profile in ("leaf_ze0", "lancer_2009"):
        _with_vehicle(profile, check)


def test_env_prefers_hakake_and_falls_back_to_leaf(monkeypatch):
    """LEAF_* names are documented and in use, so they must keep working; the
    HAKAKE_* name wins when both are set."""
    import util
    monkeypatch.delenv("HAKAKE_BLE_ADDR", raising=False)
    monkeypatch.delenv("LEAF_BLE_ADDR", raising=False)
    assert util.env("HAKAKE_BLE_ADDR", "LEAF_BLE_ADDR") is None
    assert util.env("HAKAKE_BLE_ADDR", "LEAF_BLE_ADDR", default="x") == "x"

    monkeypatch.setenv("LEAF_BLE_ADDR", "old-name")
    assert util.env("HAKAKE_BLE_ADDR", "LEAF_BLE_ADDR") == "old-name"

    monkeypatch.setenv("HAKAKE_BLE_ADDR", "new-name")
    assert util.env("HAKAKE_BLE_ADDR", "LEAF_BLE_ADDR") == "new-name"

    monkeypatch.setenv("HAKAKE_BLE_ADDR", "")          # empty is unset, not a win
    assert util.env("HAKAKE_BLE_ADDR", "LEAF_BLE_ADDR") == "old-name"


def test_local_config_honours_both_env_spellings(monkeypatch):
    import elm327
    for n in ("HAKAKE_BLE_ADDR", "LEAF_BLE_ADDR", "HAKAKE_BLE_NAME", "LEAF_BLE_NAME"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("LEAF_BLE_NAME", "OLDBLE")
    monkeypatch.setenv("HAKAKE_BLE_ADDR", "AA:BB")
    cfg = elm327.load_local_config()
    assert cfg["ble_name"] == "OLDBLE" and cfg["ble_addr"] == "AA:BB"


def test_fmt_temp_still_importable_from_leaf_decoders():
    """It moved to util.py; leaf_decoders re-exports it for its old callers."""
    import leaf_decoders
    import util
    assert leaf_decoders.fmt_temp is util.fmt_temp
    assert leaf_decoders.c_to_f is util.c_to_f
    assert util.fmt_temp(34) == "34 °C / 93 °F"


def test_reader_takes_its_cost_multiplier_from_the_transport(env, monkeypatch):
    """A profile's `est` numbers are BLE seconds; the transport says what they
    are worth on this link. Without this the USB scheduler spent a 1.5 s budget
    on work that took 60 ms and the slow lane starved."""
    tmp_path, store = env
    elm = FakeELM(["ok", "ok", "ok"])
    elm.first_cmd = "2101"
    elm.SPEED = 0.1

    async def fake_detect(prefer=None, log=None):
        return elm

    async def fake_configure(e):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)

    reader = rd.Reader(interval=0.01, adapter_pref=None, store=store)
    assert reader.speed == 1.0                      # before any adapter is known
    asyncio.run(run_for(reader, 0.2))
    assert reader.speed == 0.1
    assert reader.estimate("lbc02") == pytest.approx(rd.ITEMS["lbc02"]["est"] * 0.1)


def test_connecting_does_not_republish_the_previous_adapter(env, tmp_path):
    """Switching adapters must not show the old one's identity.

    Regression: `--adapter usb` after a BLE session published the BLE UUID and
    that session's SOC next to "Detecting adapter (usb)…", because publish()
    merges last_good wholesale. A stale reading is honest (item_age says so);
    a stale *identity* is not.
    """
    r = rd.Reader(interval=0, adapter_pref="usb", store=Store(str(tmp_path / "id.db")))
    r.last_good = {
        "adapter_type": "ble", "adapter_name": "ELM327 v1.5",
        "adapter_port": "a-previous-sessions-ble-handle", "soc": 53.78,
    }
    r.publish("connecting", "Detecting adapter (usb)…")
    st = rd.load_state()
    assert st["status"] == "connecting"
    for k in ("adapter_type", "adapter_name", "adapter_port"):
        assert k not in st, f"{k} carried over from the previous adapter"
    assert st["soc"] == 53.78          # readings still carry; identity does not

    r.publish("ok", adapter_type="usb", adapter_name="ELM327 v1.5", adapter_port="/dev/ttyUSB0")
    assert rd.load_state()["adapter_type"] == "usb"
