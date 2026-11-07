# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A reply is only ever the answer to the request that asked for it.

Over MQTT the reply's CAN frames come back on a shared topic (``rx/7BB``) and
the façade takes whatever lands between its request and its acknowledgement.
When the link stalls, the reader gives up on a request, the bridge still runs
it, and the late reply lands inside the *next* request's window. On 2026-11-05
that stored a row with a state of charge of 419 %: a group-05 reply read with
group-01 offsets. Fourteen such rows are on record, every one over MQTT.

These tests rebuild that sequence with the real façade over ``MqttSource`` and
a fake broker client, using frames captured from the car. No broker, no
network, no hardware.

The three scenario tests failed on the code as it was (they were first
committed as strict expected failures: SOC 465 %, 1088 % and a silent 40 % for 80 %).
They pass since the façade proves each reply through replyproof.py.
"""

import asyncio
import json
import os

import pytest

from conftest import ROOT, FIXTURES  # noqa: F401  (sys.path is set up there)

import mqttsource as ms  # noqa: E402
from leaf_decoders import parse_isotp, decode_group01, decode_group05, decode_reading  # noqa: E402
from test_mqtt_source import FakeClient, CFG, ROOT_T, run  # noqa: E402

pytest.importorskip("cantransport")


# ── the car's own bytes ──────────────────────────────────────────────────

def captured(cmd):
    """The lines the car gave for one battery-controller group (a real capture)."""
    with open(os.path.join(FIXTURES, "session_leaf_ze0.json")) as f:
        return json.load(f)["frames"][0]["uds"]["79B"][cmd]


def simulated_group01(soc):
    """A group-01 reply from the simulator at a chosen state of charge. SIMULATED:
    the fixtures hold one real group-01 capture and the same-group case needs two."""
    from simulator import make_sim
    return make_sim(vehicle="leaf_ze0", knobs={"soc": soc}, seed=1).respond("2101", tx="79B")


TRUE = decode_group01(parse_isotp(captured("2101")))     # what the car really said: SOC 76.87 %, 383.87 V


# ── a reader, and a bridge that can be late ──────────────────────────────

def requests(client):
    return [json.loads(p) for t, p, q, r in client.published if t == f"{ROOT_T}/tx/uds"]


def deliver(client, lines, t0=100.0):
    for n, line in enumerate(lines):
        cid, _, d = line.partition(" ")
        client.deliver(f"{ROOT_T}/rx/{cid}", {"t": t0 + n * 0.005, "id": cid, "d": d})


def ack(client, req, frames):
    client.deliver(f"{ROOT_T}/tx/uds/{req['req']}", {"req": req["req"], "ok": True, "frames": frames})


async def open_reader():
    from elm327 import set_uds_target
    clients = []

    def factory(cid):
        clients.append(FakeClient())
        return clients[-1]

    elm = await ms.open_mqtt(log=lambda *a: None, cfg=CFG, client_factory=factory)
    await set_uds_target(elm, "79B", "7BB")
    return elm, clients[0]


async def abandoned(elm, client, cmd):
    """Ask, hear nothing in time, give up: what a stalled link does to a request."""
    lines = await elm.send(cmd, wait=0.0, timeout=0.05)
    assert lines == ["NO DATA"]
    return requests(client)[-1]


async def ask_while(elm, client, cmd, bridge):
    """Ask `cmd`; `bridge(req)` plays the far end once the request is out."""
    async def far_end():
        await asyncio.sleep(0.02)
        req = requests(client)[-1]
        assert req["data"] == f"{cmd[:2]} {cmd[2:]}"
        bridge(req)

    asyncio.ensure_future(far_end())
    return await elm.send(cmd, wait=0.05, timeout=1.0)


# ── the diagnosis, as arithmetic ─────────────────────────────────────────

def test_a_group05_reply_read_with_group01_offsets_is_the_spike_on_record():
    """Captured bytes, no transport: read the car's group-05 reply with the
    group-01 decoder and the stored spike's fingerprint comes out."""
    g05 = parse_isotp(captured("2105"))
    wrong, right = decode_group01(g05), decode_group05(g05)
    assert len(g05) == 74                                   # group 01 is 39 bytes: length alone tells them apart
    assert wrong["soc"] > 100 and wrong["pack_v"] < 10
    assert wrong["lv_volts"] == 63.999                      # FFFF: group 05's discharge flag, read as "12 V"
    assert wrong["g01_word24"] == right["g05_insulation_kohm"]
    raw = round(wrong["hv_current2_a"] * 1024)              # the "current" is two cell voltages
    assert (raw >> 16, raw & 0xFFFF) == (right["g05_cell_max_mv"], right["g05_cell_min_mv"])


def test_the_row_stored_on_2026_11_05_decodes_back_to_a_group05_reply():
    """readings.id 316152: SOC 419.44 %, pack 6.47 V, 254083.865 A, 12 V 63.999 V,
    Hx 0.64, capacity 425.9904 Ah, g01_word24 899. Every value is a group-05 field."""
    raw = round(254083.865 * 1024)
    assert (raw >> 16, raw & 0xFFFF) == (3970, 3958)        # cell max / min in mV, as that row's own group 05 had them
    assert round(63.999 * 1024) == 0xFFFF                   # the discharge flag
    assert round(0.64 * 100) == 64                          # segment delta 1
    assert round(0x400040 / 10000, 2) == 419.44             # segment deltas 2 and 3 (64, 64), straddled by the SOC field
    assert round(0x410040 / 10000, 4) == 425.9904           # segment deltas 4 and 5 (65, 64), straddled by capacity


# ── the fault, reproduced ────────────────────────────────────────────────

def test_a_late_reply_to_an_abandoned_request_is_not_decoded_as_the_next_answer(monkeypatch):
    """Today's event: the group-05 request is abandoned, group 01 is asked next,
    and the late group-05 reply arrives first inside that window."""
    monkeypatch.setattr(ms, "ACK_GRACE", 0.0)
    g05, g01 = captured("2105"), captured("2101")

    async def go():
        elm, client = await open_reader()
        stale = await abandoned(elm, client, "2105")

        def bridge(fresh):
            deliver(client, g05)                            # the bridge ran the old request after all
            ack(client, stale, len(g05))                    # nobody is waiting for this ack any more
            deliver(client, g01, t0=101.0)
            ack(client, fresh, len(g01))

        got = await ask_while(elm, client, "2101", bridge)
        await elm.close()
        return got

    rec = decode_reading({"2101": run(go())})
    assert rec.get("soc") == TRUE["soc"], f"decoded SOC {rec.get('soc')} %, the car said {TRUE['soc']} %"
    assert rec.get("pack_v") == TRUE["pack_v"] and rec.get("lv_volts") == TRUE["lv_volts"]


def test_another_requesters_reply_in_the_window_is_not_decoded_as_ours():
    """The 2026-10-08 and 2026-10-12 class: a second requester on the same bridge
    asks for the cell voltages and its 192-byte reply lands in our window."""
    g02, g01 = captured("2102"), captured("2101")

    async def go():
        elm, client = await open_reader()

        def bridge(ours):
            deliver(client, g02)                            # frames of a reply nobody here asked for
            client.deliver(f"{ROOT_T}/tx/uds/veh7", {"req": "veh7", "ok": True, "frames": len(g02)})
            deliver(client, g01, t0=101.0)
            ack(client, ours, len(g01))

        got = await ask_while(elm, client, "2101", bridge)
        await elm.close()
        return got

    rec = decode_reading({"2101": run(go())})
    assert rec.get("soc") == TRUE["soc"], f"decoded SOC {rec.get('soc')} %, the car said {TRUE['soc']} %"


def test_a_late_reply_of_the_same_group_is_not_taken_for_the_fresh_one(monkeypatch):
    """The silent case. Echo and length are right, the values are plausible, and
    they are old: only knowing which request a reply answers can catch it."""
    monkeypatch.setattr(ms, "ACK_GRACE", 0.0)
    old, new = simulated_group01(40.0), simulated_group01(80.0)

    async def go():
        elm, client = await open_reader()
        stale = await abandoned(elm, client, "2101")

        def bridge(fresh):
            deliver(client, old)
            ack(client, stale, len(old))
            deliver(client, new, t0=101.0)
            ack(client, fresh, len(new))

        got = await ask_while(elm, client, "2101", bridge)
        await elm.close()
        return got

    rec = decode_reading({"2101": run(go())})
    assert rec.get("soc") == 80.0, f"decoded SOC {rec.get('soc')} %, the fresh reply said 80.0 %"


# ── what must stay true while that is fixed ──────────────────────────────

def test_a_reply_that_arrives_in_time_still_decodes():
    g01 = captured("2101")

    async def go():
        elm, client = await open_reader()

        def bridge(req):
            deliver(client, g01)
            ack(client, req, len(g01))

        got = await ask_while(elm, client, "2101", bridge)
        await elm.close()
        return got

    got = run(go())
    assert got == g01
    assert decode_reading({"2101": got})["soc"] == TRUE["soc"]
