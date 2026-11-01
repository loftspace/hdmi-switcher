from unittest.mock import Mock

import pytest

from hdmi_follow import launch


@pytest.mark.parametrize("windows", [True, False])
def test_spawn_options(monkeypatch, windows):
    monkeypatch.setattr(launch.os, "name", "nt" if windows else "posix")
    membership = Mock(return_value=False)
    monkeypatch.setattr(launch, "_in_job", membership)
    child = Mock(pid=42, _handle=123)
    popen = Mock(return_value=child)
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    output = Mock()
    environment = {"TEMP": "private", "PYINSTALLER_RESET_ENVIRONMENT": "0"}
    assert launch.spawn_background(["app", "run", "space path"], output, environment) is child
    options = popen.call_args.kwargs
    assert options["creationflags"] == (0x09000000 if windows else 0)
    assert options["start_new_session"] is (not windows)
    assert options["stdin"] == launch.subprocess.DEVNULL
    assert options["stdout"] is options["stderr"] is output
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "0"
    assert popen.call_args.args == (["app", "run", "space path"],)


def test_denied_breakaway_never_retries(monkeypatch):
    monkeypatch.setattr(launch.os, "name", "nt")
    monkeypatch.setattr(launch, "_in_job", Mock(return_value=True))
    error = OSError("sensitive body")
    error.winerror = 5
    popen = Mock(side_effect=error)
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    with pytest.raises(launch.BackgroundLaunchError, match="Task Scheduler"):
        launch.spawn_background(["app"], Mock(), {})
    assert popen.call_count == 1


def test_nested_job_warning(monkeypatch, caplog):
    monkeypatch.setattr(launch.os, "name", "nt")
    monkeypatch.setattr(launch, "_in_job", Mock(return_value=True))
    monkeypatch.setattr(launch.subprocess, "Popen", Mock(return_value=Mock(pid=42, _handle=123)))
    launch.spawn_background(["app"], Mock(), {})
    assert "remains in a Windows job" in caplog.text


def test_unrelated_oserror_propagates(monkeypatch):
    monkeypatch.setattr(launch.os, "name", "posix")
    error = FileNotFoundError("missing")
    monkeypatch.setattr(launch.subprocess, "Popen", Mock(side_effect=error))
    with pytest.raises(FileNotFoundError):
        launch.spawn_background(["app"], Mock(), {})
