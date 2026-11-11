"""One authenticated SSAP connection per helper, without replaying input commands."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Callable

from websockets.asyncio.client import ClientConnection
from websockets.protocol import State

from hdmi_follow import tv

RECONNECT_INTERVAL = 5.0
COMMAND_TIMEOUT = 4.0
HEALTH_TIMEOUT = 1.0
# A reused socket whose last proof of health is older than this may be
# half-open. Its first reply gets a short wait, then one clean reconnect.
PROOF_MAX_AGE = 2.0
PROBE_REPLY_TIMEOUT = 1.5
LOG = logging.getLogger(__name__)


class Session:
    """Serialize commands and discard the entire connection after interrupted IO."""

    def __init__(self, ip: str, key: str) -> None:
        self._ip = ip
        self._key = key
        self._ws: ClientConnection | None = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._disabled = False
        self._closed = False
        self._online = False
        self._failure_type: str | None = None
        # Monotonic time of the last register, pong or command reply.
        self._proof: float | None = None

    def _report_failure(self, error_type: str) -> None:
        if self._failure_type != error_type:
            LOG.warning("TV connection unavailable error=%s", error_type)
        self._failure_type = error_type

    def _report_ready(self) -> None:
        if self._failure_type is not None:
            LOG.info("TV connection recovered error=%s", self._failure_type)
        elif not self._online:
            LOG.info("TV connection connected")
        self._failure_type = None
        self._online = True

    def _invalidate(self) -> None:
        # Abort rather than waiting for a close handshake after a command timeout.
        # A subsequent command gets another socket and another unique request ID.
        ws, self._ws = self._ws, None
        self._proof = None
        if ws is not None:
            if self._online:
                LOG.info("TV connection disconnected")
            self._online = False
            ws.transport.abort()

    async def _ensure_connected(self) -> ClientConnection:
        if self._closed:
            raise tv.TVError("TV session is closed")
        if self._disabled:
            raise tv.PairingRequired("TV pairing was rejected; pair explicitly and restart")
        if self._ws is not None and self._ws.state is State.OPEN:
            return self._ws
        self._invalidate()
        if self._failure_type is None:
            LOG.info("TV connection attempt")
        try:
            self._ws = await tv._connect(self._ip)
            if self._closed:
                raise tv.TVError("TV session is closed")
            self._key = await tv._register(self._ws, self._key, False)
            if self._closed:
                raise tv.TVError("TV session is closed")
            self._proof = time.monotonic()
            # During an outage, registration alone is not proof of recovery:
            # wait for the maintenance pong or a successful command reply.
            if self._failure_type is None:
                self._report_ready()
            return self._ws
        except tv.PairingRequired:
            self._disabled = True
            LOG.info("TV pairing rejected; automatic reconnect disabled")
            self._invalidate()
            raise
        except Exception as exc:
            self._report_failure(type(exc).__name__)
            self._invalidate()
            raise
        except BaseException:
            self._invalidate()
            raise

    async def _warm_once(self) -> None:
        # Timeout covers lock acquisition as well, but a cancelled lock waiter
        # must not abort the socket owned by an active switch operation.
        if self._lock.locked():
            return
        async with asyncio.timeout(COMMAND_TIMEOUT), self._lock:
            try:
                ws = await self._ensure_connected()
                # Only idle maintenance pings. Input commands never pay for a
                # ping themselves, and a half-open socket is discarded promptly.
                async with asyncio.timeout(HEALTH_TIMEOUT):
                    pong = await ws.ping()
                    await pong
                self._proof = time.monotonic()
                self._report_ready()
            except BaseException:
                self._invalidate()
                raise

    async def keep_warm(self) -> None:
        """Connect quietly; never launch an input and never repeat rejected pairing."""
        while not self._closed and not self._disabled:
            try:
                await self._warm_once()
            except tv.PairingRequired:
                return
            except Exception as exc:
                # A protocol failure is retried on the next bounded warm attempt.
                # Do not log server-controlled bodies or credential-bearing frames.
                self._report_failure(type(exc).__name__)
            if not self._closed and not self._disabled:
                await asyncio.sleep(RECONNECT_INTERVAL)

    def _needs_probe(self) -> bool:
        """True when a reused open socket has no recent proof that the TV answers."""
        if self._ws is None or self._ws.state is not State.OPEN:
            return False
        return self._proof is None or time.monotonic() - self._proof > PROOF_MAX_AGE

    async def _launch(
        self, ws: ClientConnection, request_id: str, target: str, current: Callable[[], bool]
    ) -> None:
        if not current():
            raise tv.StaleRequest("USB ownership changed before the command was sent")
        LOG.info("TV switch send id=%s target=%s", request_id, target)
        await ws.send(
            json.dumps(
                {
                    "id": request_id,
                    "type": "request",
                    "uri": "ssap://system.launcher/launch",
                    "payload": {"id": f"com.webos.app.hdmi{target[-1]}"},
                }
            )
        )
        reply = await tv._receive(ws, request_id)
        body = reply.get("payload")
        if not isinstance(body, dict) or body.get("returnValue") is not True:
            raise tv.TVError("TV did not accept the command")
        self._proof = time.monotonic()

    async def switch(self, target: str, current: Callable[[], bool] = lambda: True) -> None:
        """Send while the USB transition is current; resend once after a probe timeout.

        A launch that may have reached the TV cannot be recalled. The resend is the
        same idempotent input launch and is guarded by current() again.
        """
        if target not in {f"HDMI_{number}" for number in range(1, 5)}:
            raise ValueError("Invalid HDMI input")
        request_id = f"switch-{next(self._ids)}"
        started = time.perf_counter()
        acquired = False
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT) as budget, self._lock:
                acquired = True
                if not current():
                    raise tv.StaleRequest("USB ownership changed before the command was sent")
                sent = False
                if self._needs_probe():
                    assert self._ws is not None
                    try:
                        async with asyncio.timeout(PROBE_REPLY_TIMEOUT) as probe:
                            await self._launch(self._ws, request_id, target, current)
                        sent = True
                    except TimeoutError:
                        if not probe.expired():
                            raise
                        # The socket may be half-open. A launch that did reach
                        # the TV cannot be recalled; resending the same input
                        # is idempotent and is still guarded by current().
                        LOG.info(
                            "TV switch probe timeout id=%s target=%s; reconnecting once",
                            request_id,
                            target,
                        )
                        self._invalidate()
                        request_id = f"switch-{next(self._ids)}"
                        budget.reschedule(asyncio.get_running_loop().time() + COMMAND_TIMEOUT)
                        if not current():
                            raise tv.StaleRequest(
                                "USB ownership changed before the command was resent"
                            ) from None
                if not sent:
                    ws = await self._ensure_connected()
                    await self._launch(ws, request_id, target, current)
                self._report_ready()
                LOG.info(
                    "TV switch reply id=%s target=%s duration_ms=%.1f",
                    request_id,
                    target,
                    (time.perf_counter() - started) * 1000,
                )
        except tv.StaleRequest:
            # Usually no command was sent and the warm connection stays. After a
            # probe timeout the first launch was sent and may have landed; only
            # the resend was dropped, and that socket is already discarded.
            LOG.info(
                "TV switch stale id=%s target=%s duration_ms=%.1f",
                request_id,
                target,
                (time.perf_counter() - started) * 1000,
            )
            raise
        except BaseException as exc:
            if acquired:
                self._invalidate()
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            if isinstance(exc, TimeoutError):
                outcome = "timeout"
            if acquired and isinstance(exc, Exception):
                self._report_failure(type(exc).__name__)
            LOG.info(
                "TV switch %s id=%s target=%s error=%s duration_ms=%.1f",
                outcome,
                request_id,
                target,
                type(exc).__name__,
                (time.perf_counter() - started) * 1000,
            )
            raise

    async def close(self) -> None:
        """Stop reuse immediately, including when another operation holds the lock."""
        if not self._closed:
            LOG.info("TV session closed")
        self._closed = True
        self._invalidate()
