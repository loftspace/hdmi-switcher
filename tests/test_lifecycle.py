import json
import logging

import pytest

from hdmi_follow import lifecycle


def test_unclean_previous_survives_new_instance(tmp_path, caplog, monkeypatch):
    path = tmp_path / "profile" / "run-state.json"
    monkeypatch.setattr(lifecycle, "_utc", lambda: "2026-11-01T10:00:00+00:00")
    first = lifecycle.RunRecord(path, "0.1.4")
    first.start()
    monkeypatch.setattr(lifecycle, "_utc", lambda: "2026-11-01T10:01:00+00:00")
    first.heartbeat()
    second = lifecycle.RunRecord(path, "0.1.4")
    assert second.previous["updated_at"] == "2026-11-01T10:01:00+00:00"
    with caplog.at_level(logging.WARNING):
        second.start()
    assert "cause=unknown" in caplog.text
    assert "last_seen=2026-11-01T10:01:00+00:00" in caplog.text
    assert not list(path.parent.glob("tmp*"))


@pytest.mark.parametrize("reason", ["cli_stop", "interrupt", "error", "normal"])
def test_clean_exit_no_abnormal_warning(tmp_path, caplog, reason):
    path = tmp_path / "run-state.json"
    first = lifecycle.RunRecord(path, "0.1.4")
    first.start()
    first.finish(reason)
    first.heartbeat()
    second = lifecycle.RunRecord(path, "0.1.4")
    assert second.previous["status"] == "clean"
    assert second.previous["exit_reason"] == reason
    with caplog.at_level(logging.WARNING):
        second.start()
    assert "without a clean exit" not in caplog.text


@pytest.mark.parametrize("contents", [b'{"secret":"DO_NOT_LOG"', b"x" * 4097, b"[]"])
def test_bad_record_bounded_and_no_body_logged(tmp_path, caplog, contents):
    path = tmp_path / "run-state.json"
    path.write_bytes(contents)
    record = lifecycle.RunRecord(path, "0.1.4")
    assert record.previous is None
    assert "unavailable" in caplog.text
    assert "DO_NOT_LOG" not in caplog.text


def test_unknown_fields_removed_and_unknown_reason_sanitized(tmp_path):
    path = tmp_path / "run-state.json"
    record = lifecycle.RunRecord(path, "0.1.4")
    record.start()
    data = json.loads(path.read_text())
    data["client_key"] = "secret"
    path.write_text(json.dumps(data))
    loaded = lifecycle.RunRecord(path, "0.1.4")
    assert "client_key" not in loaded.previous
    loaded.start()
    loaded.finish("exception with secret")
    assert json.loads(path.read_text())["exit_reason"] == "error"
    assert "secret" not in path.read_text()


def test_write_failure_nonfatal_preserves_previous_file(tmp_path, monkeypatch, caplog):
    path = tmp_path / "run-state.json"
    record = lifecycle.RunRecord(path, "0.1.4")
    record.start()
    original = path.read_bytes()

    def failed_replace(*args):
        raise OSError("DO_NOT_LOG")

    monkeypatch.setattr(lifecycle.os, "replace", failed_replace)
    record.heartbeat()
    record.finish("normal")
    assert path.read_bytes() == original
    assert "write failed (OSError)" in caplog.text
    assert "DO_NOT_LOG" not in caplog.text
    assert list(tmp_path.iterdir()) == [path]


def test_global_heartbeat_hook(tmp_path, monkeypatch):
    record = lifecycle.RunRecord(tmp_path / "run-state.json", "0.1.4")
    calls = []
    monkeypatch.setattr(record, "heartbeat", lambda: calls.append(True))
    try:
        lifecycle.set_current(record)
        lifecycle.heartbeat_current_run()
        lifecycle.set_current(None)
        lifecycle.heartbeat_current_run()
        assert calls == [True]
    finally:
        lifecycle.set_current(None)


def test_invalid_required_metadata_rejected(tmp_path):
    path = tmp_path / "run-state.json"
    lifecycle.RunRecord(path, "0.1.4").start()
    data = json.loads(path.read_text())
    data["pid"] = True
    path.write_text(json.dumps(data))
    assert lifecycle.RunRecord(path, "0.1.4").previous is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "secret"),
        ("version", "secret token with spaces"),
        ("started_at", "yesterday"),
        ("updated_at", "2026-11-01T10:00:00"),
        ("ppid", -1),
    ],
)
def test_invalid_fields_rejected_without_logging_values(tmp_path, caplog, field, value):
    path = tmp_path / "run-state.json"
    lifecycle.RunRecord(path, "0.1.4").start()
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    assert lifecycle.RunRecord(path, "0.1.4").previous is None
    assert "secret" not in caplog.text


def test_parent_directory_creation_failure_is_nonfatal(tmp_path, caplog):
    parent = tmp_path / "file"
    parent.write_text("unchanged")
    record = lifecycle.RunRecord(parent / "run-state.json", "0.1.4")
    record.start()
    record.heartbeat()
    record.finish("normal")
    assert parent.read_text() == "unchanged"
    assert "Run record write failed" in caplog.text


def test_non_windows_job_probe_is_safe(monkeypatch):
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    assert lifecycle._in_job() is None
