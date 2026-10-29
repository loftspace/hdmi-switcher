"""Bounded LG SSAP control. Credentials are never logged."""

import asyncio
import json
import ssl
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from hdmi_follow import __version__


class TVError(Exception):
    pass


class PairingRequired(TVError):
    pass


class StaleRequest(TVError):
    pass


MANIFEST = {
    "manifestVersion": 1,
    "appVersion": __version__,
    "appId": "org.loftspace.hdmifollow",
    "vendorId": "loftspace",
    "localizedAppNames": {"": "HDMI Follow"},
    "permissions": [
        "LAUNCH",
        "READ_APP_STATUS",
        "READ_INSTALLED_APPS",
        "READ_RUNNING_APPS",
        "READ_INPUT_DEVICE_LIST",
    ],
}


async def _receive(ws: ClientConnection, request_id: str) -> dict[str, Any]:
    while True:
        message = json.loads(await ws.recv())
        if not isinstance(message, dict):
            raise TVError("Malformed TV reply")
        if message.get("id") == request_id:
            if message.get("type") == "error":
                # Do not echo arbitrary response bodies into credential-bearing logs.
                raise TVError("TV rejected the request")
            return dict(message)


def _connect(ip: str) -> Any:
    # LG uses a self-signed certificate. Pair only on a trusted LAN.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return connect(
        f"wss://{ip}:3001",
        ssl=context,
        open_timeout=2,
        close_timeout=1,
        ping_interval=None,
        max_size=1_048_576,
        proxy=None,
    )


async def _register(ws: ClientConnection, key: str | None, allow_pair: bool) -> str:
    if not key and not allow_pair:
        raise PairingRequired("Pair the TV explicitly with the pair command first")
    payload: dict[str, Any] = {"manifest": MANIFEST, "pairingType": "PROMPT"}
    if key:
        payload["client-key"] = key
    await ws.send(json.dumps({"id": "register", "type": "register", "payload": payload}))
    while True:
        try:
            reply = await _receive(ws, "register")
        except TVError:
            raise PairingRequired("TV rejected pairing; run pair explicitly") from None
        if reply.get("type") == "registered":
            value = reply.get("payload", {}).get("client-key", key)
            if not isinstance(value, str) or not value:
                raise PairingRequired("TV did not supply a pairing key")
            return value
        if not allow_pair:
            raise PairingRequired("Pairing is no longer accepted; run pair explicitly")


async def pair(ip: str, key: str | None = None) -> str:
    async with asyncio.timeout(65), _connect(ip) as ws:
        return await _register(ws, key, True)


async def request(
    ip: str,
    key: str,
    uri: str,
    payload: dict[str, Any] | None = None,
    current: Callable[[], bool] = lambda: True,
) -> dict[str, Any]:
    async with asyncio.timeout(4), _connect(ip) as ws:
        await _register(ws, key, False)
        if not current():
            raise StaleRequest("USB ownership changed before the command was sent")
        await ws.send(
            json.dumps({"id": "command", "type": "request", "uri": uri, "payload": payload or {}})
        )
        reply = await _receive(ws, "command")
        body = reply.get("payload")
        if not isinstance(body, dict) or body.get("returnValue") is not True:
            raise TVError("TV did not accept the command")
        return dict(body)


async def switch(
    ip: str, key: str, target: str, current: Callable[[], bool] = lambda: True
) -> None:
    if target not in {f"HDMI_{i}" for i in range(1, 5)}:
        raise ValueError("Invalid HDMI input")
    await request(
        ip, key, "ssap://system.launcher/launch", {"id": "com.webos.app.hdmi" + target[-1]}, current
    )
