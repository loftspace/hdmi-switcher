"""Cancel obsolete input commands; never replay missed transitions later."""

import asyncio
import logging
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from websockets.exceptions import ConnectionClosed

from hdmi_follow import tv
from hdmi_follow.config import Config
from hdmi_follow.devices import enumerate_devices, owns_devices
from hdmi_follow.ownership import OwnershipTracker, Transition
from hdmi_follow.tv_session import Session

LOG = logging.getLogger(__name__)


def sample_ownership(config: Config) -> bool:
    if config.switch_hub:
        from hdmi_follow.windows_usb import is_present

        return is_present(config.switch_hub)
    return owns_devices(enumerate_devices(), config.devices)


def still_current(tracker: OwnershipTracker, event: Transition, config: Config) -> bool:
    # Wall time includes Mac suspend; a network task may resume before the USB poll.
    if tracker.last is None or not 0 <= time.time() - tracker.last <= tracker.resume_gap:
        return False
    if not tracker.current(event):
        return False
    try:
        return sample_ownership(config) == event.present
    except Exception:
        return False


async def follow_transition(
    config: Config,
    key: str,
    transition: Transition,
    current: Callable[[], bool],
    session: Session | None = None,
) -> None:
    target = config.own_input if transition.present else config.away_input
    if not target:
        return
    for attempt in range(2):
        if not current():
            return
        try:
            started = time.perf_counter()
            if session is not None:
                await session.switch(target, current)
            else:
                await tv.switch(config.tv_ip, key, target, current)
            LOG.info(
                "Selected %s (TV command %.0f ms)", target, (time.perf_counter() - started) * 1000
            )
            return
        except (tv.PairingRequired, tv.StaleRequest):
            LOG.warning("Switch not performed: pairing or ownership changed")
            return
        except (OSError, TimeoutError, tv.TVError, ConnectionClosed) as exc:
            LOG.warning("TV unavailable (%s), attempt %s/2", type(exc).__name__, attempt + 1)
        except Exception as exc:
            # A malformed response or protocol error must not terminate the USB watcher.
            LOG.warning("TV protocol failure (%s); dropping this event", type(exc).__name__)
            return
        if attempt == 0:
            await asyncio.sleep(0.5)


async def run(
    config: Config, key: str, stop_file: Path | None = None, session: Session | None = None
) -> None:
    tracker = OwnershipTracker(config.debounce)
    pending: asyncio.Task[None] | None = None
    warm = asyncio.create_task(session.keep_warm()) if session is not None else None
    LOG.info(
        "Watching %s; initial ownership does not switch the TV",
        "switch hub" if config.switch_hub else "keyboard USB metadata",
    )
    try:
        while True:
            if stop_file is not None and stop_file.exists():
                LOG.info("Stop requested")
                return
            try:
                present: bool | None = sample_ownership(config)
            except Exception as exc:
                LOG.warning("USB enumeration failed (%s); suspending ownership", type(exc).__name__)
                present = None
            before = tracker.generation
            transition = tracker.update(present, time.time())
            if pending is not None and before != tracker.generation:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
            if transition:
                LOG.info("USB ownership %s", "acquired" if transition.present else "released")
                pending = asyncio.create_task(
                    follow_transition(
                        config,
                        key,
                        transition,
                        partial(still_current, tracker, transition, config),
                        session,
                    )
                )
            await asyncio.sleep(config.poll_interval)
    finally:
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if warm:
            warm.cancel()
            await asyncio.gather(warm, return_exceptions=True)
        if session is not None:
            await session.close()
