"""Persistent, bounded evidence of the previous helper lifetime."""

import ctypes
import json
import logging
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)
_MAX_BYTES = 4096
_REASONS = frozenset({"cli_stop", "interrupt", "error", "normal"})
_current: "RunRecord | None" = None


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
        return offset is not None and offset.total_seconds() == 0
    except (ValueError, AttributeError):
        return False


def _validated(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid record")
    result: dict[str, object] = {}
    if value.get("status") not in ("running", "clean"):
        raise ValueError("invalid status")
    result["status"] = value["status"]
    for key in ("pid", "ppid"):
        item = value.get(key)
        if type(item) is not int or not 0 <= item <= 0xFFFFFFFF:
            raise ValueError("invalid process")
        result[key] = item
    version = value.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", version):
        raise ValueError("invalid version")
    result["version"] = version
    for key in ("started_at", "updated_at"):
        if not _timestamp(value.get(key)):
            raise ValueError("invalid timestamp")
        result[key] = value[key]
    if result["status"] == "clean":
        if value.get("exit_reason") not in _REASONS:
            raise ValueError("invalid reason")
        result["exit_reason"] = value["exit_reason"]
    if type(value.get("in_job")) is bool:
        result["in_job"] = value["in_job"]
    return result


def _in_job() -> bool | None:
    if sys.platform != "win32":
        return None
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        current = kernel.GetCurrentProcess
        current.argtypes = []
        current.restype = ctypes.c_void_p
        check = kernel.IsProcessInJob
        check.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        check.restype = ctypes.c_int
        result = ctypes.c_int()
        if check(current(), None, ctypes.byref(result)):
            return bool(result.value)
    except Exception:
        pass
    return None


class RunRecord:
    """Record clean completion; a surviving running record has an unknown exit cause."""

    def __init__(self, path: Path, version: str) -> None:
        self.path = Path(path)
        self.version = version if re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", version) else "unknown"
        self.previous: dict[str, object] | None = None
        self._record: dict[str, object] | None = None
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError("oversized record")
            self.previous = _validated(json.loads(raw))
        except FileNotFoundError:
            pass
        except Exception as exc:
            _LOG.warning("Previous run record unavailable (%s)", type(exc).__name__)

    def _write(self, *, durable: bool = True) -> None:
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temporary = stream.name
                json.dump(self._record, stream, separators=(",", ":"))
                stream.flush()
                if durable:
                    os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception as exc:
            _LOG.warning("Run record write failed (%s)", type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def start(self) -> None:
        if self.previous is not None and self.previous["status"] == "running":
            _LOG.warning(
                "Previous run ended without a clean exit: pid=%s parent_pid=%s in_job=%s "
                "version=%s last_seen=%s cause=unknown",
                self.previous["pid"],
                self.previous["ppid"],
                self.previous.get("in_job"),
                self.previous["version"],
                self.previous["updated_at"],
            )
        now = _utc()
        self._record = dict(
            status="running",
            pid=os.getpid(),
            ppid=os.getppid(),
            version=self.version,
            started_at=now,
            updated_at=now,
        )
        in_job = _in_job()
        if in_job is not None:
            self._record["in_job"] = in_job
        _LOG.info(
            "Run lifetime pid=%s parent_pid=%s in_job=%s",
            self._record["pid"],
            self._record["ppid"],
            in_job,
        )
        self._write()

    def heartbeat(self) -> None:
        if self._record is not None and self._record["status"] == "running":
            self._record["updated_at"] = _utc()
            self._write(durable=False)

    def finish(self, reason: str) -> None:
        if self._record is not None:
            self._record.update(
                status="clean",
                exit_reason=reason if reason in _REASONS else "error",
                updated_at=_utc(),
            )
            self._write()


def set_current(record: RunRecord | None) -> None:
    global _current
    _current = record


def heartbeat_current_run() -> None:
    if _current is not None:
        _current.heartbeat()
