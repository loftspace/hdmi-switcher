"""Fast native hub notifications with low-frequency inventory reconciliation."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from hdmi_follow import diagnostics
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
HEARTBEAT_INTERVAL = 30.0
SLOW_SAMPLE_THRESHOLD = 0.05
# A gap up to this long with no suspend notification is a stall: keep the last
# known ownership. Longest stall seen on 2026-11-26 was 30.9 s (issue #13).
STALL_LIMIT = 60.0
MAX_COMMAND_AGE = 5.0


def event_current(
    tracker: OwnershipTracker,
    event: Transition,
    config: Config,
    source: HubNotifications,
    power_revision: int,
    created: float,
    logged_rejections: set[str] | None = None,
    inventory_seconds: list[float] | None = None,
) -> bool:
    """Fence suspend immediately, without depending on event-loop delivery order."""

    def rejected(reason: str, observed: bool | None = None) -> bool:
        if logged_rejections is None or reason not in logged_rejections:
            LOG.info(
                "Switch guard rejected reason=%s age=%.3fs event_generation=%s "
                "generation=%s native_revision=%s power_revision=%s expected_power=%s "
                "suspended=%s expected_present=%s observed_present=%s",
                reason,
                time.time() - created,
                event.generation,
                tracker.generation,
                source.revision,
                source.power_revision,
                power_revision,
                source.suspended,
                event.present,
                observed,
            )
            if logged_rejections is not None:
                logged_rejections.add(reason)
        return False

    # Time spent in this guard's own USB inventories is not staleness: each one
    # ends in a fresh look at the hub. STALL_LIMIT still bounds the total age.
    spent = inventory_seconds if inventory_seconds is not None else [0.0]

    def stale() -> bool:
        age = time.time() - created
        return not (0 <= age <= STALL_LIMIT and age - spent[0] <= MAX_COMMAND_AGE)

    if stale():
        return rejected("stale-age")
    if source.suspended:
        return rejected("suspended")
    if source.power_revision != power_revision:
        return rejected("power-revision")
    if not tracker.current(event):
        return rejected("ownership-generation")
    for _ in range(2):
        revision = source.revision
        try:
            started = time.time()
            observed = sample_ownership(config)
            spent[0] += max(0.0, time.time() - started)
            matches = observed == event.present
        except Exception:
            return rejected("inventory-error")
        # A native callback may run while Configuration Manager is sampled. A
        # harmless duplicate gets one bounded resample, not a permanent veto.
        if not matches:
            return rejected("hub-mismatch", observed)
        if source.suspended:
            return rejected("suspended", observed)
        if source.power_revision != power_revision:
            return rejected("power-revision", observed)
        if stale():
            return rejected("stale-age", observed)
        if not tracker.current(event):
            return rejected("ownership-generation", observed)
        if source.revision == revision:
            return True
    return rejected("native-revision-churn")


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
    last_heartbeat = time.monotonic()
    last_present: bool | None = None
    have_inventory = False
    last_reset_reason: str | None = None
    # Reconciliation supplies regular samples, and suspend arrives as a power
    # revision. A gap up to STALL_LIMIT without one is a stall and keeps ownership;
    # a longer one may be a suspend whose notification was missed.
    tracker = OwnershipTracker(config.debounce, stall_limit=STALL_LIMIT)

    async def cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def heartbeat(origin: str, present: bool | None, gap: float | None) -> None:
        nonlocal last_heartbeat
        now = time.monotonic()
        if now - last_heartbeat < HEARTBEAT_INTERVAL:
            return
        last_heartbeat = now
        assert source is not None
        pending_state = (
            "none"
            if pending is None
            else ("cancelled" if pending.cancelled() else "done" if pending.done() else "running")
        )
        try:
            health = diagnostics.health_summary()
        except Exception as exc:
            health = "health-unavailable:" + type(exc).__name__
        LOG.info(
            "Monitor heartbeat origin=%s present=%s stable=%s candidate=%s generation=%s "
            "native_revision=%s power_revision=%s suspended=%s sample_gap=%s pending=%s %s",
            origin,
            present,
            tracker.stable,
            tracker.candidate,
            tracker.generation,
            source.revision,
            source.power_revision,
            source.suspended,
            "initial" if gap is None else f"{gap:.3f}s",
            pending_state,
            health,
        )

    async def cancel_pending(reason: str) -> None:
        nonlocal pending
        if pending is not None and not pending.done():
            LOG.info("Pending switch cancelled reason=%s generation=%s", reason, tracker.generation)
        await cancel(pending)
        pending = None

    async def reset(reason: str, origin: str) -> None:
        nonlocal timer, last_reset_reason
        assert source is not None
        prior = (tracker.stable, tracker.candidate, tracker.generation)
        tracker.update(None, time.time())
        if reason != last_reset_reason or prior[0] is not None or prior[1] is not None:
            LOG.info(
                "Ownership reset reason=%s origin=%s prior_stable=%s prior_candidate=%s "
                "generation=%s->%s power_revision=%s suspended=%s",
                reason,
                origin,
                prior[0],
                prior[1],
                prior[2],
                tracker.generation,
                source.power_revision,
                source.suspended,
            )
        last_reset_reason = reason
        await cancel_pending(reason)
        await cancel(timer)
        timer = None

    async def sample(recovery: bool = False, origin: str = "initial") -> None:
        nonlocal timer, pending, handled_power, last_present, have_inventory, last_reset_reason
        assert source is not None
        epoch = source.power_revision
        if epoch != handled_power:
            handled_power = epoch
            await reset("power-revision-change", origin)
        if source.suspended:
            gap = None if tracker.last is None else time.time() - tracker.last
            await reset("suspended", origin)
            heartbeat(origin, None, gap)
            return
        started = time.monotonic()
        try:
            present: bool | None = sample_ownership(config)
        except Exception as exc:
            LOG.warning("USB inventory failed (%s); waiting for another event", type(exc).__name__)
            present = None
        duration = time.monotonic() - started
        if duration >= SLOW_SAMPLE_THRESHOLD:
            LOG.info("Slow USB inventory origin=%s duration=%.3fs", origin, duration)
        if source.suspended or source.power_revision != epoch:
            await reset("power-during-inventory", origin)
            heartbeat(origin, present, None)
            return
        if not have_inventory or present != last_present:
            LOG.info(
                "USB inventory changed origin=%s present=%s->%s native_revision=%s",
                origin,
                last_present if have_inventory else "initial",
                present,
                source.revision,
            )
        last_present = present
        have_inventory = True
        if (
            recovery
            and present is not None
            and tracker.stable is not None
            and present != tracker.stable
            and present != tracker.candidate
        ):
            LOG.warning("Recovery check found a missed or delayed hub transition")
        now = time.time()
        gap = None if tracker.last is None else now - tracker.last
        if gap is not None and tracker.resume_gap < gap <= tracker.max_gap:
            LOG.info(
                "Sampling stall origin=%s elapsed=%.3fs prior_stable=%s prior_candidate=%s "
                "generation=%s; ownership kept",
                origin,
                gap,
                tracker.stable,
                tracker.candidate,
                tracker.generation,
            )
        if gap is not None and not 0 <= gap <= tracker.max_gap:
            LOG.info(
                "Sampling gap origin=%s elapsed=%.3fs prior_stable=%s prior_candidate=%s "
                "generation=%s; silent baseline reset",
                origin,
                gap,
                tracker.stable,
                tracker.candidate,
                tracker.generation,
            )
        prior = (tracker.stable, tracker.candidate, tracker.generation)
        before = tracker.generation
        transition = tracker.update(present, now)
        after = (tracker.stable, tracker.candidate, tracker.generation)
        if prior != after:
            LOG.info(
                "Ownership state origin=%s stable=%s->%s candidate=%s->%s generation=%s->%s "
                "silent_baseline=%s",
                origin,
                prior[0],
                after[0],
                prior[1],
                after[1],
                prior[2],
                after[2],
                prior[0] is None and after[0] is not None,
            )
        if present is not None:
            last_reset_reason = None
        if before != tracker.generation:
            await cancel_pending("ownership-generation-change")
        if transition is not None:
            LOG.info("USB ownership %s", "acquired" if transition.present else "released")
            logged_rejections: set[str] = set()
            inventory_seconds = [0.0]

            def current() -> bool:
                assert source is not None
                return event_current(
                    tracker,
                    transition,
                    config,
                    source,
                    epoch,
                    now,
                    logged_rejections,
                    inventory_seconds,
                )

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
        heartbeat(origin, present, gap)

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
                await sample(recovery=True, origin="reconcile")
                continue
            if stop_task is not None and stop_task in done:
                stop_task.result()
                LOG.info(
                    "Stop requested %s",
                    diagnostics.stop_summary(stop_file)
                    if stop_file
                    else "requester_pid=unknown reason=unknown",
                )
                return
            if device_task in done:
                signal = device_task.result()
                device_task = asyncio.create_task(source.wait())
                LOG.info(
                    "Hub notification received reason=%s native_action=%s revision=%s "
                    "power_revision=%s suspended=%s",
                    signal.reason,
                    getattr(signal, "native_action", None),
                    signal.revision,
                    source.power_revision,
                    source.suspended,
                )
                if signal.reason == "device":
                    recheck_until = time.monotonic() + EVENT_SETTLE_WINDOW
                if signal.reason == "reset" or source.power_revision != handled_power:
                    handled_power = source.power_revision
                    await reset("native-power-reset", "native")
                await sample(origin="native")
            if timer is not None and timer in done:
                timer.result()
                timer = None
                if source.power_revision != handled_power:
                    handled_power = source.power_revision
                    await reset("power-before-debounce", "debounce")
                await sample(origin="debounce")
    finally:
        LOG.info("Monitor shutdown stage=cancel_tasks")
        tasks = [
            task for task in (device_task, stop_task, timer, pending, warm) if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        LOG.info("Monitor shutdown stage=tasks_cancelled")
        errors: list[Exception] = []
        for owner in (source, stop):
            if owner is not None:
                try:
                    LOG.info(
                        "Monitor shutdown stage=close_begin component=%s", type(owner).__name__
                    )
                    owner.close()
                    LOG.info("Monitor shutdown stage=close_done component=%s", type(owner).__name__)
                except Exception as exc:
                    LOG.warning(
                        "Monitor shutdown component=%s error_type=%s",
                        type(owner).__name__,
                        type(exc).__name__,
                    )
                    errors.append(exc)
        if session is not None:
            try:
                await session.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
        LOG.info("Monitor shutdown stage=complete")
