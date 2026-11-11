import json

import pytest

from hdmi_follow import config as config_module
from hdmi_follow.config import Config, Device, load_key, save_key, write_private_json


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


def test_key_is_bound_to_tv_address(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
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


def _fake_dpapi(monkeypatch, *, fail=False):
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setattr(config_module.windows_dpapi, "protect", lambda data: b"sealed:" + data)

    def unprotect(blob):
        if fail or not blob.startswith(b"sealed:"):
            raise OSError(13, "CryptUnprotectData failed")
        return blob.removeprefix(b"sealed:")

    monkeypatch.setattr(config_module.windows_dpapi, "unprotect", unprotect)


def test_windows_key_round_trip_is_protected(tmp_path, monkeypatch):
    _fake_dpapi(monkeypatch)
    path = tmp_path / "credentials.json"
    save_key(path, "192.168.0.20", "secret")
    stored = json.loads(path.read_text())
    assert set(stored) == {"tv_ip", "client_key_dpapi"}
    assert "secret" not in path.read_text()
    assert load_key(path, "192.168.0.20") == "secret"
    assert load_key(path, "192.168.0.21") is None


def test_windows_plaintext_key_migrates_on_load(tmp_path, monkeypatch):
    _fake_dpapi(monkeypatch)
    path = tmp_path / "credentials.json"
    write_private_json(path, {"tv_ip": "192.168.0.20", "client_key": "secret"})
    assert load_key(path, "192.168.0.20") == "secret"
    assert set(json.loads(path.read_text())) == {"tv_ip", "client_key_dpapi"}
    assert "secret" not in path.read_text()
    assert load_key(path, "192.168.0.20") == "secret"


def test_windows_failed_migration_keeps_working_plaintext_key(tmp_path, monkeypatch):
    _fake_dpapi(monkeypatch)

    def refuse(data):
        raise OSError(5, "CryptProtectData failed")

    monkeypatch.setattr(config_module.windows_dpapi, "protect", refuse)
    path = tmp_path / "credentials.json"
    write_private_json(path, {"tv_ip": "192.168.0.20", "client_key": "secret"})
    assert load_key(path, "192.168.0.20") == "secret"
    assert json.loads(path.read_text()) == {"tv_ip": "192.168.0.20", "client_key": "secret"}


def test_windows_undecryptable_key_counts_as_missing(tmp_path, monkeypatch, capsys):
    _fake_dpapi(monkeypatch)
    path = tmp_path / "credentials.json"
    save_key(path, "192.168.0.20", "secret")
    _fake_dpapi(monkeypatch, fail=True)
    assert load_key(path, "192.168.0.20") is None
    error = capsys.readouterr().err
    assert "Run pair again" in error
    assert "secret" not in error


@pytest.mark.parametrize("blob", ["not base64!", 7, ""])
def test_windows_malformed_protected_key_counts_as_missing(tmp_path, monkeypatch, blob):
    _fake_dpapi(monkeypatch)
    path = tmp_path / "credentials.json"
    write_private_json(path, {"tv_ip": "192.168.0.20", "client_key_dpapi": blob})
    assert load_key(path, "192.168.0.20") is None


def test_non_windows_keeps_plaintext_format(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    path = tmp_path / "credentials.json"
    save_key(path, "192.168.0.20", "secret")
    assert json.loads(path.read_text()) == {"tv_ip": "192.168.0.20", "client_key": "secret"}


@pytest.mark.skipif(config_module.sys.platform != "win32", reason="real DPAPI needs Windows")
def test_real_dpapi_round_trip():
    from hdmi_follow import windows_dpapi

    blob = windows_dpapi.protect(b"secret-key")
    assert b"secret-key" not in blob
    assert windows_dpapi.unprotect(blob) == b"secret-key"
    with pytest.raises(OSError):
        windows_dpapi.unprotect(blob[:-4] + b"xxxx")
