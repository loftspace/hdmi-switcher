import json

import pytest

from hdmi_follow import config as config_module
from hdmi_follow.config import Config, Device, load_key, write_private_json


def test_config_roundtrip_keeps_peer_input_and_device_identity(tmp_path):
    config = Config("192.168.0.20", "HDMI_1", (Device(0x3434, 0x0E21, "serial"),), "HDMI_3")
    path = tmp_path / "profile" / "config.json"
    config.save(path)
    assert Config.load(path) == config
    assert "client_key" not in path.read_text()


@pytest.mark.parametrize("ip", ["127.0.0.1", "0.0.0.0", "8.8.8.8", "::1", "TV.example"])
def test_non_lan_addresses_rejected(ip):
    with pytest.raises(ValueError):
        Config(ip, "HDMI_1", (Device(1, 2),))


@pytest.mark.parametrize("own,away", [("HDMI_5", None), ("HDMI_1", "HDMI_1"), ("HDMI_1", "TV")])
def test_invalid_input_mapping_rejected(own, away):
    with pytest.raises(ValueError):
        Config("192.168.0.20", own, (Device(1, 2),), away)


def test_key_is_bound_to_tv_address(tmp_path):
    path = tmp_path / "credentials.json"
    write_private_json(path, {"tv_ip": "192.168.0.20", "client_key": "secret"})
    assert load_key(path, "192.168.0.20") == "secret"
    assert load_key(path, "192.168.0.21") is None
    assert json.loads(path.read_text())["client_key"] == "secret"


def test_windows_default_does_not_depend_on_virtualized_localappdata(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "package-cache"))
    assert config_module.data_dir() == tmp_path / "Documents" / "HDMI Follow"


@pytest.mark.parametrize("vendor,product", [(True, 2), (0, 2), (1, 65536), ("12", 2)])
def test_invalid_usb_identifiers_rejected(vendor, product):
    with pytest.raises(ValueError):
        Device(vendor, product)
