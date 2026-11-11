"""Per-profile Windows stop events without a polling or executor thread."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_INFINITE = 0xFFFFFFFF
_ONCE = 0x8
_MODIFY_STATE = 0x2
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass(frozen=True)
class _Functions:
    create: Any
    open: Any
    signal: Any
    register: Any
    unregister: Any
    close: Any
    callback_type: Any
    last_error: Any


@lru_cache(maxsize=1)
def _native_functions() -> _Functions:
    if sys.platform != "win32":
        raise RuntimeError("Native stop notifications require Windows")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    boolean = ctypes.c_int
    dword = ctypes.c_uint32
    callback_type = ctypes.WINFUNCTYPE(None, handle, ctypes.c_ubyte)
    create = library.CreateEventW
    create.argtypes = [handle, boolean, boolean, ctypes.c_wchar_p]
    create.restype = handle
    opening = library.OpenEventW
    opening.argtypes = [dword, boolean, ctypes.c_wchar_p]
    opening.restype = handle
    signal = library.SetEvent
    signal.argtypes = [handle]
    signal.restype = boolean
    register = library.RegisterWaitForSingleObject
    register.argtypes = [ctypes.POINTER(handle), handle, callback_type, handle, dword, dword]
    register.restype = boolean
    unregister = library.UnregisterWaitEx
    unregister.argtypes = [handle, handle]
    unregister.restype = boolean
    closing = library.CloseHandle
    closing.argtypes = [handle]
    closing.restype = boolean
    return _Functions(
        create, opening, signal, register, unregister, closing, callback_type, ctypes.get_last_error
    )


def _event_name(stop_file: Path) -> str:
    canonical = str(stop_file.expanduser().resolve()).casefold()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"Local\\HDMISwitcherStop-{digest}"


def _failure(functions: _Functions, operation: str) -> OSError:
    code = int(functions.last_error())
    return OSError(code, f"{operation} failed (Windows error {code})")


# Native waits do not own Python references. A callback's cycle with its owner
# is collectible, so retain owners globally until native cleanup fully succeeds.
_RETAINED: set[StopNotification] = set()


class StopNotification:
    """Bridge a native one-shot event wait into the owning asyncio event loop."""

    def __init__(self, stop_file: Path, loop: asyncio.AbstractEventLoop) -> None:
        self._functions = _native_functions()
        self._loop = loop
        self._signal = asyncio.Event()
        self._closing = False
        self._event: int | None = None
        self._wait: int | None = None
        self._callback: Any = None
        created = self._functions.create(None, True, False, _event_name(stop_file))
        if not created:
            raise _failure(self._functions, "CreateEventW")
        self._event = int(created)
        _RETAINED.add(self)
        try:
            self._callback = self._functions.callback_type(self._notified)
            wait_handle = ctypes.c_void_p()
            if not self._functions.register(
                ctypes.byref(wait_handle), self._event, self._callback, None, _INFINITE, _ONCE
            ):
                raise _failure(self._functions, "RegisterWaitForSingleObject")
            self._wait = wait_handle.value
            # CLI writes this marker before opening the native event. This one
            # check catches stop between the runner clearing it and registering.
            if stop_file.exists():
                self._signal.set()
        except BaseException:
            self.close()
            raise

    def _notified(self, context: int | None, timed_out: int) -> None:
        if self._closing or timed_out:
            return
        try:
            self._loop.call_soon_threadsafe(self._signal.set)
        except RuntimeError:
            # Shutdown may already have closed the loop. Never raise through C.
            pass

    async def wait(self) -> None:
        await self._signal.wait()

    def close(self) -> None:
        """Unregister synchronously before freeing the callback or event handle."""
        self._closing = True
        if self._wait is not None:
            if not self._functions.unregister(self._wait, _INVALID_HANDLE_VALUE):
                # Keep both callback and event alive if the native wait remains.
                raise _failure(self._functions, "UnregisterWaitEx")
            self._wait = None
        self._callback = None
        if self._event is not None:
            if not self._functions.close(self._event):
                raise _failure(self._functions, "CloseHandle")
            self._event = None
        _RETAINED.discard(self)


def signal_stop(stop_file: Path) -> bool:
    """Signal an existing helper; never create a persistent stop event ourselves."""
    functions = _native_functions()
    handle = functions.open(_MODIFY_STATE, False, _event_name(stop_file))
    if not handle:
        if functions.last_error() == 2:
            return False
        raise _failure(functions, "OpenEventW")
    try:
        if not functions.signal(handle):
            raise _failure(functions, "SetEvent")
    finally:
        if not functions.close(handle):
            raise _failure(functions, "CloseHandle")
    return True
