from unittest.mock import AsyncMock

import pytest

from hdmi_follow import runner, windows_usb
from hdmi_follow.config import Config, default_config
from hdmi_follow.ownership import Transition

HUB = r"USB\VID_0557&PID_2414\A&2648020B&0&9"


@pytest.fixture
def config():
    return Config("192.168.0.20", "HDMI_1", (), "HDMI_2", switch_hub=HUB)


def test_keyboard_unplug_cannot_change_hub_ownership(config, monkeypatch):
    monkeypatch.setattr(windows_usb, "is_present", lambda identity: identity == HUB)
    monkeypatch.setattr(
        runner, "enumerate_devices", lambda: pytest.fail("Must not inspect keyboard")
    )
    assert runner.sample_ownership(config)


def test_hub_removal_is_not_keyboard_presence(config, monkeypatch):
    monkeypatch.setattr(windows_usb, "is_present", lambda identity: False)
    assert not runner.sample_ownership(config)


def test_inventory_failure_is_unknown_not_disconnect(config, monkeypatch):
    def fail(_):
        raise windows_usb.USBEnumerationError("Transient failure")

    monkeypatch.setattr(windows_usb, "is_present", fail)
    with pytest.raises(windows_usb.USBEnumerationError):
        runner.sample_ownership(config)


def test_hub_configuration_roundtrips(config, tmp_path):
    path = tmp_path / default_config().name
    config.save(path)
    assert Config.load(path) == config


@pytest.mark.parametrize("hub", ["0557:2414", "USB\\ROOT_HUB30", "USB\\VID_0557&PID_2414\\bad\n"])
def test_invalid_hub_id_rejected(hub):
    with pytest.raises(ValueError):
        Config("192.168.0.20", "HDMI_1", (), switch_hub=hub)


async def test_runner_sends_on_reusable_session(config):
    from unittest.mock import create_autospec

    from hdmi_follow.tv_session import Session

    session = create_autospec(Session, instance=True)

    def current():
        return True

    await runner.follow_transition(config, "key", Transition(False, 1), current, session)
    session.switch.assert_awaited_once_with("HDMI_2", current)


async def test_runner_closes_warm_session_on_stop(config, tmp_path):
    from unittest.mock import create_autospec

    from hdmi_follow.tv_session import Session

    session = create_autospec(Session, instance=True)
    session.keep_warm = AsyncMock()
    stop = tmp_path / "stop"
    stop.touch()
    await runner.run(config, "key", stop, session)
    session.close.assert_awaited_once()


@pytest.mark.parametrize("command", ["start", "run", "install"])
def test_mac_rejects_windows_hub_mode_before_starting(config, tmp_path, monkeypatch, command):
    from hdmi_follow import cli

    path = tmp_path / "config.json"
    config.save(path)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Windows-only"):
        cli.execute(cli.parser().parse_args([command, "--config", str(path)]))


async def test_lost_warm_connection_gets_one_clean_retry(config):
    from unittest.mock import create_autospec

    from websockets.exceptions import ConnectionClosed

    from hdmi_follow.tv_session import Session

    session = create_autospec(Session, instance=True)
    session.switch.side_effect = [ConnectionClosed(None, None), None]
    await runner.follow_transition(config, "key", Transition(False, 1), lambda: True, session)
    assert session.switch.await_count == 2


async def test_real_socket_disconnect_reconnects_once(config, monkeypatch):
    import json

    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    from hdmi_follow import tv
    from hdmi_follow.tv_session import Session

    commands = []

    async def server(ws):
        await ws.recv()
        await ws.send(
            json.dumps({"id": "register", "type": "registered", "payload": {"client-key": "key"}})
        )
        command = json.loads(await ws.recv())
        commands.append(command)
        if len(commands) == 1:
            ws.transport.abort()
        else:
            await ws.send(
                json.dumps(
                    {"id": command["id"], "type": "response", "payload": {"returnValue": True}}
                )
            )
            await ws.wait_closed()

    async with serve(server, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            tv,
            "_connect",
            lambda _: connect(f"ws://127.0.0.1:{port}", proxy=None, close_timeout=0.1),
        )
        session = Session(config.tv_ip, "key")
        try:
            await runner.follow_transition(
                config, "key", Transition(False, 1), lambda: True, session
            )
        finally:
            await session.close()
    assert len(commands) == 2
    assert commands[0]["id"] != commands[1]["id"]
