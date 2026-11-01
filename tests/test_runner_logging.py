import logging
from unittest.mock import AsyncMock

from hdmi_follow import runner, tv
from hdmi_follow.config import Config, Device
from hdmi_follow.ownership import Transition


async def test_success_and_guard_skip_can_be_correlated_without_credentials(monkeypatch, caplog):
    config = Config("192.168.0.20", "HDMI_1", (Device(1, 2),), away_input="HDMI_2")
    monkeypatch.setattr(tv, "switch", AsyncMock())
    caplog.set_level(logging.INFO, logger="hdmi_follow")
    await runner.follow_transition(config, "private-client-key", Transition(True, 71), lambda: True)
    await runner.follow_transition(
        config, "private-client-key", Transition(False, 72), lambda: False
    )
    assert "Command starting generation=71 target=HDMI_1 attempt=1" in caplog.text
    assert "Selected HDMI_1" in caplog.text
    assert "generation=71 attempt=1" in caplog.text
    assert "generation=72 target=HDMI_2 reason=ownership_guard" in caplog.text
    assert "private-client-key" not in caplog.text


async def test_pairing_failure_has_specific_safe_reason(monkeypatch, caplog):
    config = Config("192.168.0.20", "HDMI_1", (Device(1, 2),))
    monkeypatch.setattr(tv, "switch", AsyncMock(side_effect=tv.PairingRequired("secret-frame")))
    caplog.set_level(logging.INFO, logger="hdmi_follow")
    await runner.follow_transition(config, "secret-key", Transition(True, 13), lambda: True)
    assert "generation=13 target=HDMI_1 reason=pairing_required" in caplog.text
    assert "secret-frame" not in caplog.text
    assert "secret-key" not in caplog.text
