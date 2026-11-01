"""Install only this user's HDMI Follow login entry."""

from __future__ import annotations

import getpass
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET

TASK_NAME = "HDMIFollow"
LABEL = "org.loftspace.hdmi-follow"
MARKER = "HDMI Follow managed login entry (org.loftspace.hdmi-follow)"
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _command(config_path: Path) -> list[str]:
    executable = Path(sys.executable)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        windowless = executable.with_name("hdmi-follow-background.exe")
        if not windowless.is_file():
            raise RuntimeError(
                "The bundled background executable is missing; extract the entire release ZIP."
            )
        executable = windowless
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            executable = windowless
    command = [str(executable)]
    if not getattr(sys, "frozen", False):
        command.extend(["-m", "hdmi_follow"])
    return [*command, "run", "--config", str(config_path.resolve())]


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=check)


def _windows_task() -> str | None:
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], check=False)
    if result.returncode:
        # Do not interpret access failures as permission to replace a task.
        listing = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        if TASK_NAME.casefold() in listing.stdout.casefold():
            raise RuntimeError("Cannot inspect the existing HDMI Follow login task.")
        return None
    root = ET.fromstring(result.stdout)
    description = root.find(f"{{{TASK_NS}}}RegistrationInfo/{{{TASK_NS}}}Description")
    if description is None or description.text != MARKER:
        raise RuntimeError("An unrelated task named HDMIFollow exists; it was left unchanged.")
    return result.stdout


def _windows_xml(command: list[str]) -> bytes:
    ET.register_namespace("", TASK_NS)
    root = ET.Element(f"{{{TASK_NS}}}Task", {"version": "1.2"})

    def child(parent: ET.Element, tag: str, value: str | None = None) -> ET.Element:
        node = ET.SubElement(parent, f"{{{TASK_NS}}}{tag}")
        node.text = value
        return node

    registration = child(root, "RegistrationInfo")
    child(registration, "Description", MARKER)
    user = getpass.getuser()
    if os.environ.get("USERDOMAIN"):
        user = f"{os.environ['USERDOMAIN']}\\{user}"
    trigger = child(child(root, "Triggers"), "LogonTrigger")
    child(trigger, "Enabled", "true")
    child(trigger, "UserId", user)
    principal = child(child(root, "Principals"), "Principal")
    principal.set("id", "CurrentUser")
    child(principal, "UserId", user)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(root, "Settings")
    for tag, value in {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "StartWhenAvailable": "true",
        "ExecutionTimeLimit": "PT0S",
    }.items():
        child(settings, tag, value)
    restart = child(settings, "RestartOnFailure")
    child(restart, "Interval", "PT1M")
    child(restart, "Count", "3")
    actions = child(root, "Actions")
    actions.set("Context", "CurrentUser")
    action = child(actions, "Exec")
    child(action, "Command", command[0])
    child(action, "Arguments", subprocess.list2cmdline(command[1:]))
    child(action, "WorkingDirectory", str(Path(command[0]).parent))
    return cast(bytes, ET.tostring(root, encoding="utf-16", xml_declaration=True))


def _launchagent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _check_launchagent(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("rb") as source:
        data = plistlib.load(source)
    owner = data.get("EnvironmentVariables", {}).get("HDMI_FOLLOW_AUTOSTART_OWNER")
    if data.get("Label") != LABEL or owner != MARKER:
        raise RuntimeError("An unrelated LaunchAgent exists at the HDMI Follow path.")
    return True


def _mac_domain() -> str:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise RuntimeError("A macOS user session is required to install a LaunchAgent.")
    return f"gui/{getuid()}"


def install(config_path: Path) -> str:
    """Install or update an owned login entry without changing other startup items."""
    if not config_path.is_file():
        raise ValueError(f"Configuration does not exist: {config_path}")
    command = _command(config_path)
    if sys.platform == "win32":
        exists = _windows_task() is not None
        with tempfile.TemporaryDirectory(prefix="hdmi-follow-") as temporary:
            xml_path = Path(temporary) / "task.xml"
            xml_path.write_bytes(_windows_xml(command))
            args = ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path)]
            if exists:
                args.append("/F")
            _run(args)
        return "Windows login task installed. Run the helper to start it now."
    if sys.platform == "darwin":
        path = _launchagent_path()
        exists = _check_launchagent(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "Label": LABEL,
            "EnvironmentVariables": {"HDMI_FOLLOW_AUTOSTART_OWNER": MARKER},
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(config_path.resolve().parent / "autostart.log"),
            "StandardErrorPath": str(config_path.resolve().parent / "autostart.log"),
        }
        if exists:
            _run(["launchctl", "bootout", f"{_mac_domain()}/{LABEL}"], check=False)
        with path.open("wb") as output:
            plistlib.dump(data, output)
        path.chmod(0o600)
        _run(["launchctl", "bootstrap", _mac_domain(), str(path)])
        return "macOS login LaunchAgent installed and started."
    raise RuntimeError("Login installation supports Windows and macOS only.")


def uninstall() -> str:
    """Remove only a login entry identified as ours, retaining configuration."""
    if sys.platform == "win32":
        if _windows_task() is None:
            return "Windows login task is not installed."
        _run(["schtasks", "/End", "/TN", TASK_NAME], check=False)
        _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        return "Windows login task removed. Configuration was kept."
    if sys.platform == "darwin":
        path = _launchagent_path()
        if not _check_launchagent(path):
            return "macOS login LaunchAgent is not installed."
        _run(["launchctl", "bootout", f"{_mac_domain()}/{LABEL}"], check=False)
        path.unlink()
        return "macOS login LaunchAgent removed. Configuration was kept."
    raise RuntimeError("Login installation supports Windows and macOS only.")


def status() -> str:
    """Describe registration, not whether a running helper is healthy."""
    if sys.platform == "win32":
        return "installed" if _windows_task() is not None else "not installed"
    if sys.platform == "darwin":
        return "installed" if _check_launchagent(_launchagent_path()) else "not installed"
    return "unsupported platform"
