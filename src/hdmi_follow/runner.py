"""Cancel obsolete input commands; never replay missed transitions later."""

import asyncio
import logging
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from hdmi_follow import tv
from hdmi_follow.config import Config
from hdmi_follow.devices import enumerate_devices, owns_devices
from hdmi_follow.ownership import OwnershipTracker, Transition

LOG = logging.getLogger(__name__)


def still_current(tracker: OwnershipTracker, event: Transition, config: Config) -> bool:
    # Wall time includes Mac suspend; a network task may resume before the USB poll.
    if tracker.last is None or not 0 <= time.time() - tracker.last <= tracker.resume_gap:
        return False
    if not tracker.current(event):
        return False
    try:
        return owns_devices(enumerate_devices(), config.devices) == event.present
    except Exception:
        return False


async def follow_transition(
    config: Config, key: str, transition: Transition, current: Callable[[], bool]
) -> None:
    target = config.own_input if transition.present else config.away_input
    if not target:
        return
    for attempt in range(2):
        if not current():
            return
        try:
            await tv.switch(config.tv_ip, key, target, current)
            LOG.info("Selected %s", target)
            return
        except (tv.PairingRequired, tv.StaleRequest):
            LOG.warning("Switch not performed: pairing or ownership changed")
            return
        except (OSError, TimeoutError, tv.TVError) as exc:
            LOG.warning("TV unavailable (%s), attempt %s/2", type(exc).__name__, attempt + 1)
        except Exception as exc:
            # A malformed response or protocol error must not terminate the USB watcher.
            LOG.warning("TV protocol failure (%s); dropping this event", type(exc).__name__)
            return
        if attempt == 0:
            await asyncio.sleep(0.5)


async def run(config: Config, key: str, stop_file: Path | None = None) -> None:
    tracker = OwnershipTracker(config.debounce)
    pending: asyncio.Task[None] | None = None
    LOG.info("Watching USB metadata; initial ownership does not switch the TV")
    try:
        while True:
            if stop_file is not None and stop_file.exists():
                LOG.info("Stop requested")
                return
            try:
                present: bool | None = owns_devices(enumerate_devices(), config.devices)
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
                        config, key, transition, partial(still_current, tracker, transition, config)
                    )
                )
            await asyncio.sleep(config.poll_interval)
    finally:
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
