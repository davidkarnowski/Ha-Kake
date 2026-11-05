# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""tools/laggy_link.py — the proxy that makes a desk link behave like a tunnel.

Loopback sockets on ephemeral ports, an echo server as the far end. What is
held to: bytes arrive unchanged and in order whatever is done to the link; a
delay is at least what was asked; a stall releases everything it held, in
order; a hold starts with the very chunk that carries its pattern; and the
proxy refuses to listen anywhere but the loopback address.
"""

import asyncio
import os
import socket
import sys
import time

import pytest

from conftest import ROOT  # noqa: F401  (sys.path is set up there)

sys.path.insert(0, os.path.join(ROOT, "tools"))
import laggy_link as ll  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def echo_server():
    async def handle(reader, writer):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, family=socket.AF_INET)
    return server, server.sockets[0].getsockname()[1]


async def rig(**kw):
    server, port = await echo_server()
    link = await ll.LaggyLink(0, port, **kw).start()
    reader, writer = await asyncio.open_connection("127.0.0.1", link.listen_port, family=socket.AF_INET)
    return server, link, reader, writer


async def teardown(server, link, writer):
    writer.close()
    await link.stop()
    server.close()
    await server.wait_closed()


def test_bytes_pass_through_unchanged_in_both_directions():
    async def go():
        server, link, reader, writer = await rig()
        payload = bytes(range(256)) * 40
        writer.write(payload)
        await writer.drain()
        got = await asyncio.wait_for(reader.readexactly(len(payload)), 2.0)
        stats = dict(link.stats)
        await teardown(server, link, writer)
        return payload, got, stats

    payload, got, stats = run(go())
    assert got == payload
    assert stats["connections"] == 1 and stats["bytes_up"] == stats["bytes_down"] == len(payload)


def test_a_delay_is_at_least_what_was_asked_each_way():
    async def go():
        server, link, reader, writer = await rig(delay_up=0.08, delay_down=0.05)
        t0 = time.monotonic()
        writer.write(b"ping")
        await writer.drain()
        got = await asyncio.wait_for(reader.readexactly(4), 2.0)
        took = time.monotonic() - t0
        await teardown(server, link, writer)
        return got, took

    got, took = run(go())
    assert got == b"ping"
    assert 0.13 <= took < 1.0


def test_a_stall_holds_everything_and_releases_it_in_order():
    async def go():
        server, link, reader, writer = await rig()
        writer.write(b"a")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(1), 2.0) == b"a"
        link.stall(0.3, ll.UP)
        t0 = time.monotonic()
        for chunk in (b"b", b"c", b"d"):
            writer.write(chunk)
            await writer.drain()
            await asyncio.sleep(0.02)
        with pytest.raises(asyncio.TimeoutError):          # nothing gets through while it is stalled
            await asyncio.wait_for(reader.readexactly(1), 0.1)
        got = await asyncio.wait_for(reader.readexactly(3), 2.0)
        took = time.monotonic() - t0
        await teardown(server, link, writer)
        return got, took, link.stats["stalls"]

    got, took, stalls = run(go())
    assert got == b"bcd"
    assert took >= 0.28 and stalls == 1


def test_a_stall_in_one_direction_leaves_the_other_alone():
    async def go():
        server, link, reader, writer = await rig()
        link.stall(0.4, ll.DOWN)
        writer.write(b"x")
        await writer.drain()
        await asyncio.sleep(0.1)
        up_seen = link.stats["bytes_up"]                   # went up at once ...
        down_seen = link.stats["bytes_down"]               # ... and its echo is held on the way down
        got = await asyncio.wait_for(reader.readexactly(1), 2.0)
        await teardown(server, link, writer)
        return up_seen, down_seen, got

    up_seen, down_seen, got = run(go())
    assert up_seen == 1 and down_seen == 0 and got == b"x"


def test_a_hold_starts_with_the_chunk_that_carries_the_pattern():
    async def go():
        server, link, reader, writer = await rig()
        link.hold_on('"data":"21 05"', 0.3)
        writer.write(b'{"req":"a","data":"21 01"}')
        await writer.drain()
        first = await asyncio.wait_for(reader.readexactly(26), 2.0)     # no pattern: straight through
        t0 = time.monotonic()
        writer.write(b'{"req":"b","data":"21 05"}')
        await writer.drain()
        await asyncio.sleep(0.02)
        writer.write(b'{"req":"c","data":"21 01"}')                    # queues behind the held one
        await writer.drain()
        rest = await asyncio.wait_for(reader.readexactly(52), 2.0)
        took = time.monotonic() - t0
        await teardown(server, link, writer)
        return first, rest, took, dict(link.stats), list(link._holds)

    first, rest, took, stats, holds = run(go())
    assert first == b'{"req":"a","data":"21 01"}'
    assert rest == b'{"req":"b","data":"21 05"}{"req":"c","data":"21 01"}'
    assert took >= 0.28
    assert stats["holds_fired"] == 1 and holds == []                    # a hold fires once


def test_a_pattern_split_across_two_chunks_still_fires():
    async def go():
        server, link, reader, writer = await rig()
        link.hold_on("ABCDEF", 0.2)
        t0 = time.monotonic()
        writer.write(b"xxABC")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(b"DEFyy")
        await writer.drain()
        got = await asyncio.wait_for(reader.readexactly(10), 2.0)
        took = time.monotonic() - t0
        await teardown(server, link, writer)
        return got, took, link.stats["holds_fired"]

    got, took, fired = run(go())
    assert got == b"xxABCDEFyy" and fired == 1 and took >= 0.2


def test_stopping_does_not_wait_for_a_client_that_is_still_connected():
    async def go():
        server, link, reader, writer = await rig()
        writer.write(b"x")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(1), 2.0) == b"x"
        await asyncio.wait_for(link.stop(), 2.0)                       # the client never closed its end
        writer.close()
        server.close()
        await server.wait_closed()

    run(go())


def test_it_listens_on_the_loopback_address_only():
    with pytest.raises(ValueError):
        ll.LaggyLink(0, 1883, listen_host="0.0.0.0")
    with pytest.raises(ValueError):
        ll.LaggyLink(0, 1883, listen_host="192.0.2.10")


def test_a_remote_target_needs_to_be_asked_for():
    with pytest.raises(ValueError):
        ll.LaggyLink(0, 1883, target_host="192.0.2.10")
    assert ll.LaggyLink(0, 1883, target_host="192.0.2.10", allow_remote_target=True).target_host == "192.0.2.10"


def test_bad_directions_and_empty_patterns_are_refused():
    async def go():
        link = ll.LaggyLink(0, 1)
        with pytest.raises(ValueError):
            link.stall(1, "sideways")
        with pytest.raises(ValueError):
            link.hold_on("", 1)

    run(go())


def test_the_thread_wrapper_serves_and_takes_controls_from_another_thread():
    async def far_end(started, stop, port_box):
        server, port = await echo_server()
        port_box.append(port)
        started.set()
        while not stop.is_set():
            await asyncio.sleep(0.01)
        server.close()
        await server.wait_closed()

    import threading
    started, stop, port_box = threading.Event(), threading.Event(), []
    t = threading.Thread(target=lambda: asyncio.run(far_end(started, stop, port_box)), daemon=True)
    t.start()
    assert started.wait(2.0)
    link = ll.LaggyLinkThread(listen_port=0, target_port=port_box[0]).start()
    try:
        link.stall(0.25, ll.UP)
        s = socket.create_connection(("127.0.0.1", link.port), timeout=2.0)
        t0 = time.monotonic()
        s.sendall(b"hello")
        got = b""
        while len(got) < 5:
            chunk = s.recv(16)                  # times out after 2 s; an empty read is the far end closing
            if not chunk:
                break
            got += chunk
        took = time.monotonic() - t0
        s.close()
        assert got == b"hello", f"the link closed early: {link.stats} {link.events}"
        assert took >= 0.2
        assert link.stats["stalls"] == 1 and any("stall" in text for _, text in link.events)
    finally:
        link.stop()
        stop.set()
        t.join(timeout=2.0)


def test_command_lines_drive_the_controls():
    async def go():
        link = ll.LaggyLink(0, 1)
        said = []
        assert ll.run_command(link, "delay 200 up", said.append) is True
        assert ll.run_command(link, "stall 0.01", said.append) is True
        assert ll.run_command(link, 'hold "data":"21 05" 12.5', said.append) is True
        assert ll.run_command(link, "nonsense", said.append) is True
        assert ll.run_command(link, "stall", said.append) is True          # missing argument: said, not raised
        assert ll.run_command(link, "quit", said.append) is False
        return link, said

    link, said = run(go())
    assert link.delay == {ll.UP: 0.2, ll.DOWN: 0.0}
    assert link._holds[0]["pattern"] == b'"data":"21 05"' and link._holds[0]["seconds"] == 12.5
    assert any("commands:" in s for s in said) and any("not understood" in s for s in said)
