import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hdmi_follow import cli
from hdmi_follow.config import Config, Device, key_path, load_key, write_private_json


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


def test_frozen_background_start_delegates_process_lifetime(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    Config("192.168.0.20", "HDMI_1", (Device(1, 2),)).save(path)
    write_private_json(key_path(path), {"tv_ip": "192.168.0.20", "client_key": "key"})
    calls = []
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)

    def spawn(*args):
        calls.append(args)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(cli.launch, "spawn_background", spawn)
    cli.execute(cli.parser().parse_args(["start", "--config", str(path)]))
    assert "-m" not in calls[0][0]
    assert calls[0][0][-2:] == ["--config", str(path)]


def test_windows_stop_persists_marker_before_signaling(tmp_path, monkeypatch):
    from hdmi_follow import windows_stop

    path = tmp_path / "profile" / "config.json"
    calls = []

    def signal(stop_file):
        assert stop_file == path.with_name("stop")
        assert stop_file.exists()
        request = json.loads(stop_file.read_text(encoding="utf-8"))
        assert request["requester_pid"] == cli.os.getpid()
        assert request["reason"] == "cli_stop"
        calls.append(stop_file)
        return True

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(windows_stop, "signal_stop", signal)
    cli.execute(cli.parser().parse_args(["stop", "--config", str(path)]))
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_run_logs_startup_version_lifecycle_and_bounded_rotation(tmp_path, monkeypatch, failure):
    import logging

    path = tmp_path / "config.json"
    Config("192.168.0.20", "HDMI_1", (Device(1, 2),)).save(path)
    write_private_json(key_path(path), {"tv_ip": "192.168.0.20", "client_key": "secret-key"})
    monkeypatch.setattr(
        cli, "run", AsyncMock(side_effect=RuntimeError("secret-frame") if failure else None)
    )
    monkeypatch.setattr(cli.diagnostics, "enable_fatal_traces", lambda path: None)
    monkeypatch.setattr(cli.diagnostics, "health_summary", lambda: "memory=test")
    logger = logging.getLogger("hdmi_follow")
    before = list(logger.handlers)
    old_level = logger.level
    try:
        if failure:
            with pytest.raises(RuntimeError):
                cli.execute(cli.parser().parse_args(["run", "--config", str(path)]))
        else:
            cli.execute(cli.parser().parse_args(["run", "--config", str(path)]))
        handler = next(handler for handler in logger.handlers if handler not in before)
        assert handler.maxBytes == 2_000_000
        assert handler.backupCount == 4
        content = (tmp_path / "follow.log").read_text(encoding="utf-8")
        assert f"Helper starting version={cli.__version__}" in content
        assert "pid=" in content
        assert "memory=test" in content
        assert "Helper stopped" in content
        assert "secret-key" not in content
        assert "secret-frame" not in content
        record = json.loads(path.with_name("run-state.json").read_text(encoding="utf-8"))
        assert record["status"] == "clean"
        assert record["exit_reason"] == ("error" if failure else "normal")
        assert cli.lifecycle._current is None
    finally:
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(old_level)


def test_uninstall_waits_for_helper_exit_before_removing_autostart(tmp_path, monkeypatch):
    import threading

    from hdmi_follow import windows_stop
    from hdmi_follow.instance import instance_lock

    path = tmp_path / "config.json"
    monkeypatch.setattr(windows_stop, "signal_stop", lambda stop_file: True)
    holding = threading.Event()
    released = threading.Event()

    def helper():
        with instance_lock(path.with_name("run.lock")):
            holding.set()
            while not path.with_name("stop").exists():
                cli.time.sleep(0.01)
            cli.time.sleep(0.2)
        released.set()

    thread = threading.Thread(target=helper)
    thread.start()
    assert holding.wait(5)
    seen = []
    monkeypatch.setattr(cli.autostart, "uninstall", lambda: seen.append(released.is_set()) or "ok")
    cli.execute(cli.parser().parse_args(["uninstall", "--config", str(path)]))
    thread.join(5)
    assert seen == [True]


def test_uninstall_wait_is_bounded(tmp_path, monkeypatch):
    from hdmi_follow.instance import instance_lock

    path = tmp_path / "config.json"
    with instance_lock(path.with_name("run.lock")):
        start = cli.time.monotonic()
        assert cli._wait_for_exit(path, timeout=0.3) is False
        assert cli.time.monotonic() - start < 2
    assert cli._wait_for_exit(path, timeout=0.3) is True


@pytest.mark.parametrize("content", ['{"tv_ip": "192.168', "[]"])
def test_pair_repairs_corrupt_credentials(tmp_path, monkeypatch, content):
    path = tmp_path / "config.json"
    Config("192.168.0.20", "HDMI_1", (Device(1, 2),)).save(path)
    key_path(path).write_text(content, encoding="utf-8")
    pair = AsyncMock(return_value="fresh")
    monkeypatch.setattr(cli.tv, "pair", pair)
    cli.execute(cli.parser().parse_args(["pair", "--config", str(path)]))
    pair.assert_awaited_once_with("192.168.0.20", None)
    assert load_key(key_path(path), "192.168.0.20") == "fresh"


def test_duplicate_run_exits_successfully(tmp_path, monkeypatch, capsys):
    from hdmi_follow.instance import instance_lock

    path = tmp_path / "config.json"
    Config("192.168.0.20", "HDMI_1", (Device(1, 2),)).save(path)
    write_private_json(key_path(path), {"tv_ip": "192.168.0.20", "client_key": "key"})
    run = AsyncMock()
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(cli.sys, "argv", ["hdmi-follow", "run", "--config", str(path)])
    with instance_lock(path.with_name("run.lock")):
        cli.main()  # Returns normally: exit code 0, so launchd does not relaunch it.
    run.assert_not_called()
    assert "already running" in capsys.readouterr().err
