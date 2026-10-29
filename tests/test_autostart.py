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
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(autostart, "_run", run)
    autostart.install(config)
    with agent.open("rb") as source:
        data = plistlib.load(source)
    assert data["ProgramArguments"][-1] == str(config.resolve())
    assert data["ProgramArguments"][-3:] == ["run", "--config", str(config.resolve())]
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"]["HDMI_FOLLOW_AUTOSTART_OWNER"] == autostart.MARKER
    run.assert_called_once_with(["launchctl", "bootstrap", "gui/501", str(agent)])


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
