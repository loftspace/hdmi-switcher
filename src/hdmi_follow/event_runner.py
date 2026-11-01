"""Fast native hub notifications with low-frequency inventory reconciliation."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from hdmi_follow.config import Config
from hdmi_follow.ownership import OwnershipTracker, Transition
from hdmi_follow.runner import follow_transition, sample_ownership
from hdmi_follow.tv_session import Session
from hdmi_follow.windows_notifications import HubNotifications, Signal
from hdmi_follow.windows_stop import StopNotification

LOG = logging.getLogger(__name__)
RECONCILE_INTERVAL = 1.0
EVENT_SETTLE_WINDOW = 0.5
EVENT_RECHECK_INTERVAL = 0.05


def event_current(
    tracker: OwnershipTracker,
    event: Transition,
    config: Config,
    source: HubNotifications,
    power_revision: int,
    created: float,
) -> bool:
    """Fence suspend immediately, without depending on event-loop delivery order."""
    if not 0 <= time.time() - created <= 5:
        return False
    if source.suspended or source.power_revision != power_revision or not tracker.current(event):
        return False
    for _ in range(2):
        revision = source.revision
        try:
            matches = sample_ownership(config) == event.present
        except Exception:
            return False
        # A native callback may run while Configuration Manager is sampled. A
        # harmless duplicate gets one bounded resample, not a permanent veto.
        if (
            not matches
            or source.suspended
            or source.power_revision != power_revision
            or not 0 <= time.time() - created <= 5
            or not tracker.current(event)
        ):
            return False
        if source.revision == revision:
            return True
    return False


async def run(
    config: Config,
    key: str,
    stop_file: Path | None = None,
    session: Session | None = None,
) -> None:
    if stop_file is not None and stop_file.exists():
        if session is not None:
            await session.close()
        return
    if not config.switch_hub:
        raise ValueError("Native event mode requires a configured switch hub")
    source: HubNotifications | None = None
    stop: StopNotification | None = None
    device_task: asyncio.Task[Signal] | None = None
    stop_task: asyncio.Task[None] | None = None
    timer: asyncio.Task[None] | None = None
    pending: asyncio.Task[None] | None = None
    warm: asyncio.Task[None] | None = None
    handled_power = 0
    recheck_until = 0.0
    # Reconciliation supplies regular samples, so a long gap now indicates
    # suspend or scheduler starvation even if the power notification was missed.
    tracker = OwnershipTracker(config.debounce)

    async def cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def reset() -> None:
        nonlocal timer, pending
        tracker.update(None, time.time())
        await cancel(pending)
        pending = None
        await cancel(timer)
        timer = None

    async def sample(recovery: bool = False) -> None:
        nonlocal timer, pending, handled_power
        assert source is not None
        epoch = source.power_revision
        if epoch != handled_power:
            handled_power = epoch
            await reset()
        if source.suspended:
            await reset()
            return
        try:
            present: bool | None = sample_ownership(config)
        except Exception as exc:
            LOG.warning("USB inventory failed (%s); waiting for another event", type(exc).__name__)
            present = None
        if source.suspended or source.power_revision != epoch:
            await reset()
            return
        if (
            recovery
            and present is not None
            and tracker.stable is not None
            and present != tracker.stable
            and present != tracker.candidate
        ):
            LOG.warning("Recovery check found a missed or delayed hub transition")
        now = time.time()
        before = tracker.generation
        transition = tracker.update(present, now)
        if before != tracker.generation:
            await cancel(pending)
            pending = None
        if transition is not None:
            LOG.info("USB ownership %s", "acquired" if transition.present else "released")

            def current() -> bool:
                assert source is not None
                return event_current(tracker, transition, config, source, epoch, now)

            pending = asyncio.create_task(
                follow_transition(config, key, transition, current, session)
            )
        # Native arrival can precede the present-device inventory update. Keep
        # checking briefly even when the first read still matches the old state.
        await cancel(timer)
        timer = None
        delay: float | None = None
        if tracker.candidate is not None and (
            tracker.stable is None or tracker.candidate != tracker.stable
        ):
            delay = max(0.0, tracker.since + tracker.debounce - time.time())
        if time.monotonic() < recheck_until:
            delay = (
                min(delay, EVENT_RECHECK_INTERVAL) if delay is not None else EVENT_RECHECK_INTERVAL
            )
        if delay is not None:
            timer = asyncio.create_task(asyncio.sleep(delay))

    try:
        loop = asyncio.get_running_loop()
        source = HubNotifications(config.switch_hub, loop)
        if stop_file is not None:
            stop = StopNotification(stop_file, loop)
            stop_task = asyncio.create_task(stop.wait())
        if session is not None:
            warm = asyncio.create_task(session.keep_warm())
        handled_power = source.power_revision
        device_task = asyncio.create_task(source.wait())
        await sample()  # Register first, then establish a silent ownership baseline.
        LOG.info("Watching native hub events; %.1fs recovery checks", RECONCILE_INTERVAL)
        while True:
            waiters: set[asyncio.Task[Any]] = {device_task}
            if stop_task is not None:
                waiters.add(stop_task)
            if timer is not None:
                waiters.add(timer)
            done, _ = await asyncio.wait(
                waiters, timeout=RECONCILE_INTERVAL, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                await sample(recovery=True)
                continue
            if stop_task is not None and stop_task in done:
                stop_task.result()
                LOG.info("Stop requested")
                return
            if device_task in done:
                signal = device_task.result()
                device_task = asyncio.create_task(source.wait())
                if signal.reason == "device":
                    LOG.info("Hub notification received (revision %s)", signal.revision)
                    recheck_until = time.monotonic() + EVENT_SETTLE_WINDOW
                if signal.reason == "reset" or source.power_revision != handled_power:
                    handled_power = source.power_revision
                    await reset()
                await sample()
            if timer is not None and timer in done:
                timer.result()
                timer = None
                if source.power_revision != handled_power:
                    handled_power = source.power_revision
                    await reset()
                await sample()
    finally:
        tasks = [
            task for task in (device_task, stop_task, timer, pending, warm) if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        errors: list[Exception] = []
        for owner in (source, stop):
            if owner is not None:
                try:
                    owner.close()
                except Exception as exc:
                    errors.append(exc)
        if session is not None:
            try:
                await session.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
