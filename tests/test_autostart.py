import plistlib
import subprocess
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest

from hdmi_follow import autostart


def test_windows_xml_keeps_executable_and_quoted_config_separate(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "MY-PC")
    monkeypatch.setattr(autostart.getpass, "getuser", lambda: "Test User")
    command = [
        r"C:\Program Files\Python\pythonw.exe",
        "-m",
        "hdmi_follow",
        "run",
        "--config",
        r"C:\Users\Test User\A&B\config.json",
    ]
    xml = ET.fromstring(autostart._windows_xml(command))
    namespace = {"t": autostart.TASK_NS}
    assert xml.findtext("t:Actions/t:Exec/t:Command", namespaces=namespace) == command[0]
    assert xml.findtext("t:Actions/t:Exec/t:Arguments", namespaces=namespace) == (
        '-m hdmi_follow run --config "C:\\Users\\Test User\\A&B\\config.json"'
    )
    assert (
        xml.findtext("t:Principals/t:Principal/t:LogonType", namespaces=namespace)
        == "InteractiveToken"
    )
    assert (
        xml.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=namespace)
        == "LeastPrivilege"
    )
    assert (
        xml.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=namespace)
        == "MY-PC\\Test User"
    )


def test_refuse_unrelated_windows_task(monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    xml = (
        f'<Task xmlns="{autostart.TASK_NS}"><RegistrationInfo>'
        "<Description>Other software</Description></RegistrationInfo></Task>"
    )
    run = Mock(return_value=subprocess.CompletedProcess([], 0, xml, ""))
    monkeypatch.setattr(autostart, "_run", run)
    with pytest.raises(RuntimeError, match="unrelated task"):
        autostart.uninstall()
    assert run.call_count == 1


def test_windows_install_without_admin_and_without_force_on_new_task(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_windows_task", lambda: None)
    config = tmp_path / "configuration with spaces.json"
    config.write_text("{}")
    captured = []

    def run(command, **kwargs):
        captured.append(command)
        xml_path = Path(command[command.index("/XML") + 1])
        xml = ET.fromstring(xml_path.read_bytes())
        arguments = xml.findtext(
            f"{{{autostart.TASK_NS}}}Actions/{{{autostart.TASK_NS}}}Exec/{{{autostart.TASK_NS}}}Arguments"
        )
        assert arguments is not None
        assert str(config.resolve()) in arguments
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(autostart, "_run", run)
    autostart.install(config)
    assert captured[0][:4] == ["schtasks", "/Create", "/TN", autostart.TASK_NAME]
    assert "/F" not in captured[0]
    assert "/RP" not in captured[0]


def test_missing_windows_task_does_not_delete_anything(monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_windows_task", lambda: None)
    run = Mock()
    monkeypatch.setattr(autostart, "_run", run)
    assert "not installed" in autostart.uninstall()
    run.assert_not_called()


def test_mac_launchagent_preserves_paths_and_uses_argument_array(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_mac_domain", lambda: "gui/501")
    agent = tmp_path / "LaunchAgents" / f"{autostart.LABEL}.plist"
    monkeypatch.setattr(autostart, "_launchagent_path", lambda: agent)
    config = tmp_path / "Folder & spaces" / "config.json"
    config.parent.mkdir()
    config.write_text("{}")
    run = Mock(
        side_effect=lambda command, check=True: subprocess.CompletedProcess(
            command, 1 if command[1] == "print" else 0, "", ""
        )
    )
    monkeypatch.setattr(autostart, "_run", run)
    autostart.install(config)
    with agent.open("rb") as source:
        data = plistlib.load(source)
    assert data["ProgramArguments"][-1] == str(config.resolve())
    assert data["ProgramArguments"][-3:] == ["run", "--config", str(config.resolve())]
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"]["HDMI_FOLLOW_AUTOSTART_OWNER"] == autostart.MARKER
    assert run.call_args_list[-1].args[0] == ["launchctl", "bootstrap", "gui/501", str(agent)]
    assert [call.args[0][1] for call in run.call_args_list] == ["print", "bootstrap"]


def test_mac_refuses_to_overwrite_unrelated_launchagent(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    agent = tmp_path / "other.plist"
    with agent.open("wb") as output:
        plistlib.dump({"Label": autostart.LABEL, "ProgramArguments": ["other"]}, output)
    monkeypatch.setattr(autostart, "_launchagent_path", lambda: agent)
    config = tmp_path / "config.json"
    config.write_text("{}")
    with pytest.raises(RuntimeError, match="unrelated LaunchAgent"):
        autostart.install(config)
    assert plistlib.loads(agent.read_bytes())["ProgramArguments"] == ["other"]


def test_mac_uninstall_keeps_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_mac_domain", lambda: "gui/501")
    agent = tmp_path / "managed.plist"
    agent.write_bytes(
        plistlib.dumps(
            {
                "Label": autostart.LABEL,
                "EnvironmentVariables": {"HDMI_FOLLOW_AUTOSTART_OWNER": autostart.MARKER},
            }
        )
    )
    monkeypatch.setattr(autostart, "_launchagent_path", lambda: agent)
    run = Mock()
    monkeypatch.setattr(autostart, "_run", run)
    autostart.uninstall()
    assert not agent.exists()
    run.assert_called_once_with(
        ["launchctl", "bootout", "gui/501/org.loftspace.hdmi-follow"], check=False
    )


def test_frozen_windows_uses_background_binary(monkeypatch, tmp_path):
    executable = tmp_path / "hdmi-follow.exe"
    background = tmp_path / "hdmi-follow-background.exe"
    background.touch()
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(autostart.sys, "executable", str(executable))
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
    command = autostart._command(tmp_path / "config.json")
    assert command == [str(background), "run", "--config", str(tmp_path / "config.json")]
    background.unlink()
    with pytest.raises(RuntimeError, match="background executable is missing"):
        autostart._command(tmp_path / "config.json")


def test_failed_task_inspection_does_not_allow_replacement(monkeypatch):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", "Access denied"),
            subprocess.CompletedProcess([], 0, '"\\HDMIFollow","N/A","Ready"', ""),
        ]
    )
    monkeypatch.setattr(autostart, "_run", run)
    with pytest.raises(RuntimeError, match="Cannot inspect"):
        autostart._windows_task()


def _managed_launchagent(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": autostart.LABEL,
                "EnvironmentVariables": {"HDMI_FOLLOW_AUTOSTART_OWNER": autostart.MARKER},
                "ProgramArguments": ["old"],
            }
        )
    )


class _Launchctl:
    """Fake launchctl: the job stays loaded for a few polls after bootout."""

    def __init__(self, unload_after, bootstrap_codes):
        self.unload_after = unload_after
        self.bootstrap_codes = list(bootstrap_codes)
        self.calls = []

    def __call__(self, command, check=True):
        assert check is False
        verb = command[1]
        self.calls.append(verb)
        if verb == "print":
            loaded = self.calls.count("print") <= self.unload_after
            return subprocess.CompletedProcess(command, 0 if loaded else 113, "", "")
        if verb == "bootstrap":
            code = self.bootstrap_codes.pop(0)
            stderr = "Bootstrap failed: 5: Input/output error" if code else ""
            return subprocess.CompletedProcess(command, code, "", stderr)
        return subprocess.CompletedProcess(command, 0, "", "")


def _mac_setup(monkeypatch, tmp_path, launchctl):
    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_mac_domain", lambda: "gui/501")
    monkeypatch.setattr(autostart.time, "sleep", lambda seconds: None)
    agent = tmp_path / "LaunchAgents" / f"{autostart.LABEL}.plist"
    monkeypatch.setattr(autostart, "_launchagent_path", lambda: agent)
    monkeypatch.setattr(autostart, "_run", launchctl)
    config = tmp_path / "config.json"
    config.write_text("{}")
    return agent, config


def test_mac_reinstall_waits_for_unload_and_retries_bootstrap(monkeypatch, tmp_path):
    launchctl = _Launchctl(unload_after=2, bootstrap_codes=[5, 0])
    agent, config = _mac_setup(monkeypatch, tmp_path, launchctl)
    _managed_launchagent(agent)
    assert "started" in autostart.install(config)
    assert launchctl.calls == ["bootout", "print", "print", "print", "bootstrap", "bootstrap"]
    assert plistlib.loads(agent.read_bytes())["ProgramArguments"][-2:] == [
        "--config",
        str(config.resolve()),
    ]


def test_mac_install_unloads_job_whose_plist_is_missing(monkeypatch, tmp_path):
    launchctl = _Launchctl(unload_after=1, bootstrap_codes=[0])
    _agent, config = _mac_setup(monkeypatch, tmp_path, launchctl)
    autostart.install(config)
    assert launchctl.calls == ["print", "bootout", "print", "bootstrap"]


def test_mac_reinstall_keeps_plist_when_old_job_does_not_stop(monkeypatch, tmp_path):
    launchctl = _Launchctl(unload_after=10_000, bootstrap_codes=[])
    agent, config = _mac_setup(monkeypatch, tmp_path, launchctl)
    _managed_launchagent(agent)
    with pytest.raises(RuntimeError, match="did not stop"):
        autostart.install(config)
    assert launchctl.calls.count("print") == autostart.UNLOAD_POLLS
    assert "bootstrap" not in launchctl.calls
    assert plistlib.loads(agent.read_bytes())["ProgramArguments"] == ["old"]


def test_mac_failed_bootstrap_reports_clear_error(monkeypatch, tmp_path):
    launchctl = _Launchctl(unload_after=0, bootstrap_codes=[5] * autostart.BOOTSTRAP_ATTEMPTS)
    agent, config = _mac_setup(monkeypatch, tmp_path, launchctl)
    _managed_launchagent(agent)
    with pytest.raises(RuntimeError, match="Input/output error.*nothing is loaded"):
        autostart.install(config)
    assert launchctl.calls.count("bootstrap") == autostart.BOOTSTRAP_ATTEMPTS
