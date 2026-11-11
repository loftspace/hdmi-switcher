import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from hdmi_follow import tv


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


async def test_switch_uses_paired_key_and_exact_hdmi_launch(monkeypatch, caplog) -> None:
    received = []

    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"id": "unrelated", "type": "response"}))
        await ws.send(
            json.dumps(
                {"id": "register", "type": "registered", "payload": {"client-key": "private-key"}}
            )
        )
        received.append(json.loads(await ws.recv()))
        await ws.send(
            json.dumps({"id": "command", "type": "response", "payload": {"returnValue": True}})
        )

    async with fake_tv(monkeypatch, handler):
        await tv.switch("192.168.0.20", "private-key", "HDMI_3")
    assert received[0]["payload"]["client-key"] == "private-key"
    assert received[1] == {
        "id": "command",
        "type": "request",
        "uri": "ssap://system.launcher/launch",
        "payload": {"id": "com.webos.app.hdmi3"},
    }
    assert "private-key" not in caplog.text


async def test_explicit_pair_waits_for_prompt_and_returns_key(monkeypatch) -> None:
    async def handler(ws):
        register = json.loads(await ws.recv())
        assert "client-key" not in register["payload"]
        await ws.send(
            json.dumps({"id": "register", "type": "response", "payload": {"pairingType": "PROMPT"}})
        )
        await ws.send(
            json.dumps(
                {"id": "register", "type": "registered", "payload": {"client-key": "new-key"}}
            )
        )

    async with fake_tv(monkeypatch, handler):
        assert await tv.pair("192.168.0.20") == "new-key"


async def test_auth_rejection_does_not_send_hdmi_command_or_leak_key(monkeypatch, caplog) -> None:
    received = []

    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        await ws.send(
            json.dumps({"id": "register", "type": "error", "error": "invalid private-key"})
        )
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        with pytest.raises(tv.PairingRequired) as error:
            await tv.switch("192.168.0.20", "private-key", "HDMI_1")
    assert len(received) == 1
    assert "private-key" not in str(error.value) + caplog.text


async def test_revoked_pairing_does_not_wait_for_user_or_launch(monkeypatch) -> None:
    async def handler(ws):
        await ws.recv()
        await ws.send(
            json.dumps({"id": "register", "type": "response", "payload": {"pairingType": "PROMPT"}})
        )
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        with pytest.raises(tv.PairingRequired):
            await tv.switch("192.168.0.20", "revoked", "HDMI_1")


async def test_stale_ownership_after_registration_prevents_command(monkeypatch) -> None:
    command_received = False

    async def handler(ws):
        nonlocal command_received
        await ws.recv()
        await ws.send(
            json.dumps({"id": "register", "type": "registered", "payload": {"client-key": "key"}})
        )
        async for _ in ws:
            command_received = True

    async with fake_tv(monkeypatch, handler):
        with pytest.raises(tv.StaleRequest):
            await tv.switch("192.168.0.20", "key", "HDMI_1", lambda: False)
    assert not command_received


async def test_tv_return_value_false_is_not_success(monkeypatch) -> None:
    async def handler(ws):
        await ws.recv()
        await ws.send(
            json.dumps({"id": "register", "type": "registered", "payload": {"client-key": "key"}})
        )
        await ws.recv()
        await ws.send(
            json.dumps({"id": "command", "type": "response", "payload": {"returnValue": False}})
        )

    async with fake_tv(monkeypatch, handler):
        with pytest.raises(tv.TVError):
            await tv.switch("192.168.0.20", "key", "HDMI_1")


@pytest.mark.parametrize("payload", [{}, {"id": "com.webos.app.hdmi1"}, {"returnValue": "true"}])
async def test_malformed_command_acknowledgement_is_not_success(monkeypatch, payload) -> None:
    async def handler(ws):
        await ws.recv()
        await ws.send(
            json.dumps({"id": "register", "type": "registered", "payload": {"client-key": "key"}})
        )
        await ws.recv()
        await ws.send(json.dumps({"id": "command", "type": "response", "payload": payload}))

    async with fake_tv(monkeypatch, handler):
        with pytest.raises(tv.TVError):
            await tv.switch("192.168.0.20", "key", "HDMI_1")


async def test_cancelling_pending_handshake_closes_without_input_command(monkeypatch) -> None:
    registered = asyncio.Event()
    closed = asyncio.Event()

    async def handler(ws):
        await ws.recv()
        registered.set()
        await ws.wait_closed()
        closed.set()

    async with fake_tv(monkeypatch, handler):
        task = asyncio.create_task(tv.switch("192.168.0.20", "key", "HDMI_1"))
        await registered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), timeout=1)


async def test_invalid_input_never_connects(monkeypatch) -> None:
    monkeypatch.setattr(tv, "_connect", lambda ip: pytest.fail("Must reject before connecting"))
    with pytest.raises(ValueError):
        await tv.switch("192.168.0.20", "key", "HDMI_9")
