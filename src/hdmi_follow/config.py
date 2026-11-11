"""Validated local configuration and pairing storage."""

import ipaddress
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    if sys.platform == "win32":
        # Documents is stable inside and outside packaged launchers such as Codex.
        # Windows can virtualize LOCALAPPDATA per application, splitting profiles.
        return Path.home() / "Documents" / "HDMI Follow"
    return Path.home() / "Library/Application Support/HDMIFollow"


def default_config() -> Path:
    return data_dir() / "config.json"


@dataclass(frozen=True)
class Device:
    vendor_id: int
    product_id: int
    serial: str = ""

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or not 1 <= v <= 65535 for v in (self.vendor_id, self.product_id)
        ):
            raise ValueError("USB IDs must be integers between 1 and 65535")
        if not isinstance(self.serial, str):
            raise ValueError("USB serial must be a string")


@dataclass(frozen=True)
class Config:
    tv_ip: str
    own_input: str
    devices: tuple[Device, ...]
    away_input: str | None = None
    debounce: float = 0.2
    poll_interval: float = 0.1
    switch_hub: str | None = None

    def __post_init__(self) -> None:
        ip = ipaddress.ip_address(self.tv_ip)
        if ip.version != 4 or not ip.is_private or ip.is_unspecified or ip.is_loopback:
            raise ValueError("TV address must be a private LAN IPv4 address")
        inputs = {f"HDMI_{i}" for i in range(1, 5)}
        if self.own_input not in inputs or (
            self.away_input is not None and self.away_input not in inputs
        ):
            raise ValueError("Inputs must be HDMI_1 through HDMI_4")
        if self.own_input == self.away_input:
            raise ValueError("Own and away inputs must differ")
        if self.switch_hub is not None and (
            not isinstance(self.switch_hub, str)
            or not re.fullmatch(
                r"USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}(?:[^\\]*\\[^\r\n\x00]+)?",
                self.switch_hub,
                re.IGNORECASE,
            )
        ):
            raise ValueError("Switch hub must be a USB VID/PID or full device instance ID")
        if not self.devices and not self.switch_hub:
            raise ValueError("Select a switch hub or at least one USB device")
        if self.devices and self.switch_hub:
            raise ValueError("Choose switch-hub mode or keyboard mode, not both")
        if not 0.05 <= self.debounce <= 10 or not 0.02 <= self.poll_interval <= 2:
            raise ValueError("Debounce must be 0.05..10s and polling must be 0.02..2s")

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{**raw, "devices": tuple(Device(**d) for d in raw.get("devices", []))})

    def save(self, path: Path) -> None:
        write_private_json(path, asdict(self))


def write_private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        json.dump(data, stream, indent=2)
        stream.write("\n")
    temp.replace(path)


def key_path(config_path: Path) -> Path:
    return config_path.with_name("credentials.json")


def load_key(path: Path, tv_ip: str) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("tv_ip") != tv_ip:
        return None
    value = data.get("client_key")
    return value if isinstance(value, str) and value else None
