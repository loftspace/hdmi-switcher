"""Current-user DPAPI protection for small local secrets on Windows."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# Extra entropy binds blobs to this purpose; it is not a secret.
_ENTROPY = b"HDMI Follow pairing key v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _transform(name: str, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is available on Windows only")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = getattr(crypt32, name)
    function.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # The buffers stay referenced until the call returns.
    source, _source_buffer = _blob(data)
    entropy, _entropy_buffer = _blob(_ENTROPY)
    output = _Blob()
    ok = function(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    if not ok:
        # Only the numeric Windows error is reported, never the data.
        raise OSError(ctypes.get_last_error(), f"{name} failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    """Encrypt data so only the current Windows user on this machine can read it."""
    return _transform("CryptProtectData", data)


def unprotect(blob: bytes) -> bytes:
    """Decrypt a blob from protect(); raises OSError for another user or machine."""
    return _transform("CryptUnprotectData", blob)
