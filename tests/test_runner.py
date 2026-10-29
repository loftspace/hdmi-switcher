import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hdmi_follow import runner, tv
from hdmi_follow.config import Config, Device
from hdmi_follow.devices import SeenDevice
from hdmi_follow.ownership import Transition


@pytest.fixture
def config() -> Config:
    return Config("192.168.0.20", "HDMI_1", (Device(1, 2),), away_input="HDMI_3")


async def test_arrival_and_loss_select_correct_target(config, monkeypatch) -> None:
    switch = AsyncMock()
    monkeypatch.setattr(tv, "switch", switch)

    def current():
        return True

    await runner.follow_transition(config, "private-key", Transition(True, 1), current)
    await runner.follow_transition(config, "private-key", Transition(False, 2), current)
    assert [call.args[2] for call in switch.await_args_list] == ["HDMI_1", "HDMI_3"]


async def test_arrival_only_mode_does_nothing_on_loss(monkeypatch) -> None:
    config = Config("192.168.0.20", "HDMI_1", (Device(1, 2),))
    switch = AsyncMock()
    monkeypatch.setattr(tv, "switch", switch)
    await runner.follow_transition(config, "private-key", Transition(False, 1), lambda: True)
    switch.assert_not_awaited()


async def test_stale_transition_is_never_sent(config, monkeypatch) -> None:
    switch = AsyncMock()
    monkeypatch.setattr(tv, "switch", switch)
    await runner.follow_transition(config, "private-key", Transition(True, 1), lambda: False)
    switch.assert_not_awaited()


async def test_network_failure_has_exactly_two_attempts(config, monkeypatch, caplog) -> None:
    switch = AsyncMock(side_effect=TimeoutError("private-key"))
    monkeypatch.setattr(tv, "switch", switch)
    await runner.follow_transition(config, "private-key", Transition(True, 1), lambda: True)
    assert switch.await_count == 2
    assert "private-key" not in caplog.text


async def test_pairing_failure_is_not_retried(config, monkeypatch) -> None:
    switch = AsyncMock(side_effect=tv.PairingRequired())
    monkeypatch.setattr(tv, "switch", switch)
    await runner.follow_transition(config, "private-key", Transition(True, 1), lambda: True)
    assert switch.await_count == 1


async def test_cancel_during_retry_wait_never_sends_late_command(config, monkeypatch) -> None:
    first_attempt = asyncio.Event()

    async def unavailable(*args):
        first_attempt.set()
        raise OSError("offline")

    switch = AsyncMock(side_effect=unavailable)
    monkeypatch.setattr(tv, "switch", switch)
    task = asyncio.create_task(
        runner.follow_transition(config, "private-key", Transition(True, 1), lambda: True)
    )
    await first_attempt.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert switch.await_count == 1


async def test_ownership_lost_during_retry_wait_prevents_second_attempt(
    config, monkeypatch
) -> None:
    current = True

    async def unavailable(*args):
        nonlocal current
        current = False
        raise OSError("offline")

    switch = AsyncMock(side_effect=unavailable)
    monkeypatch.setattr(tv, "switch", switch)
    await runner.follow_transition(config, "private-key", Transition(True, 1), lambda: current)
    assert switch.await_count == 1


async def test_watcher_cancels_active_command_on_first_loss_sample(monkeypatch) -> None:
    config = Config("192.168.0.20", "HDMI_1", (Device(1, 2),), debounce=0.3, poll_interval=0.1)
    clock = 0.0
    cancelled = False
    original_sleep = asyncio.sleep

    def enumerate_fake():
        step = round(clock / 0.1)
        if step >= 17:
            raise asyncio.CancelledError from None
        present = 5 <= step < 11
        return {SeenDevice(1, 2, "", "Keyboard")} if present else set()

    async def tick(_delay):
        nonlocal clock
        clock += 0.1
        await original_sleep(0)

    async def blocked_switch(*args):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    switch = AsyncMock(side_effect=blocked_switch)
    monkeypatch.setattr(runner, "enumerate_devices", enumerate_fake)
    monkeypatch.setattr(
        runner, "time", SimpleNamespace(time=lambda: clock, perf_counter=lambda: clock)
    )
    monkeypatch.setattr(
        runner,
        "asyncio",
        SimpleNamespace(sleep=tick, create_task=asyncio.create_task, gather=asyncio.gather),
    )
    monkeypatch.setattr(tv, "switch", switch)
    with pytest.raises(asyncio.CancelledError):
        await runner.run(config, "key")
    assert switch.await_count == 1
    assert cancelled


async def test_stop_file_cancels_active_switch_before_run_returns(monkeypatch, tmp_path) -> None:
    config = Config("192.168.0.20", "HDMI_1", (Device(1, 2),), debounce=0.3, poll_interval=0.1)
    stop_file = tmp_path / "stop"
    started = asyncio.Event()
    cancelled = False
    clock = 0.0
    original_sleep = asyncio.sleep

    def enumerate_fake():
        return {SeenDevice(1, 2, "", "Keyboard")} if clock >= 0.5 else set()

    async def tick(_delay):
        nonlocal clock
        clock += 0.1
        if started.is_set():
            stop_file.touch()
        # Bound this test independently of the implementation's termination behavior.
        if clock > 3:
            pytest.fail("run ignored its stop file")
        await original_sleep(0)

    async def blocked_switch(*args):
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    switch = AsyncMock(side_effect=blocked_switch)
    monkeypatch.setattr(runner, "enumerate_devices", enumerate_fake)
    monkeypatch.setattr(
        runner, "time", SimpleNamespace(time=lambda: clock, perf_counter=lambda: clock)
    )
    monkeypatch.setattr(
        runner,
        "asyncio",
        SimpleNamespace(sleep=tick, create_task=asyncio.create_task, gather=asyncio.gather),
    )
    monkeypatch.setattr(tv, "switch", switch)
    await runner.run(config, "key", stop_file=stop_file)
    assert stop_file.exists()
    assert switch.await_count == 1
    assert cancelled


async def test_existing_stop_file_does_not_enumerate_or_connect(monkeypatch, tmp_path) -> None:
    config = Config("192.168.0.20", "HDMI_1", (Device(1, 2),))
    stop_file = tmp_path / "stop"
    stop_file.touch()
    monkeypatch.setattr(runner, "enumerate_devices", lambda: pytest.fail("Must stop first"))
    switch = AsyncMock()
    monkeypatch.setattr(tv, "switch", switch)
    await runner.run(config, "key", stop_file=stop_file)
    switch.assert_not_awaited()
