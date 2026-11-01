"""Small local health snapshots, without commands, credentials or input data."""

import ctypes
import faulthandler
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from hdmi_follow import __version__, lifecycle


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
    lifecycle.heartbeat_current_run()
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


def stop_summary(path: Path) -> str:
    """Describe a stop request without reflecting untrusted file contents."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise ValueError("Oversized request")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid request")
        pid = data.get("requester_pid")
        reason = data.get("reason")
        requested = data.get("requested_at")
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise ValueError("Invalid requester")
        if reason not in {"cli_stop", "uninstall"} or not isinstance(requested, str):
            raise ValueError("Invalid reason")
        stamp = datetime.fromisoformat(requested).isoformat()
        return f"requester_pid={pid} reason={reason} requested_at={stamp}"
    except Exception:
        return "requester_pid=unknown reason=unknown"


def enable_fatal_traces(path: Path) -> TextIO | None:
    """Arm native/Python fatal stack traces. Forced termination cannot call this."""
    stream: TextIO | None = None
    try:
        if path.exists() and path.stat().st_size >= 1_000_000:
            path.replace(path.with_name(path.name + ".1"))
        stream = path.open("a", encoding="utf-8")
        stream.write(
            f"\nFatal trace run version={__version__} pid={os.getpid()} "
            f"started_at={datetime.now(UTC).isoformat()}\n"
        )
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
        return stream
    except Exception as exc:
        if stream is not None:
            stream.close()
        logging.getLogger(__name__).warning(
            "Fatal traces unavailable error_type=%s", type(exc).__name__
        )
        return None


def close_fatal_traces(stream: TextIO | None) -> None:
    if stream is not None:
        faulthandler.disable()
        stream.close()
