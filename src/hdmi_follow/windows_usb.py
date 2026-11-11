"""Present USB devnodes through Configuration Manager, without opening devices."""

import ctypes
import sys
from functools import lru_cache
from typing import Any

_ULONG = ctypes.c_uint32
_CR_SUCCESS = 0
_CR_BUFFER_SMALL = 0x1A
_FILTER_PRESENT_ENUMERATOR = 0x101
_MAX_ATTEMPTS = 3


class USBEnumerationError(RuntimeError):
    """A failed inventory must not be interpreted as a USB disconnect."""


@lru_cache(maxsize=1)
def _native_functions() -> tuple[Any, Any]:
    if sys.platform != "win32":
        raise RuntimeError("Windows USB instance detection is only available on Windows")
    # Resolve lazily so importing this module is safe on macOS and Linux.
    library = ctypes.WinDLL("cfgmgr32", use_last_error=False)
    size = library.CM_Get_Device_ID_List_SizeW
    size.argtypes = [ctypes.POINTER(_ULONG), ctypes.c_wchar_p, _ULONG]
    size.restype = _ULONG
    listing = library.CM_Get_Device_ID_ListW
    listing.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_wchar), _ULONG, _ULONG]
    listing.restype = _ULONG
    return size, listing


def present_instances() -> set[str]:
    """Return exact uppercase IDs of USB instances currently present.

    Device enumeration can grow between the size and retrieval calls. Retry only
    that documented race, bounded to three attempts. All other errors propagate.
    Buffer lengths are wide-character counts rather than byte counts.
    """
    size_function, list_function = _native_functions()
    for _ in range(_MAX_ATTEMPTS):
        length = _ULONG()
        result = size_function(ctypes.byref(length), "USB", _FILTER_PRESENT_ENUMERATOR)
        if result == _CR_BUFFER_SMALL:
            continue
        if result != _CR_SUCCESS:
            raise USBEnumerationError(f"USB inventory size failed (CONFIGRET 0x{result:08X})")
        if length.value < 1:
            raise USBEnumerationError("USB inventory returned an invalid MULTI_SZ buffer length")
        buffer = ctypes.create_unicode_buffer(length.value)
        result = list_function("USB", buffer, length.value, _FILTER_PRESENT_ENUMERATOR)
        if result == _CR_BUFFER_SMALL:
            continue
        if result != _CR_SUCCESS:
            raise USBEnumerationError(f"USB inventory failed (CONFIGRET 0x{result:08X})")
        data = "".join(buffer)
        if data.startswith("\0"):
            return set()
        end = data.find("\0\0")
        if end == -1:
            raise USBEnumerationError("USB inventory returned an unterminated MULTI_SZ list")
        return {instance.upper() for instance in data[:end].split("\0") if instance}
    raise USBEnumerationError("USB inventory kept changing during three enumeration attempts")


def is_hardware_id(selector: str) -> bool:
    return selector.count("\\") == 1


def matches_hub(selector: str, instance_id: str) -> bool:
    selector = selector.upper()
    instance_id = instance_id.upper()
    if is_hardware_id(selector):
        return instance_id.rpartition("\\")[0] == selector
    return instance_id == selector


def is_present(instance_id: str) -> bool:
    matches = sum(matches_hub(instance_id, device) for device in present_instances())
    if matches > 1:
        raise USBEnumerationError("Multiple matching USB hubs; configure a full instance ID")
    return matches == 1
