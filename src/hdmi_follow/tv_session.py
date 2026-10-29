"""One authenticated SSAP connection per helper, without replaying input commands."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Callable

from websockets.asyncio.client import ClientConnection
from websockets.protocol import State

from hdmi_follow import tv

RECONNECT_INTERVAL = 5.0
COMMAND_TIMEOUT = 4.0
HEALTH_TIMEOUT = 1.0


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

    def _invalidate(self) -> None:
        # Abort rather than waiting for a close handshake after a command timeout.
        # A subsequent command gets another socket and another unique request ID.
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.transport.abort()

    async def _ensure_connected(self) -> ClientConnection:
        if self._closed:
            raise tv.TVError("TV session is closed")
        if self._disabled:
            raise tv.PairingRequired("TV pairing was rejected; pair explicitly and restart")
        if self._ws is not None and self._ws.state is State.OPEN:
            return self._ws
        self._invalidate()
        try:
            self._ws = await tv._connect(self._ip)
            if self._closed:
                raise tv.TVError("TV session is closed")
            self._key = await tv._register(self._ws, self._key, False)
            if self._closed:
                raise tv.TVError("TV session is closed")
            return self._ws
        except tv.PairingRequired:
            self._disabled = True
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
            except (OSError, TimeoutError, tv.TVError):
                pass
            except Exception:
                # A protocol failure is retried on the next bounded warm attempt.
                # Do not log server-controlled bodies or credential-bearing frames.
                pass
            if not self._closed and not self._disabled:
                await asyncio.sleep(RECONNECT_INTERVAL)

    async def switch(self, target: str, current: Callable[[], bool] = lambda: True) -> None:
        """Send once on the current socket, only while the USB transition is current."""
        if target not in {f"HDMI_{number}" for number in range(1, 5)}:
            raise ValueError("Invalid HDMI input")
        async with asyncio.timeout(COMMAND_TIMEOUT), self._lock:
            try:
                if not current():
                    raise tv.StaleRequest("USB ownership changed before the command was sent")
                ws = await self._ensure_connected()
                request_id = f"switch-{next(self._ids)}"
                if not current():
                    raise tv.StaleRequest("USB ownership changed before the command was sent")
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
            except tv.StaleRequest:
                # No command was sent. The healthy warm connection is still useful.
                raise
            except BaseException:
                self._invalidate()
                raise

    async def close(self) -> None:
        """Stop reuse immediately, including when another operation holds the lock."""
        self._closed = True
        self._invalidate()
