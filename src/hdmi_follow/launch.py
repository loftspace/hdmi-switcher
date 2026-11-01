"""Start a background helper without silently inheriting a host lifetime job."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from typing import BinaryIO

log = logging.getLogger(__name__)
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class BackgroundLaunchError(RuntimeError):
    """Windows refused an independent background process."""


def _in_job(handle: int | None = None) -> bool:
    """Check any job membership, including outer jobs in a nested hierarchy."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    member = wintypes.BOOL()
    process = kernel.GetCurrentProcess() if handle is None else handle
    if not kernel.IsProcessInJob(process, None, ctypes.byref(member)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(member.value)


def spawn_background(
    command: Sequence[str], output: BinaryIO, environment: Mapping[str, str]
) -> subprocess.Popen[bytes]:
    """Request a hidden independent helper; never retry a denied breakaway attached.

    A successful Windows breakaway can still retain an outer nested job. Report
    that uncertainty explicitly. An installed scheduled task is the durable
    alternative when the host prohibits breakaway.
    """
    windows = os.name == "nt"
    env = dict(environment)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if windows:
        try:
            log.info(
                "Background launch launcher_pid=%s parent_pid=%s launcher_in_job=%s",
                os.getpid(),
                os.getppid(),
                _in_job(),
            )
        except OSError:
            log.warning("Background launch parent job membership unavailable")
    try:
        child = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            creationflags=(CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB) if windows else 0,
            start_new_session=not windows,
            env=env,
        )
    except OSError as exc:
        if windows and getattr(exc, "winerror", None) == 5:
            log.warning("Background launch breakaway denied; no attached fallback started")
            raise BackgroundLaunchError(
                "Windows denied independent background launch. Start the installed "
                "login task from Task Scheduler instead; no attached fallback was started."
            ) from exc
        raise
    if windows:
        try:
            # Popen owns this process handle until its lifecycle completes.
            handle = int(child._handle)  # type: ignore[attr-defined]
            if _in_job(handle):
                log.warning(
                    "Background child pid=%s remains in a Windows job; host-independent "
                    "lifetime is not guaranteed. Prefer the installed scheduled task.",
                    child.pid,
                )
            else:
                log.info("Background child pid=%s is outside Windows jobs", child.pid)
        except OSError:
            log.warning("Background child pid=%s job membership unavailable", child.pid)
    return child
