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
