import ctypes
from types import SimpleNamespace

from hdmi_follow import diagnostics


def test_native_health_snapshot_reports_memory_counters(monkeypatch):
    def read(pointer):
        status = ctypes.cast(pointer, ctypes.POINTER(diagnostics._MemoryStatus)).contents
        assert status.length == ctypes.sizeof(diagnostics._MemoryStatus)
        status.load = 95
        status.available_physical = 3072 * 1024 * 1024
        status.total_physical = 65536 * 1024 * 1024
        status.available_commit = 4096 * 1024 * 1024
        status.total_commit = 147456 * 1024 * 1024
        return True

    class Function:
        def __call__(self, pointer):
            return read(pointer)

    monkeypatch.setattr(diagnostics.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *a, **k: SimpleNamespace(GlobalMemoryStatusEx=Function()),
        raising=False,
    )
    result = diagnostics.health_summary()
    assert "ram_load_pct=95" in result
    assert "ram_free_mb=3072" in result
    assert "commit_free_mb=4096" in result
    assert "commit_limit_mb=147456" in result


def test_unavailable_native_counters_do_not_interrupt_helper(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(diagnostics.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", fail, raising=False)
    assert diagnostics.health_summary().endswith("memory=unavailable")


def test_health_snapshot_is_safe_on_other_platforms(monkeypatch):
    monkeypatch.setattr(diagnostics.sys, "platform", "darwin")
    assert diagnostics.health_summary().endswith("memory=unavailable")


def test_stop_summary_reports_requester_without_reflecting_unknown_data(tmp_path):
    import json

    path = tmp_path / "stop"
    path.write_text(
        json.dumps(
            {
                "reason": "cli_stop",
                "requester_pid": 123,
                "requested_at": "2026-11-01T15:00:00+00:00",
                "secret": "private-key",
            }
        ),
        encoding="utf-8",
    )
    result = diagnostics.stop_summary(path)
    assert "requester_pid=123 reason=cli_stop" in result
    assert "private-key" not in result
    path.write_text('{"reason":"private-key"}', encoding="utf-8")
    assert diagnostics.stop_summary(path) == "requester_pid=unknown reason=unknown"


def test_fatal_trace_file_is_bounded_and_handler_is_closed(tmp_path, monkeypatch):
    enabled = []
    disabled = []
    monkeypatch.setattr(diagnostics.faulthandler, "enable", lambda **kwargs: enabled.append(kwargs))
    monkeypatch.setattr(diagnostics.faulthandler, "disable", lambda: disabled.append(True))
    path = tmp_path / "crash.log"
    path.write_text("x" * 1_000_001, encoding="utf-8")
    stream = diagnostics.enable_fatal_traces(path)
    assert stream is not None
    assert path.with_name("crash.log.1").stat().st_size == 1_000_001
    assert "Fatal trace run version=" in path.read_text(encoding="utf-8")
    assert enabled[0]["file"] is stream
    assert enabled[0]["all_threads"] is True
    diagnostics.close_fatal_traces(stream)
    assert stream.closed and disabled == [True]
