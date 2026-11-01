"""Small local health snapshots, without commands, credentials or input data."""

import ctypes
import os
import sys


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("total_physical", ctypes.c_uint64),
        ("available_physical", ctypes.c_uint64),
        ("total_commit", ctypes.c_uint64),
        ("available_commit", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("available_virtual", ctypes.c_uint64),
        ("extended_virtual", ctypes.c_uint64),
    ]


def health_summary() -> str:
    """Read native counters only when requested; never fail the ownership loop."""
    prefix = f"pid={os.getpid()}"
    if sys.platform != "win32":
        return prefix + " memory=unavailable"
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        read = kernel.GlobalMemoryStatusEx
        read.argtypes = [ctypes.POINTER(_MemoryStatus)]
        read.restype = ctypes.c_int
        status = _MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not read(ctypes.byref(status)):
            return prefix + " memory=unavailable"
        mib = 1024 * 1024
        return (
            f"{prefix} ram_load_pct={status.load}"
            f" ram_free_mb={status.available_physical // mib}"
            f" ram_total_mb={status.total_physical // mib}"
            f" commit_free_mb={status.available_commit // mib}"
            f" commit_limit_mb={status.total_commit // mib}"
        )
    except Exception:
        return prefix + " memory=unavailable"
