import asyncio
import json
from contextlib import asynccontextmanager, suppress

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from hdmi_follow import tv, tv_session


@asynccontextmanager
async def fake_tv(monkeypatch, handler):
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            tv,
            "_connect",
            lambda ip: connect(f"ws://127.0.0.1:{port}", proxy=None, close_timeout=0.1),
        )
        yield


async def registered(ws):
    message = json.loads(await ws.recv())
    assert message["type"] == "register"
    assert message["payload"]["manifest"] == tv.MANIFEST
    await ws.send(
        json.dumps({"id": "register", "type": "registered", "payload": {"client-key": "key"}})
    )


async def acknowledge(ws, request, value=True):
    await ws.send(
        json.dumps({"id": request["id"], "type": "response", "payload": {"returnValue": value}})
    )


async def test_multiple_switches_reuse_one_registration_and_unique_ids(monkeypatch):
    connections = []
    commands = []

    async def handler(ws):
        connections.append(ws)
        await registered(ws)
        async for frame in ws:
            request = json.loads(frame)
            commands.append(request)
            await ws.send(json.dumps({"id": "unrelated", "payload": {"returnValue": False}}))
            await acknowledge(ws, request)

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        try:
            await session.switch("HDMI_2")
            await session.switch("HDMI_1")
        finally:
            await session.close()
    assert len(connections) == 1
    assert len({request["id"] for request in commands}) == 2
    assert [request["payload"] for request in commands] == [
        {"id": "com.webos.app.hdmi2"},
        {"id": "com.webos.app.hdmi1"},
    ]


async def test_keep_warm_only_registers_without_input_commands(monkeypatch):
    frames = []
    connected = asyncio.Event()
    monkeypatch.setattr(tv_session, "RECONNECT_INTERVAL", 0.01)

    async def handler(ws):
        frames.append(json.loads(await ws.recv()))
        await ws.send(
            json.dumps({"id": "register", "type": "registered", "payload": {"client-key": "key"}})
        )
        connected.set()
        async for frame in ws:
            frames.append(json.loads(frame))

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        task = asyncio.create_task(session.keep_warm())
        await asyncio.wait_for(connected.wait(), 1)
        await asyncio.sleep(0.04)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.close()
    assert [frame["type"] for frame in frames] == ["register"]


async def test_stale_before_connection_never_connects(monkeypatch):
    monkeypatch.setattr(tv, "_connect", lambda ip: pytest.fail("Must not connect stale command"))
    session = tv_session.Session("192.168.0.20", "key")
    with pytest.raises(tv.StaleRequest):
        await session.switch("HDMI_1", lambda: False)
    await session.close()


async def test_rechecks_ownership_after_registration(monkeypatch):
    commands = []
    calls = iter([True, False])

    async def handler(ws):
        await registered(ws)
        async for frame in ws:
            commands.append(json.loads(frame))

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        with pytest.raises(tv.StaleRequest):
            await session.switch("HDMI_1", lambda: next(calls))
        await session.close()
    assert not commands


async def test_cancelled_reply_discards_socket_and_next_command_registers_cleanly(monkeypatch):
    waiting = asyncio.Event()
    first_closed = asyncio.Event()
    connections = []
    commands = []

    async def handler(ws):
        connections.append(ws)
        number = len(connections)
        await registered(ws)
        command = json.loads(await ws.recv())
        commands.append(command)
        if number == 1:
            waiting.set()
            await ws.wait_closed()
            first_closed.set()
        else:
            await ws.send(json.dumps({"id": commands[0]["id"], "payload": {"returnValue": False}}))
            await acknowledge(ws, command)
            await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        pending = asyncio.create_task(session.switch("HDMI_2"))
        await asyncio.wait_for(waiting.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(first_closed.wait(), 1)
        await session.switch("HDMI_1")
        await session.close()
    assert len(connections) == 2
    assert commands[0]["id"] != commands[1]["id"]


async def test_closed_connection_reconnects_before_next_command(monkeypatch):
    connections = []

    async def handler(ws):
        connections.append(ws)
        await registered(ws)
        command = json.loads(await ws.recv())
        await acknowledge(ws, command)
        await ws.close()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        await session.switch("HDMI_2")
        await session._ws.wait_closed()
        await session.switch("HDMI_1")
        await session.close()
    assert len(connections) == 2


async def test_auth_rejection_disables_all_automatic_retries(monkeypatch, caplog):
    registrations = []
    monkeypatch.setattr(tv_session, "RECONNECT_INTERVAL", 0.01)

    async def handler(ws):
        registrations.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"id": "register", "type": "error", "error": "private-key"}))
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "private-key")
        await asyncio.wait_for(session.keep_warm(), 1)
        await session.keep_warm()
        with pytest.raises(tv.PairingRequired):
            await session.switch("HDMI_1")
        await session.close()
    assert len(registrations) == 1
    assert "private-key" not in caplog.text


@pytest.mark.parametrize("value", [False, "true", None])
async def test_rejected_ack_invalidates_connection(monkeypatch, value):
    async def handler(ws):
        await registered(ws)
        command = json.loads(await ws.recv())
        await acknowledge(ws, command, value)
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        with pytest.raises(tv.TVError):
            await session.switch("HDMI_1")
        assert session._ws is None
        await session.close()


async def test_command_timeout_discards_pending_connection(monkeypatch):
    closed = asyncio.Event()
    monkeypatch.setattr(tv_session, "COMMAND_TIMEOUT", 0.05)

    async def handler(ws):
        await registered(ws)
        await ws.recv()
        await ws.wait_closed()
        closed.set()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        with pytest.raises(TimeoutError):
            await session.switch("HDMI_1")
        assert session._ws is None
        await asyncio.wait_for(closed.wait(), 1)
        await session.close()


async def test_invalid_target_never_connects(monkeypatch):
    monkeypatch.setattr(tv, "_connect", lambda ip: pytest.fail("Must not connect invalid input"))
    session = tv_session.Session("192.168.0.20", "key")
    with pytest.raises(ValueError):
        await session.switch("HDMI_9")
    await session.close()


async def test_closed_session_cannot_reconnect(monkeypatch):
    monkeypatch.setattr(tv, "_connect", lambda ip: pytest.fail("Must not reopen a closed session"))
    session = tv_session.Session("192.168.0.20", "key")
    await session.close()
    with pytest.raises(tv.TVError, match="closed"):
        await session.switch("HDMI_1")


async def test_cancelled_lock_waiter_does_not_abort_active_switch(monkeypatch):
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(ws):
        await registered(ws)
        command = json.loads(await ws.recv())
        received.set()
        await release.wait()
        await acknowledge(ws, command)
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        active = asyncio.create_task(session.switch("HDMI_2"))
        await asyncio.wait_for(received.wait(), 1)
        waiter = asyncio.create_task(session.switch("HDMI_1"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await asyncio.wait_for(active, 1)
        assert session._ws is not None
        await session.close()


async def test_authentication_timeout_closes_without_launch(monkeypatch):
    frames = []
    closed = asyncio.Event()
    monkeypatch.setattr(tv_session, "COMMAND_TIMEOUT", 0.05)

    async def handler(ws):
        frames.append(json.loads(await ws.recv()))
        await ws.wait_closed()
        closed.set()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        with pytest.raises(TimeoutError):
            await session.switch("HDMI_1")
        await asyncio.wait_for(closed.wait(), 1)
        await session.close()
    assert [frame["type"] for frame in frames] == ["register"]


async def test_keep_warm_detects_missing_pong_and_reconnects_without_launch(monkeypatch):
    connections = []
    commands = []
    first_closed = asyncio.Event()
    reconnected = asyncio.Event()
    monkeypatch.setattr(tv_session, "HEALTH_TIMEOUT", 0.03)
    monkeypatch.setattr(tv_session, "RECONNECT_INTERVAL", 0.01)

    async def handler(ws):
        connections.append(ws)
        number = len(connections)
        await registered(ws)
        if number == 2:
            reconnected.set()
        with suppress(ConnectionClosed):
            async for frame in ws:
                commands.append(json.loads(frame))
        if number == 1:
            first_closed.set()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        await session._warm_once()
        assert session._ws is not None

        async def missing_pong():
            return asyncio.get_running_loop().create_future()

        monkeypatch.setattr(session._ws, "ping", missing_pong)
        task = asyncio.create_task(session.keep_warm())
        await asyncio.wait_for(first_closed.wait(), 1)
        await asyncio.wait_for(reconnected.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.close()
    assert len(connections) == 2
    assert commands == []


async def test_idle_health_does_not_queue_behind_active_switch(monkeypatch):
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(ws):
        await registered(ws)
        command = json.loads(await ws.recv())
        received.set()
        await release.wait()
        await acknowledge(ws, command)
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", "key")
        active = asyncio.create_task(session.switch("HDMI_1"))
        await asyncio.wait_for(received.wait(), 1)
        await asyncio.wait_for(session._warm_once(), 0.1)
        assert not active.done()
        release.set()
        await active
        await session.close()
