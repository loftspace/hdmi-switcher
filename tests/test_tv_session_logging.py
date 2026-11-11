import asyncio
import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.protocol import State

from hdmi_follow import tv, tv_session

SECRET = "never-log-this-pairing-key"


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


async def register(ws):
    await ws.recv()
    await ws.send(
        json.dumps({"id": "register", "type": "registered", "payload": {"client-key": SECRET}})
    )


def messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == tv_session.__name__]


async def test_send_reply_ids_targets_and_durations_correlate(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=tv_session.__name__)

    async def handler(ws):
        await register(ws)
        async for frame in ws:
            request = json.loads(frame)
            await ws.send(
                json.dumps(
                    {"id": request["id"], "type": "response", "payload": {"returnValue": True}}
                )
            )

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", SECRET)
        await session.switch("HDMI_2")
        await session.switch("HDMI_1")
        await session.close()
    records = messages(caplog)
    for request_id, target in [("switch-1", "HDMI_2"), ("switch-2", "HDMI_1")]:
        assert f"TV switch send id={request_id} target={target}" in records
        assert any(
            item.startswith(f"TV switch reply id={request_id} target={target} duration_ms=")
            for item in records
        )
    assert records.count("TV connection attempt") == 1
    assert records.count("TV connection connected") == 1
    assert SECRET not in caplog.text


async def test_repeated_connection_failures_are_bounded_without_exception_body(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=tv_session.__name__)
    monkeypatch.setattr(tv_session, "RECONNECT_INTERVAL", 0.001)
    retried = asyncio.Event()
    calls = 0

    async def unavailable(ip):
        nonlocal calls
        calls += 1
        if calls >= 5:
            retried.set()
        raise OSError(f"server included {SECRET}")

    monkeypatch.setattr(tv, "_connect", unavailable)
    session = tv_session.Session("192.168.0.20", SECRET)
    task = asyncio.create_task(session.keep_warm())
    await asyncio.wait_for(retried.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.close()
    records = messages(caplog)
    assert records.count("TV connection attempt") == 1
    assert records.count("TV connection unavailable error=OSError") == 1
    assert SECRET not in caplog.text


async def test_successful_maintenance_pings_do_not_generate_log_spam(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=tv_session.__name__)

    async def handler(ws):
        await register(ws)
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", SECRET)
        await session._warm_once()
        baseline = messages(caplog)
        for _ in range(8):
            await session._warm_once()
        assert messages(caplog) == baseline
        await session.close()


async def test_repeated_health_failures_suppress_noise_until_actual_recovery(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=tv_session.__name__)
    monkeypatch.setattr(tv_session, "RECONNECT_INTERVAL", 0.001)
    monkeypatch.setattr(tv_session, "HEALTH_TIMEOUT", 0.01)
    recovered = asyncio.Event()
    pings = 0

    class Socket:
        state = State.OPEN
        transport = Mock()

        async def ping(self):
            nonlocal pings
            pings += 1
            result = asyncio.get_running_loop().create_future()
            if pings >= 4:
                result.set_result(None)
                recovered.set()
            return result

    async def connecting(ip):
        return Socket()

    async def registering(ws, key, allow_pair):
        return key

    monkeypatch.setattr(tv, "_connect", connecting)
    monkeypatch.setattr(tv, "_register", registering)
    session = tv_session.Session("192.168.0.20", SECRET)
    task = asyncio.create_task(session.keep_warm())
    await asyncio.wait_for(recovered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.close()
    records = messages(caplog)
    assert records.count("TV connection attempt") == 1
    assert records.count("TV connection unavailable error=TimeoutError") == 1
    assert records.count("TV connection recovered error=TimeoutError") == 1
    assert SECRET not in caplog.text


async def test_pairing_rejection_is_logged_once_without_rejected_body(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=tv_session.__name__)

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"id": "register", "type": "error", "error": SECRET}))
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", SECRET)
        await session.keep_warm()
        await session.keep_warm()
        await session.close()
    assert messages(caplog).count("TV pairing rejected; automatic reconnect disabled") == 1
    assert SECRET not in caplog.text


@pytest.mark.parametrize("cancel", [True, False])
async def test_interrupted_switch_logs_request_and_failure_type(monkeypatch, caplog, cancel):
    caplog.set_level(logging.INFO, logger=tv_session.__name__)
    monkeypatch.setattr(tv_session, "COMMAND_TIMEOUT", 0.05)
    received = asyncio.Event()

    async def handler(ws):
        await register(ws)
        await ws.recv()
        received.set()
        await ws.wait_closed()

    async with fake_tv(monkeypatch, handler):
        session = tv_session.Session("192.168.0.20", SECRET)
        task = asyncio.create_task(session.switch("HDMI_2"))
        await asyncio.wait_for(received.wait(), 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        await session.close()
    outcome, error_type = ("cancelled", "CancelledError") if cancel else ("timeout", "TimeoutError")
    assert any(
        item.startswith(
            f"TV switch {outcome} id=switch-1 target=HDMI_2 error={error_type} duration_ms="
        )
        for item in messages(caplog)
    )
    assert SECRET not in caplog.text
