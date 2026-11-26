"""A stall without suspend must not lose a real hub change (issue #13)."""

import asyncio
import json
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest
from test_event_runner import Harness
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from hdmi_follow import event_runner, runner, tv, tv_session
from hdmi_follow.ownership import OwnershipTracker
from hdmi_follow.tv_session import Session


class Clock:
    """Wall clock that the test can move forward, as a starved event loop would see it."""

    def __init__(self, monkeypatch):
        self.offset = 0.0
        fake = SimpleNamespace(time=self.time, monotonic=time.monotonic)
        monkeypatch.setattr(event_runner, "time", fake)

    def time(self):
        return time.time() + self.offset


class LocalTV:
    """A real Session against a local WebSocket TV; records the selected apps."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.launched = []

    async def _serve(self, ws):
        await ws.recv()
        await ws.send(
            json.dumps({"id": "register", "type": "registered", "payload": {"client-key": "key"}})
        )
        with suppress(ConnectionClosed):
            async for frame in ws:
                command = json.loads(frame)
                self.launched.append(command["payload"]["id"])
                await ws.send(
                    json.dumps(
                        {
                            "id": command["id"],
                            "type": "response",
                            "payload": {"returnValue": True},
                        }
                    )
                )

    async def __aenter__(self):
        self.server = await serve(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.monkeypatch.setattr(
            tv,
            "_connect",
            lambda _: connect(f"ws://127.0.0.1:{port}", proxy=None, close_timeout=0.1),
        )
        self.session = Session("192.168.0.20", "key")
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()


def stall_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(event_runner, "RECONCILE_INTERVAL", 0.02)
    harness = Harness(monkeypatch, tmp_path)
    monkeypatch.setattr(event_runner, "follow_transition", runner.follow_transition)
    return harness


async def finish(harness, television):
    harness.stop.event.set()
    await asyncio.wait_for(harness.task, 5)
    await television.session.close()


async def wait_for(condition, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("stall", [5.2, 14.2, 30.9])
async def test_hub_change_during_a_stall_still_switches(monkeypatch, tmp_path, stall):
    harness = stall_harness(monkeypatch, tmp_path)
    clock = Clock(monkeypatch)
    async with LocalTV(monkeypatch) as television:
        await harness.start(television.session)
        clock.offset += stall  # No power notification arrives.
        harness.present = False
        await wait_for(lambda: television.launched == ["com.webos.app.hdmi3"])
        assert harness.hub.power_revision == 0
        await finish(harness, television)


async def test_back_to_back_stalls_still_switch(monkeypatch, tmp_path):
    """2026-11-26: almost every reading arrived after another stall."""
    harness = stall_harness(monkeypatch, tmp_path)
    clock = Clock(monkeypatch)
    async with LocalTV(monkeypatch) as television:
        await harness.start(television.session)
        harness.present = False

        def stalled_sample(config):
            clock.offset += 6
            return harness.sample(config)

        monkeypatch.setattr(event_runner, "sample_ownership", stalled_sample)
        await wait_for(lambda: television.launched[:1] == ["com.webos.app.hdmi3"])
        await asyncio.sleep(0.1)
        assert television.launched == ["com.webos.app.hdmi3"]
        await finish(harness, television)


async def test_stall_with_no_hub_change_sends_nothing(monkeypatch, tmp_path):
    harness = stall_harness(monkeypatch, tmp_path)
    clock = Clock(monkeypatch)
    async with LocalTV(monkeypatch) as television:
        await harness.start(television.session)
        before = harness.samples
        clock.offset += 30
        await wait_for(lambda: harness.samples > before + 3)
        assert harness.tracker.stable is True
        assert television.launched == []
        await finish(harness, television)


async def test_gap_over_the_stall_limit_stays_silent(monkeypatch, tmp_path):
    harness = stall_harness(monkeypatch, tmp_path)
    clock = Clock(monkeypatch)
    async with LocalTV(monkeypatch) as television:
        await harness.start(television.session)
        clock.offset += event_runner.STALL_LIMIT + 5
        harness.present = False
        await wait_for(lambda: harness.tracker.stable is False)
        await asyncio.sleep(0.1)
        assert television.launched == []
        await finish(harness, television)


async def test_suspend_inside_the_stall_limit_stays_silent(monkeypatch, tmp_path):
    harness = stall_harness(monkeypatch, tmp_path)
    clock = Clock(monkeypatch)
    async with LocalTV(monkeypatch) as television:
        await harness.start(television.session)
        # Slept and woke within the limit; the callback is seen only by its revision.
        harness.hub.power(True, deliver=False)
        harness.hub.power(False, deliver=False)
        clock.offset += 20
        harness.present = False
        await wait_for(lambda: harness.tracker.stable is False)
        await asyncio.sleep(0.1)
        assert television.launched == []
        await finish(harness, television)


async def test_slow_inventory_does_not_age_out_or_time_out_the_switch(
    monkeypatch, tmp_path, caplog
):
    """Scaled model: each USB inventory outlasts the age limit and the TV command timeout."""
    caplog.set_level("INFO")
    harness = stall_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(event_runner, "MAX_COMMAND_AGE", 0.5)
    monkeypatch.setattr(tv_session, "COMMAND_TIMEOUT", 0.4)
    monkeypatch.setattr(tv_session, "PROBE_REPLY_TIMEOUT", 0.15)
    async with LocalTV(monkeypatch) as television:
        await harness.start(television.session)

        def slow_sample(config):
            time.sleep(0.6)  # Blocks the loop, as the real inventory does.
            return harness.sample(config)

        monkeypatch.setattr(event_runner, "sample_ownership", slow_sample)
        harness.present = False
        harness.hub.device()
        await wait_for(lambda: "Selected HDMI_3" in caplog.text, timeout=15)
        assert television.launched == ["com.webos.app.hdmi3"]
        assert "TimeoutError" not in caplog.text
        await finish(harness, television)


def test_tracker_default_still_rebaselines_after_resume_gap():
    """The keyboard path (Mac) keeps the old rule: it opts in to nothing."""
    tracker = OwnershipTracker(debounce=1)
    tracker.update(False, 0)
    tracker.update(False, 1)
    assert tracker.update(True, 20) is None
    assert tracker.stable is None
