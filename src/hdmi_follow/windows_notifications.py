"""Native hub and power notifications; callbacks only fence and schedule work."""

import asyncio
import ctypes
import struct
import sys
import threading
from dataclasses import dataclass
from typing import Any

_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_MAX_DEVICE_ID_LEN = 200
_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_CM_CALLBACK = _CALLBACK(_DWORD, _HANDLE, _HANDLE, _DWORD, _HANDLE, _DWORD)
_POWER_CALLBACK = _CALLBACK(_DWORD, _HANDLE, _DWORD, _HANDLE)


class _FilterUnion(ctypes.Union):
    _fields_ = [
        ("guid", ctypes.c_ubyte * 16),
        ("handle", _HANDLE),
        ("instance_id", ctypes.c_uint16 * _MAX_DEVICE_ID_LEN),
    ]


class _CMFilter(ctypes.Structure):
    _fields_ = [
        ("size", _DWORD),
        ("flags", _DWORD),
        ("filter_type", _DWORD),
        ("reserved", _DWORD),
        ("data", _FilterUnion),
    ]


class _PowerSubscribe(ctypes.Structure):
    _fields_ = [("callback", _HANDLE), ("context", _HANDLE)]


@dataclass(frozen=True)
class Signal:
    reason: str
    revision: int


class NotificationError(RuntimeError):
    """Registration or cleanup failed; never silently substitute polling."""


@dataclass
class _API:
    cm_register: Any
    cm_unregister: Any
    power_register: Any
    power_unregister: Any


def _native_api() -> _API:
    if sys.platform != "win32":
        raise RuntimeError("Windows USB notifications are only available on Windows")
    cfg = ctypes.WinDLL("cfgmgr32", use_last_error=False)
    power = ctypes.WinDLL("powrprof", use_last_error=False)
    register = cfg.CM_Register_Notification
    register.argtypes = [ctypes.POINTER(_CMFilter), _HANDLE, _CM_CALLBACK, ctypes.POINTER(_HANDLE)]
    register.restype = _DWORD
    unregister = cfg.CM_Unregister_Notification
    unregister.argtypes = [_HANDLE]
    unregister.restype = _DWORD
    power_register = power.PowerRegisterSuspendResumeNotification
    power_register.argtypes = [_DWORD, _HANDLE, ctypes.POINTER(_HANDLE)]
    power_register.restype = _DWORD
    power_unregister = power.PowerUnregisterSuspendResumeNotification
    power_unregister.argtypes = [_HANDLE]
    power_unregister.restype = _DWORD
    return _API(register, unregister, power_register, power_unregister)


# Windows doesn't own Python references to callback trampolines. Retain the
# entire active owner until unregister succeeds, including across cleanup errors.
_RETAINED: set["HubNotifications"] = set()


class HubNotifications:
    def __init__(self, instance_id: str, loop: asyncio.AbstractEventLoop) -> None:
        encoded = instance_id.upper().encode("utf-16-le")
        if (
            not instance_id.upper().startswith("USB\\")
            or "\0" in instance_id
            or len(encoded) // 2 >= _MAX_DEVICE_ID_LEN
        ):
            raise ValueError("Expected one complete USB instance ID shorter than 200 characters")
        self._instance_id = instance_id.upper()
        self._loop = loop
        self._queue: asyncio.Queue[Signal] = asyncio.Queue()
        self._lock = threading.Lock()
        self._revision = 0
        self._power_revision = 0
        self._suspended = False
        self._closed = False
        self._cm_handle = _HANDLE()
        self._power_handle = _HANDLE()
        self._api = _native_api()
        self._device_callback = _CM_CALLBACK(self._on_device)
        self._power_callback = _POWER_CALLBACK(self._on_power)
        self._power_subscribe = _PowerSubscribe(ctypes.cast(self._power_callback, _HANDLE), None)
        self._filter = _CMFilter()
        self._filter.size = ctypes.sizeof(_CMFilter)
        self._filter.filter_type = 2
        for index, unit in enumerate(struct.unpack(f"<{len(encoded) // 2}H", encoded)):
            self._filter.data.instance_id[index] = unit
        try:
            result = self._api.cm_register(
                ctypes.byref(self._filter),
                None,
                self._device_callback,
                ctypes.byref(self._cm_handle),
            )
            if result != 0 or not self._cm_handle.value:
                raise NotificationError(f"Hub notification registration failed (0x{result:08X})")
            _RETAINED.add(self)
            result = self._api.power_register(
                2, ctypes.byref(self._power_subscribe), ctypes.byref(self._power_handle)
            )
            if result != 0 or not self._power_handle.value:
                raise NotificationError(f"Power notification registration failed (0x{result:08X})")
        except BaseException:
            self.close()
            raise

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def power_revision(self) -> int:
        with self._lock:
            return self._power_revision

    @property
    def suspended(self) -> bool:
        with self._lock:
            return self._suspended

    def _signal(self, reason: str, power_type: int | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            if power_type is not None:
                self._power_revision += 1
                self._suspended = power_type == 4
            self._revision += 1
            signal = Signal(reason, self._revision)
        # Revision advances before scheduling, fencing a task already on the loop.
        try:
            self._loop.call_soon_threadsafe(self._enqueue, signal)
        except RuntimeError:
            pass  # Loop was closed; the revision fence is still in force.

    def _enqueue(self, signal: Signal) -> None:
        with self._lock:
            if self._closed:
                return
        self._queue.put_nowait(signal)

    def _on_device(
        self, _handle: Any, _context: Any, action: int, event_data: Any, event_size: int
    ) -> int:
        try:
            if action not in (7, 8, 9) or not event_data or not 10 <= event_size <= 4096:
                return 0
            raw = ctypes.string_at(event_data, event_size)
            filter_type, reserved = struct.unpack_from("<II", raw)
            if filter_type != 2 or reserved != 0:
                return 0
            units = raw[8 : 8 + _MAX_DEVICE_ID_LEN * 2]
            if len(units) % 2:
                return 0
            decoded = units.decode("utf-16-le")
            if "\0" not in decoded:
                return 0
            if decoded.split("\0", 1)[0].upper() == self._instance_id:
                self._signal("device")
        except Exception:
            pass  # No Python exception may escape a native callback.
        return 0

    def _on_power(self, _context: Any, power_type: int, _setting: Any) -> int:
        try:
            if power_type in (4, 7, 18):
                self._signal("reset", power_type)
        except Exception:
            pass
        return 0

    async def wait(self) -> Signal:
        with self._lock:
            if self._closed:
                raise RuntimeError("Hub notifications are closed")
        return await self._queue.get()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        failures = []
        # Never unregister inside a native callback, where CM can deadlock.
        for handle, unregister in (
            (self._power_handle, self._api.power_unregister),
            (self._cm_handle, self._api.cm_unregister),
        ):
            if not handle.value:
                continue
            try:
                result = unregister(handle)
                if result == 0:
                    handle.value = None
                else:
                    failures.append(f"0x{result:08X}")
            except Exception as exc:
                failures.append(type(exc).__name__)
        if failures:
            _RETAINED.add(self)
            raise NotificationError("Notification cleanup failed: " + ", ".join(failures))
        _RETAINED.discard(self)
