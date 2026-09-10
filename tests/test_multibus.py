# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Several adapters at once, and where a value comes from (plan §10,
docs/ARCHITECTURE.md "Several adapters"), without a car:

  * the contract: ITEMS[i]["bus"], BUSES, SIGNALS `sources` / `tolerance`,
    and what the validator refuses;
  * the adapters list: `--adapter X` shorthand, HAKAKE_ADAPTERS, the file,
    the per-entry overrides detect_adapter(cfg=) receives;
  * two fake transports polled concurrently with per-bus timing and target
    state; one bus dropping and reconnecting while the other keeps storing;
  * the record: `adapters` per bus, `bus_alive`, the old adapter_* keys
    unchanged from the primary bus;
  * the resolver's precedence table, the disagreement event, and the Leaf's
    worked example (current_a from two verified sources, never overwritten).
"""
import asyncio
import json
import os
import time
import types

import pytest

from conftest import ROOT, fixture  # noqa: E402

import reader as rd  # noqa: E402
import vehicles  # noqa: E402
import elm327  # noqa: E402
from store import Store  # noqa: E402

RAW = fixture("lbc_raw_20260824.json")["groups"]


def run(coro):
    return asyncio.run(coro)


class FakeBusELM:
    """An adapter on one bus. UDS groups answer from the fixture; passive
    captures answer one frame per id; every command costs `delay` seconds so
    concurrency is measurable. `script` per LBC poll: ok | nodata | die."""

    adapter_port = "mem"

    def __init__(self, bus="car", name="Fake 1", adapter_type="fake", delay=0.0, script=(), passive=None):
        self.bus = bus
        self.adapter_name = name
        self.adapter_type = adapter_type
        self.delay = delay
        self.script = list(script)
        self.behaviour = "ok"
        self.passive = passive or {}
        self.target = None
        self.cmds = []
        self.closed = False
        self.polls = 0

    async def send(self, cmd, wait=0, timeout=0):
        self.cmds.append(cmd)
        if self.delay:
            await asyncio.sleep(self.delay)
        up = cmd.upper()
        if up.startswith("ATSH"):
            self.target = up.split()[-1]
            return []
        if up.startswith("ATCRA"):
            self.rx = up.split()[-1]
            return []
        if up == "ATI":
            return [] if self.behaviour == "dropped" else [self.adapter_name]
        if up == "ATMA":
            if self.behaviour == "die":
                raise ConnectionError(f"{self.bus} link dropped")
            line = self.passive.get(getattr(self, "rx", ""))
            return [line] if line else []
        if up.startswith("AT") or not cmd:
            return []
        if cmd == "2101" and self.target == "79B":
            self.polls += 1
            self.behaviour = self.script.pop(0) if self.script else "ok"
        if self.behaviour == "die":
            raise ConnectionError(f"{self.bus} link dropped")
        if self.behaviour == "nodata":
            return ["NO DATA"]
        return RAW.get(cmd, [])

    async def close(self):
        self.closed = True


@pytest.fixture
def env(isolated_reader, leaf_profile, tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STORE_PERIOD", 0.0)
    monkeypatch.setattr(rd, "BACKOFF_MIN", 0.01)
    monkeypatch.setattr(rd, "BACKOFF_MAX", 0.02)
    monkeypatch.setattr(rd, "ASLEEP_INTERVAL", 0.01)
    monkeypatch.delenv("HAKAKE_ADAPTERS", raising=False)
    store = Store(str(tmp_path / "t.db"))
    yield tmp_path, store
    store.close()


@pytest.fixture
def two_buses(env, monkeypatch):
    """The Leaf with its gear frame moved to a second bus, "ev"."""
    monkeypatch.setattr(rd.VEHICLE, "BUSES", ("car", "ev"), raising=False)
    monkeypatch.setitem(rd.ITEMS["p421"], "bus", "ev")
    monkeypatch.setitem(rd.ITEMS["p358"], "bus", "ev")
    rd.set_vehicle("leaf_ze0")                     # rebind BUSES / PRIMARY_BUS
    yield env
    rd.set_vehicle("leaf_ze0")


async def run_for(reader, seconds):
    task = asyncio.ensure_future(reader.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _tiles(tmp_path, ids):
    with open(tmp_path / "tiles.json", "w") as f:
        json.dump({"tiles": [{"id": i, "enabled": True} for i in ids]}, f)


# ── the contract ─────────────────────────────────────────────────────────

def test_bus_defaults_and_helpers(leaf_profile):
    assert vehicles.buses(leaf_profile) == ("car",)
    assert vehicles.item_bus(leaf_profile, "lbc01") == "car" and vehicles.item_bus(leaf_profile, "nope") == "car"
    assert vehicles.primary_bus(leaf_profile) == "car"
    specs = vehicles.source_specs(leaf_profile)
    assert list(specs) == ["current_a"]
    assert [s["key"] for s in specs["current_a"]["sources"]] == ["hv_current2_a", "g05_current_a"]
    assert specs["current_a"]["tolerance"] == 3.0
    lancer = vehicles.get_vehicle("lancer_2009")
    assert vehicles.source_specs(lancer) == {} and vehicles.buses(lancer) == ("car",)


def _stub(base, **over):
    ns = types.SimpleNamespace(**{k: getattr(base, k) for k in dir(base) if not k.startswith("__")})
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_validator_checks_buses_and_sources():
    leaf = vehicles.get_vehicle("leaf_ze0")
    items = {k: dict(v) for k, v in leaf.ITEMS.items()}
    items["p421"]["bus"] = "ev"
    p = "\n".join(vehicles.validate_profile(_stub(leaf, ITEMS=items)))
    assert "item 'p421' is on bus 'ev', which BUSES ('car') does not declare" in p
    assert not vehicles.validate_profile(_stub(leaf, ITEMS=items, BUSES=("car", "ev")))
    p = "\n".join(vehicles.validate_profile(_stub(leaf, BUSES=("car", "car"))))
    assert "lists a bus twice" in p
    p = "\n".join(vehicles.validate_profile(_stub(leaf, BUSES=())))
    assert "BUSES must be a non-empty tuple" in p
    items["lbc01"]["bus"] = "ev"
    p = "\n".join(vehicles.validate_profile(_stub(leaf, ITEMS=items, BUSES=("car", "ev"), FAST_ONLY={"lbc01", "lbc05"})))
    assert "FAST_ONLY items must share one bus" in p
    # sources
    sig = {k: dict(v) for k, v in leaf.SIGNALS.items()}
    sig["current_a"]["sources"] = [{"key": "hv_current2_a", "item": "nope", "confidence": "sure", "rate_hz": 0},
                                   {"item": "lbc05"}, {"key": "hv_current2_a", "item": "lbc01"}]
    sig["soc"]["tolerance"] = -1
    sig["gear"]["sources"] = [{"key": "gear", "item": "p421"}]
    p = "\n".join(vehicles.validate_profile(_stub(leaf, SIGNALS=sig)))
    assert "names unknown item 'nope'" in p and "confidence must be 'verified' or 'tentative'" in p
    assert "rate_hz must be a positive number" in p and "without a record 'key'" in p
    assert "lists source 'hv_current2_a' twice" in p
    assert "signal 'soc' tolerance must be a number >= 0" in p and "tolerance but no sources" in p
    assert "signal 'gear' has sources but is not a number signal" in p


# ── the adapters list ────────────────────────────────────────────────────

def test_adapter_entries_shorthand_file_and_env(leaf_profile, monkeypatch):
    monkeypatch.delenv("HAKAKE_ADAPTERS", raising=False)
    assert rd.adapter_entries("usb") == [{"type": "usb", "bus": "car", "shorthand": True}]
    assert rd.adapter_entries(None, cfg={}) == [{"type": None, "bus": "car", "shorthand": True}]
    assert rd.entry_cfg(rd.adapter_entries("can")[0]) == {}          # the file's can_bus still names the bus
    monkeypatch.setattr(rd, "BUSES", ("car", "ev"))
    ents = rd.adapter_entries(None, cfg={"adapters": [{"type": "can", "bus": "ev", "can_channel": "/dev/x"},
                                                      {"type": "usb", "serial_port": "/dev/y"}]})
    assert [e["bus"] for e in ents] == ["car", "ev"]                    # primary bus first
    assert rd.entry_cfg(ents[0]) == {"serial_port": "/dev/y"}
    assert rd.entry_cfg(ents[1]) == {"can_channel": "/dev/x", "can_bus": "ev"}
    m = rd.entry_cfg({"type": "mqtt", "bus": "ev", "host": "pi", "port": 1884, "can_bitrate": 1})
    assert m == {"mqtt": {"host": "pi", "port": 1884, "bus": "ev"}, "can_bitrate": 1}
    monkeypatch.setenv("HAKAKE_ADAPTERS", json.dumps([{"type": "mqtt", "bus": "ev"}, {"type": "ble"}]))
    ents = rd.adapter_entries(None, cfg={"adapters": [{"type": "usb"}]})
    assert [(e["type"], e["bus"]) for e in ents] == [("ble", "car"), ("mqtt", "ev")]   # env wins
    assert rd.adapter_entries("replay")[0]["type"] == "replay"                       # --adapter wins over both
    for bad in ([{"type": "warp"}], [{"bus": "nope"}], [{"type": "usb"}, {"type": "ble"}], "usb", [1]):
        monkeypatch.setenv("HAKAKE_ADAPTERS", json.dumps(bad))
        with pytest.raises(ValueError):
            rd.adapter_entries(None)
    monkeypatch.setenv("HAKAKE_ADAPTERS", "{not json")
    with pytest.raises(ValueError):
        rd.adapter_entries(None)


def test_detect_adapter_takes_per_entry_overrides(monkeypatch):
    seen = {}

    async def fake_open_can(cfg, log=print):
        seen["can"] = dict(cfg)
        return "CAN"

    async def fake_open_mqtt(log=print, cfg=None):
        seen["mqtt"] = cfg
        return "MQTT"

    import cantransport
    import mqttsource
    monkeypatch.setattr(cantransport, "open_can", fake_open_can)
    monkeypatch.setattr(mqttsource, "open_mqtt", fake_open_mqtt)
    monkeypatch.setattr(elm327, "_cfg", {"can_bus": "car", "can_channel": "/dev/a", "mqtt": {"host": "pi", "bus": "car"}})
    assert run(elm327.detect_adapter("can", cfg={"can_bus": "ev"})) == "CAN"
    assert seen["can"]["can_bus"] == "ev" and seen["can"]["can_channel"] == "/dev/a"
    assert run(elm327.detect_adapter("can")) == "CAN" and seen["can"]["can_bus"] == "car"
    assert run(elm327.detect_adapter("mqtt", cfg={"mqtt": {"bus": "ev"}})) == "MQTT"
    assert seen["mqtt"] == {"host": "pi", "bus": "ev"}
    assert run(elm327.detect_adapter("mqtt")) == "MQTT" and seen["mqtt"] is None


# ── two buses at once ────────────────────────────────────────────────────

def test_two_buses_are_polled_concurrently_with_their_own_targets(two_buses):
    tmp_path, store = two_buses
    _tiles(tmp_path, ["soc", "vehicle", "body"])
    car = FakeBusELM("car", "Car 1", delay=0.02)
    ev = FakeBusELM("ev", "Ev 1", delay=0.02, passive={"421": "421 20 00 00", "358": "358 00 00 00 00 00 00 00 00"})
    r = rd.Reader(interval=0, adapter_pref=None, store=store)
    r.entries = rd.adapter_entries(None, cfg={"adapters": [{"type": "usb"}, {"type": "can", "bus": "ev"}]})
    r.transports = {"car": car, "ev": ev}
    t0 = time.monotonic()
    rec, alive = run(r.poll_once())
    elapsed = time.monotonic() - t0
    assert alive is True and rec["soc"] > 0 and isinstance(rec["gear"], str)   # both buses decoded
    car_cost = sum(0.02 for c in car.cmds)
    ev_cost = sum(0.02 for c in ev.cmds)
    assert elapsed < car_cost + ev_cost - 0.05                        # overlapped, not serialised
    assert "lbc01" in rec["timing"] and "p421" in rec["timing"]
    assert set(r._targets) == {"car", "ev"} and r._targets["ev"] == "passive"   # per-bus target state
    assert car.target == "79B" and any(c.startswith("ATSH") for c in car.cmds)   # UDS only on the car bus
    assert "ATCAF0" in ev.cmds and not any(c.startswith("ATSH") for c in ev.cmds)
    assert rec["bus_alive"] == {"car": True, "ev": True}
    assert rec["item_ts_epoch"]["p421"] > 0


def test_a_secondary_bus_drops_and_reconnects_while_the_primary_keeps_storing(two_buses, monkeypatch):
    tmp_path, store = two_buses
    _tiles(tmp_path, ["soc", "vehicle", "body"])
    car = FakeBusELM("car", "Car 1")
    ev1 = FakeBusELM("ev", "Ev 1", script=["ok", "ok"], passive={"421": "421 20 00 00"})
    ev2 = FakeBusELM("ev", "Ev 2", passive={"421": "421 20 00 00"})
    calls = {"can": 0}
    handed = []

    async def fake_detect(prefer=None, log=None, cfg=None):
        handed.append((prefer, cfg))
        if prefer == "can":
            calls["can"] += 1
            if calls["can"] == 1:
                return ev1                           # the first open
            if calls["can"] == 2:
                raise ConnectionError("board unplugged")   # first reconnect attempt fails
            return ev2                               # then a fresh adapter
        return car

    async def fake_configure(elm):
        pass

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    monkeypatch.setattr(rd, "load_local_config", lambda: {"adapters": [{"type": "usb"}, {"type": "can", "bus": "ev", "can_channel": "x"}]})
    states = []
    orig = rd.Reader.publish

    def spy(self, status, message=None, **fields):
        states.append(status)
        orig(self, status, message, **fields)

    monkeypatch.setattr(rd.Reader, "publish", spy)

    r = rd.Reader(interval=0.01, adapter_pref=None, store=store)

    async def scenario():
        task = asyncio.ensure_future(r.run())
        await asyncio.sleep(0.12)
        n_before = store.count()
        ev1.behaviour = "die"                        # the EV adapter goes away mid-cycle
        await asyncio.sleep(0.25)
        n_after = store.count()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return n_before, n_after

    n_before, n_after = run(scenario())
    assert n_after > n_before + 3                    # the car bus never stopped storing rows
    assert "reconnecting" not in states              # the primary never went down
    assert r.transports.get("ev") is ev2 or ev2.closed   # the secondary came back on a fresh adapter
    assert ev1.closed
    assert calls["can"] >= 3                         # one failed attempt, then success, with backoff
    assert handed[1][1] == {"can_channel": "x", "can_bus": "ev"}   # the entry's overrides reached detect_adapter
    with open(tmp_path / "state.json") as f:
        state = json.load(f)
    assert state["adapter_type"] == "fake" and state["adapter_name"] == "Car 1"
    buses = {a["bus"]: a for a in state["adapters"]}
    assert set(buses) == {"car", "ev"} and buses["ev"]["type"] == "fake"
    sid = store.conn.execute("SELECT adapters FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert [a["bus"] for a in json.loads(sid)] == ["car", "ev"]
    assert not any(k in json.loads(sid)[0] for k in ("alive", "connected"))


def test_a_silent_secondary_bus_is_reported_not_treated_as_asleep(two_buses):
    tmp_path, store = two_buses
    _tiles(tmp_path, ["soc", "vehicle"])
    car = FakeBusELM("car", "Car 1")
    ev = FakeBusELM("ev", "Ev 1", passive={})          # connected, nothing on the bus
    r = rd.Reader(interval=0, adapter_pref=None, store=store)
    r.entries = [{"type": "usb", "bus": "car"}, {"type": "can", "bus": "ev"}]
    r.transports = {"car": car, "ev": ev}
    rec, alive = run(r.poll_once())
    assert alive is True                               # the primary decides asleep
    assert rec["bus_alive"] == {"car": True, "ev": False}
    info = {a["bus"]: a for a in r.adapters_info()}
    assert info["ev"]["alive"] is False and info["ev"]["connected"] is True
    # no adapter on a bus at all: its items are skipped and said once
    r.transports = {"car": car}
    rec, alive = run(r.poll_once())
    assert "p421" not in rec["timing"] and alive is True
    assert any("no adapter on bus 'ev'" in s for s in r._said)


def test_single_adapter_record_shape_is_unchanged_but_for_the_new_keys(env):
    tmp_path, store = env
    r = rd.Reader(interval=0, adapter_pref="usb", store=store)
    r.entries = rd.adapter_entries("usb")
    elm = FakeBusELM("car", "Car 1", adapter_type="usb")
    r.transports = {"car": elm}
    rec, alive = run(r.poll_once())
    assert rec["bus_alive"] == {"car": True}
    assert r.adapters_info(static=True) == [{"bus": "car", "type": "usb", "name": "Car 1", "port": "mem",
                                             "listen_only": False, "speed": 1.0}]
    # the single-transport call still works: the tests and the bench pass one adapter
    r2 = rd.Reader(interval=0, adapter_pref=None, store=store)
    rec2, alive2 = run(r2.poll_once(elm))
    assert alive2 is True and rec2["soc"] == rec["soc"]


# ── the resolver ─────────────────────────────────────────────────────────

SPECS = {"v": {"sources": [{"key": "a_v", "item": "A", "confidence": "verified", "rate_hz": 0.5},
                           {"key": "b_v", "item": "B", "confidence": "verified", "rate_hz": 100},
                           {"key": "c_v", "item": "C", "confidence": "tentative", "rate_hz": 100}],
               "tolerance": 2.0}}
PERIOD = {"A": 0, "B": 0, "C": 0}
BUS = {"A": "car", "B": "ev", "C": "ev"}


def _resolve(cache, ages, pins=None, decoded=(), specs=SPECS):
    return rd.resolve_sources(specs, cache, ages, lambda i: PERIOD[i], pins, decoded, bus_of=lambda i: BUS[i])


@pytest.mark.parametrize("cache, ages, pins, want_src, want_v", [
    # confidence first: the tentative 100 Hz source never displaces a verified one
    ({"a_v": 10.0, "b_v": 10.5, "c_v": 11.0}, {"A": 1.0, "B": 1.0, "C": 0.0}, None, "ev:B", 10.5),
    # among verified: the fresher wins
    ({"a_v": 10.0, "b_v": 10.5}, {"A": 0.5, "B": 1.0}, None, "car:A", 10.0),
    # equal freshness: the faster wins
    ({"a_v": 10.0, "b_v": 10.5}, {"A": 1.0, "B": 1.0}, None, "ev:B", 10.5),
    # a pin beats everything, by source key …
    ({"a_v": 10.0, "b_v": 10.5, "c_v": 11.0}, {"A": 1.0, "B": 0.0, "C": 0.0}, {"v": "c_v"}, "ev:C", 11.0),
    # … or by bus:item
    ({"a_v": 10.0, "b_v": 10.5}, {"A": 2.0, "B": 0.0}, {"v": "car:A"}, "car:A", 10.0),
    # a stale source (older than 3 s for a fast-lane item) is out of the running
    ({"a_v": 10.0, "b_v": 10.5}, {"A": 4.0, "B": 1.0}, {"v": "a_v"}, "ev:B", 10.5),
    # a missing value is not a candidate
    ({"a_v": None, "b_v": 10.5}, {"A": 0.0, "B": 1.0}, None, "ev:B", 10.5),
])
def test_resolver_precedence(cache, ages, pins, want_src, want_v):
    ups, dis = _resolve(cache, ages, pins)
    assert ups["v_src"] == want_src and ups["v"] == want_v
    assert "v_disagree" not in ups and dis == {}


def test_resolver_stale_and_disagreement_and_decoded_keys():
    ups, dis = _resolve({"a_v": 10.0, "b_v": 10.5}, {"A": 9.0, "B": 9.0})
    assert ups == {"v_src": "stale"}                                   # the last value stands, no rewrite
    ups, dis = _resolve({"a_v": 10.0, "b_v": 13.0}, {"A": 0.0, "B": 1.0})
    assert ups["v"] == 10.0 and ups["v_disagree"] == {"a": "a_v", "b": "b_v", "delta": -3.0}
    assert dis == {"v": ups["v_disagree"]}
    ups, dis = _resolve({"a_v": 10.0, "b_v": 11.9}, {"A": 0.0, "B": 1.0})
    assert "v_disagree" not in ups                                     # inside tolerance
    ups, dis = _resolve({"a_v": 10.0}, {"A": 0.0}, decoded={"v"})
    assert ups == {"v_resolved": 10.0, "v_src": "car:A"}               # a decoder's key is never overwritten
    # slow items are fresh for 3× their period
    specs = {"v": {"sources": [{"key": "a_v", "item": "S", "confidence": "verified"}], "tolerance": None}}
    ups, _ = rd.resolve_sources(specs, {"a_v": 1.0}, {"S": 14.0}, lambda i: 5, bus_of=lambda i: "car")
    assert ups["v"] == 1.0
    ups, _ = rd.resolve_sources(specs, {"a_v": 1.0}, {"S": 16.0}, lambda i: 5, bus_of=lambda i: "car")
    assert ups == {"v_src": "stale"}
    assert rd.source_freshness_limit(0) == 3.0 and rd.source_freshness_limit(20) == 60.0


def test_leaf_current_is_resolved_beside_the_reported_value_and_disagreement_is_an_event(env, monkeypatch):
    tmp_path, store = env
    r = rd.Reader(interval=0, adapter_pref=None, store=store, budget=100)
    elm = FakeBusELM("car", "Car 1")
    rec, _ = run(r.poll_once(elm))                                     # lbc01 + lbc05 both polled on the first cycle
    assert rec["current_a"] == rec["hv_current2_a"]                    # decode()'s value, untouched
    assert rec["current_a_resolved"] == rec["hv_current2_a"]           # both verified; lbc01 is the fresher
    assert rec["current_a_src"] == "car:lbc01"
    fixture_delta = abs(rec["hv_current2_a"] - rec["g05_current_a"])
    assert ("current_a_disagree" in rec) == (fixture_delta > 3.0)
    # force a disagreement: the two sources 10 A apart
    monkeypatch.setattr(rd.VEHICLE, "decode",
                        lambda responses: ({"current_a": -1.0, "hv_current2_a": -1.0, "g05_current_a": -11.0,
                                            "pack_v": 380.0, "soc": 50.0}, True))
    rec, _ = run(r.poll_once(elm))
    assert rec["current_a_disagree"] == {"a": "hv_current2_a", "b": "g05_current_a", "delta": 10.0}
    assert rec["current_a"] == -1.0
    rec, _ = run(r.poll_once(elm))                                     # still disagreeing: no second event
    monkeypatch.setattr(rd.VEHICLE, "decode",
                        lambda responses: ({"current_a": -1.0, "hv_current2_a": -1.0, "g05_current_a": -1.5,
                                            "pack_v": 380.0, "soc": 50.0}, True))
    rec, _ = run(r.poll_once(elm))
    assert "current_a_disagree" not in rec
    evs = [(e["value"], e["prev"]) for e in store.events("source_disagree")]
    assert evs == [("current_a: hv_current2_a vs g05_current_a delta 10.0", None), ("current_a: agree", "disagree")]
    # apply_policy still ran on the reported value, under its own keys
    assert rec["current_adj_a"] is not None and rec["current_adj_src"]


def test_a_tile_option_pins_a_source(env):
    tmp_path, store = env
    with open(tmp_path / "tiles.json", "w") as f:
        json.dump({"tiles": [{"id": "soc", "enabled": True}, {"id": "power", "enabled": True},
                             {"id": "cur", "kind": "signal", "signal": "current_a", "type": "number",
                              "opts": {"source": "g05_current_a"}}]}, f)
    r = rd.Reader(interval=0, adapter_pref=None, store=store, budget=100)
    rec, _ = run(r.poll_once(FakeBusELM("car", "Car 1")))
    assert r.pins == {"current_a": "g05_current_a"}
    assert rec["current_a_src"] == "car:lbc05" and rec["current_a_resolved"] == rec["g05_current_a"]
    assert rec["current_a"] == rec["hv_current2_a"]
