from unittest.mock import AsyncMock

from hdmi_follow import cli
from hdmi_follow.config import Config, Device, key_path, write_private_json


def test_inputs_uses_supported_lg_endpoint(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    Config("192.168.0.20", "HDMI_1", (Device(1, 2),)).save(path)
    write_private_json(key_path(path), {"tv_ip": "192.168.0.20", "client_key": "key"})
    request = AsyncMock(return_value={"returnValue": True, "devices": []})
    monkeypatch.setattr(cli.tv, "request", request)
    cli.execute(cli.parser().parse_args(["inputs", "--config", str(path)]))
    request.assert_awaited_once_with("192.168.0.20", "key", "ssap://tv/getExternalInputList")


def test_setup_does_not_overwrite_configuration(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("keep", encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="exists"):
        cli.execute(
            cli.parser().parse_args(
                [
                    "setup",
                    "--config",
                    str(path),
                    "--tv-ip",
                    "192.168.0.20",
                    "--own-input",
                    "HDMI_1",
                    "--device",
                    "1234:5678",
                ]
            )
        )
    assert path.read_text() == "keep"


def test_frozen_background_child_resets_packager_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    Config("192.168.0.20", "HDMI_1", (Device(1, 2),)).save(path)
    write_private_json(key_path(path), {"tv_ip": "192.168.0.20", "client_key": "key"})
    calls = []
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    cli.execute(cli.parser().parse_args(["start", "--config", str(path)]))
    assert calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "-m" not in calls[0][0][0]


def test_windows_stop_persists_marker_before_signaling(tmp_path, monkeypatch):
    from hdmi_follow import windows_stop

    path = tmp_path / "profile" / "config.json"
    calls = []

    def signal(stop_file):
        assert stop_file == path.with_name("stop")
        assert stop_file.exists()
        calls.append(stop_file)
        return True

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(windows_stop, "signal_stop", signal)
    cli.execute(cli.parser().parse_args(["stop", "--config", str(path)]))
    assert len(calls) == 1
