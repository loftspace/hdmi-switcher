import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hdmi_follow import event_runner, runner
from hdmi_follow.config import Config
from hdmi_follow.ownership import OwnershipTracker
from hdmi_follow.windows_notifications import Signal

HUB = r"USB\VID_0557&PID_2414\A&2648020B&0&9"


class FakeHub:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.revision = 0
        self.power_revision = 0
        self.suspended = False
        self.closed = False

    async def wait(self):
        return await self.queue.get()

    def device(self):
        self.revision += 1
        self.queue.put_nowait(Signal("device", self.revision))

    def power(self, suspended, deliver=True):
        self.revision += 1
        self.power_revision += 1
        self.suspended = suspended
        if deliver:
            self.queue.put_nowait(Signal("reset", self.revision))

    def close(self):
        self.closed = True


class FakeStop:
    def __init__(self):
        self.event = asyncio.Event()
        self.closed = False

    async def wait(self):
        await self.event.wait()

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, monkeypatch, tmp_path):
        self.hub = FakeHub()
        self.stop = FakeStop()
        self.config = Config(
            "192.168.0.20", "HDMI_1", (), away_input="HDMI_3", debounce=0.05, switch_hub=HUB
        )
        self.path = tmp_path / "stop"
        self.present = True
        self.samples = 0
        self.failed_samples = 0
        self.fail_sample = False
        self.sent = []
        self.tracker = None

        def observe_tracker(*args, **kwargs):
            self.tracker = OwnershipTracker(*args, **kwargs)
            return self.tracker

        monkeypatch.setattr(event_runner, "OwnershipTracker", observe_tracker)
        monkeypatch.setattr(event_runner, "HubNotifications", lambda *args: self.hub)
        monkeypatch.setattr(event_runner, "StopNotification", lambda *args: self.stop)
        monkeypatch.setattr(event_runner, "sample_ownership", self.sample)
        monkeypatch.setattr(event_runner, "follow_transition", self.record)

    def sample(self, config):
        self.samples += 1
        if self.fail_sample:
            self.failed_samples += 1
            raise OSError("inventory unavailable")
        return self.present

    async def record(self, config, key, transition, current, session):
        if current():
            self.sent.append(transition.present)

    async def start(self, session=None):
        self.task = asyncio.create_task(event_runner.run(self.config, "key", self.path, session))
        await self.until(lambda: self.tracker is not None and self.tracker.stable is self.present)

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                if self.task.done():
                    await self.task
                    pytest.fail("Reactor exited before the expected observation")
                await asyncio.sleep(0)

    async def finish(self):
        self.stop.event.set()
        await asyncio.wait_for(self.task, 1)


async def test_idle_establishes_baseline_then_never_rescans(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    await harness.start()
    count = harness.samples
    assert count >= 2
    await asyncio.sleep(0.15)
    assert harness.samples == count
    assert harness.sent == []
    await harness.finish()
    assert harness.hub.closed and harness.stop.closed


async def test_first_edge_after_long_idle_still_switches(monkeypatch, tmp_path):
    monkeypatch.setattr(event_runner, "RECONCILE_INTERVAL", 0.01)
    harness = Harness(monkeypatch, tmp_path)
    offset = 0

    def heartbeat_sample(config):
        nonlocal offset
        # Advance idle wall time through observed heartbeats, not one suspend-like gap.
        if harness.present:
            offset += 1
        return harness.sample(config)

    monkeypatch.setattr(event_runner, "sample_ownership", heartbeat_sample)
    monkeypatch.setattr(
        event_runner,
        "time",
        SimpleNamespace(time=lambda: time.time() + offset, monotonic=time.monotonic),
    )
    await harness.start()
    await harness.until(lambda: harness.samples >= 20)
    assert offset >= 20
    assert harness.sent == []
    harness.present = False
    harness.hub.device()
    await harness.until(lambda: harness.sent == [False])
    assert harness.sent == [False]
    await harness.finish()


async def test_long_sampling_gap_silently_rebaselines_then_next_edge_works(monkeypatch, tmp_path):
    monkeypatch.setattr(event_runner, "RECONCILE_INTERVAL", 0.1)
    harness = Harness(monkeypatch, tmp_path)
    offset = 0
    monkeypatch.setattr(
        event_runner,
        "time",
        SimpleNamespace(time=lambda: time.time() + offset, monotonic=time.monotonic),
    )
    await harness.start()
    offset = 3600  # Suspend or loop starvation with no delivered power callback.
    harness.present = False
    await harness.until(lambda: harness.tracker.stable is False)
    assert harness.sent == []
    assert harness.hub.power_revision == 0
    harness.present = True
    harness.hub.device()
    await harness.until(lambda: harness.sent == [True])
    await harness.finish()


async def test_fast_reversal_inside_debounce_does_not_switch(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    await harness.start()
    frozen_time = time.time()
    monkeypatch.setattr(
        event_runner, "time", SimpleNamespace(time=lambda: frozen_time, monotonic=time.monotonic)
    )
    harness.present = False
    before = harness.samples
    harness.hub.device()
    await harness.until(lambda: harness.samples > before)
    harness.present = True
    before = harness.samples
    harness.hub.device()
    await harness.until(lambda: harness.samples > before)
    assert harness.sent == []
    await harness.finish()


async def test_duplicate_device_event_does_not_cancel_same_state_command(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    cancelled = False

    async def blocked(config, key, transition, current, session):
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
            if current():
                harness.sent.append(transition.present)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            finished.set()

    monkeypatch.setattr(event_runner, "follow_transition", blocked)
    await harness.start()
    harness.present = False
    harness.hub.device()
    await asyncio.wait_for(started.wait(), 1)
    before = harness.samples
    harness.hub.device()
    await harness.until(lambda: harness.samples > before)
    release.set()
    await asyncio.wait_for(finished.wait(), 3)
    assert not cancelled
    assert harness.sent == [False]
    await harness.finish()


async def test_guard_rechecks_actual_hub_even_without_queued_callback(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    checked = asyncio.Event()

    async def blocked(config, key, transition, current, session):
        started.set()
        await release.wait()
        if current():
            harness.sent.append(transition.present)
        checked.set()

    monkeypatch.setattr(event_runner, "follow_transition", blocked)
    await harness.start()
    harness.present = False
    harness.hub.device()
    await asyncio.wait_for(started.wait(), 1)
    harness.present = True  # Native notification has not yet reached the event loop.
    release.set()
    await asyncio.wait_for(checked.wait(), 3)
    assert harness.sent == []
    await harness.finish()


async def test_native_power_epoch_blocks_send_before_reset_is_delivered(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    checked = asyncio.Event()

    async def blocked(config, key, transition, current, session):
        started.set()
        await release.wait()
        if current():
            harness.sent.append(transition.present)
        checked.set()

    monkeypatch.setattr(event_runner, "follow_transition", blocked)
    await harness.start()
    harness.present = False
    harness.hub.device()
    await asyncio.wait_for(started.wait(), 1)
    harness.hub.power(True, deliver=False)
    harness.hub.power(False, deliver=False)
    release.set()
    await asyncio.wait_for(checked.wait(), 3)
    assert harness.sent == []
    await harness.finish()


async def test_suspend_loss_and_resume_are_silent_but_next_physical_edge_works(
    monkeypatch, tmp_path
):
    harness = Harness(monkeypatch, tmp_path)
    await harness.start()
    harness.hub.power(True)
    harness.present = False
    harness.hub.device()
    await harness.until(lambda: harness.tracker.stable is None)
    assert harness.sent == []
    harness.hub.power(False)
    await harness.until(lambda: harness.tracker.stable is False)
    assert harness.sent == []
    harness.present = True
    harness.hub.device()
    await harness.until(lambda: harness.sent == [True])
    assert harness.sent == [True]
    await harness.finish()


async def test_failed_inventory_rebaselines_without_inferring_disconnect(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    await harness.start()
    harness.fail_sample = True
    harness.hub.device()
    await harness.until(lambda: harness.failed_samples > 0)
    harness.fail_sample = False
    harness.present = False
    harness.hub.device()
    await harness.until(lambda: harness.tracker.stable is False)
    assert harness.sent == []
    await harness.finish()


@pytest.mark.parametrize("target_present", [True, False])
async def test_early_callback_old_inventory_recovers_without_second_callback(
    monkeypatch, tmp_path, target_present
):
    # Disable slow reconciliation: success must come from the short settle burst.
    monkeypatch.setattr(event_runner, "RECONCILE_INTERVAL", 60)
    harness = Harness(monkeypatch, tmp_path)
    harness.present = not target_present
    await harness.start()
    before = harness.samples
    harness.hub.device()
    await harness.until(lambda: harness.samples > before)
    assert harness.tracker.stable is not target_present
    assert harness.sent == []
    harness.present = target_present  # Inventory catches up, but Windows sends no new callback.
    await harness.until(lambda: harness.sent == [target_present])
    assert harness.hub.revision == 1
    await harness.finish()


@pytest.mark.parametrize("target_present", [True, False])
async def test_lost_callback_recovers_on_reconciliation_and_debounce(
    monkeypatch, tmp_path, target_present
):
    interval = 0.1
    monkeypatch.setattr(event_runner, "RECONCILE_INTERVAL", interval)
    harness = Harness(monkeypatch, tmp_path)
    harness.present = not target_present
    await harness.start()
    harness.present = target_present
    # Extra wall time is CI scheduling tolerance, not an additional retry interval.
    async with asyncio.timeout(interval + harness.config.debounce + 1):
        await harness.until(lambda: harness.sent == [target_present])
    assert harness.hub.revision == 0
    await harness.finish()


async def test_steady_idle_reconciles_at_low_frequency_without_sending(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    await harness.start()
    before = harness.samples
    started = time.monotonic()
    # This is an observation period for the default one-second recovery cadence.
    await asyncio.sleep(event_runner.RECONCILE_INTERVAL + 0.1)
    elapsed = time.monotonic() - started
    count = harness.samples - before
    assert 1 <= count <= int(elapsed / event_runner.RECONCILE_INTERVAL) + 1
    assert harness.sent == []
    await harness.finish()


async def test_reconciliation_respects_suspend_and_silent_resume_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(event_runner, "RECONCILE_INTERVAL", 0.1)
    harness = Harness(monkeypatch, tmp_path)
    await harness.start()
    harness.hub.power(True, deliver=False)
    harness.present = False  # Hub powers down without a delivered device callback.
    await harness.until(lambda: harness.tracker.stable is None)
    samples = harness.samples
    await asyncio.sleep(0.15)
    assert harness.samples == samples
    assert harness.sent == []
    harness.hub.power(False, deliver=False)
    await harness.until(lambda: harness.tracker.stable is False)
    assert harness.sent == []
    harness.present = True
    await harness.until(lambda: harness.sent == [True])
    assert harness.hub.queue.empty()
    await harness.finish()


async def test_stop_cancels_active_command_and_closes_all_owners(monkeypatch, tmp_path):
    harness = Harness(monkeypatch, tmp_path)
    started = asyncio.Event()
    cancelled = False

    async def blocked(*args):
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    session = SimpleNamespace(keep_warm=AsyncMock(side_effect=lambda: None), close=AsyncMock())
    monkeypatch.setattr(event_runner, "follow_transition", blocked)
    await harness.start(session)
    harness.present = False
    harness.hub.device()
    await asyncio.wait_for(started.wait(), 1)
    await harness.finish()
    assert cancelled
    assert harness.hub.closed and harness.stop.closed
    session.close.assert_awaited_once()


async def test_preexisting_stop_does_not_initialize_native_sources(monkeypatch, tmp_path):
    config = Config("192.168.0.20", "HDMI_1", (), switch_hub=HUB)
    path = tmp_path / "stop"
    path.touch()
    monkeypatch.setattr(
        event_runner, "HubNotifications", lambda *args: pytest.fail("Must stop first")
    )
    session = SimpleNamespace(keep_warm=AsyncMock(), close=AsyncMock())
    await event_runner.run(config, "key", path, session)
    session.close.assert_awaited_once()
    session.keep_warm.assert_not_awaited()


async def test_native_registration_error_closes_session_and_propagates(monkeypatch, tmp_path):
    config = Config("192.168.0.20", "HDMI_1", (), switch_hub=HUB)

    def fail(*args):
        raise RuntimeError("register failed")

    monkeypatch.setattr(event_runner, "HubNotifications", fail)
    session = SimpleNamespace(keep_warm=AsyncMock(), close=AsyncMock())
    with pytest.raises(RuntimeError, match="register failed"):
        await event_runner.run(config, "key", tmp_path / "stop", session)
    session.close.assert_awaited_once()


async def test_hub_config_routes_to_native_reactor(monkeypatch):
    config = Config("192.168.0.20", "HDMI_1", (), switch_hub=HUB)
    reactor = AsyncMock()
    monkeypatch.setattr(event_runner, "run", reactor)
    await runner.run(config, "key")
    reactor.assert_awaited_once_with(config, "key", None, None)


@pytest.fixture
def pending_guard(monkeypatch):
    config = Config("192.168.0.20", "HDMI_1", (), switch_hub=HUB)
    tracker = OwnershipTracker(debounce=0.05, resume_gap=float("inf"))
    tracker.update(False, 0)
    tracker.update(False, 0.1)
    tracker.update(True, 0.2)
    event = tracker.update(True, 0.3)
    assert event is not None
    source = FakeHub()
    monkeypatch.setattr(event_runner, "time", SimpleNamespace(time=lambda: 0.3))
    return lambda: event_runner.event_current(tracker, event, config, source, 0, 0.3), source


def test_callback_during_inventory_read_requires_new_snapshot(monkeypatch, pending_guard):
    guard, source = pending_guard
    samples = 0

    def sample(config):
        nonlocal samples
        samples += 1
        if samples == 1:
            source.revision += 1
            return True  # Old snapshot arrived while physical reversal was notified.
        return False

    monkeypatch.setattr(event_runner, "sample_ownership", sample)
    assert not guard()
    assert samples == 2


def test_duplicate_callback_during_inventory_read_allows_one_resample(monkeypatch, pending_guard):
    guard, source = pending_guard
    samples = 0

    def sample(config):
        nonlocal samples
        samples += 1
        if samples == 1:
            source.revision += 1
        return True

    monkeypatch.setattr(event_runner, "sample_ownership", sample)
    assert guard()
    assert samples == 2


def test_changing_inventory_is_bounded_to_two_snapshots(monkeypatch, pending_guard):
    guard, source = pending_guard
    samples = 0

    def sample(config):
        nonlocal samples
        samples += 1
        source.revision += 1
        return True

    monkeypatch.setattr(event_runner, "sample_ownership", sample)
    assert not guard()
    assert samples == 2


def test_power_callback_during_inventory_read_vetoes_without_resampling(monkeypatch, pending_guard):
    guard, source = pending_guard
    samples = 0

    def sample(config):
        nonlocal samples
        samples += 1
        source.power(True, deliver=False)
        source.power(False, deliver=False)
        return True

    monkeypatch.setattr(event_runner, "sample_ownership", sample)
    assert not guard()
    assert samples == 1
