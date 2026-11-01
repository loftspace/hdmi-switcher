import logging
from unittest.mock import Mock

import pytest

from hdmi_follow import exit_evidence


def event(provider, number, data):
    return (
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
        f'<System><Provider Name="{provider}"/><EventID>{number}</EventID>'
        '<TimeCreated SystemTime="2026-11-01T13:56:11Z"/></System>'
        f"<EventData>{data}</EventData></Event>"
    )


def test_host_stop_is_safe_evidence_not_a_claim_of_cause():
    xml = (
        "<Events>"
        + event(
            exit_evidence.HOST, 1001, "<Data>The Codex sandbox service was asked to stop.</Data>"
        )
        + "</Events>"
    )
    assert exit_evidence.parse_events(xml)[0]["kind"] == "host_service_stop"


def test_crash_report_discards_raw_bodies_and_other_applications():
    xml = (
        "<Events>"
        + event(
            "Application Error",
            1000,
            '<Data Name="AppName">hdmi-follow.exe</Data>'
            '<Data Name="ExceptionCode">c0000005</Data>'
            '<Data Name="ProcessId">0x123</Data>'
            '<Data Name="RawBody">secret-key</Data>',
        )
        + event("Application Error", 1000, '<Data Name="AppName">private-app.exe</Data>')
        + "</Events>"
    )
    facts = exit_evidence.parse_events(xml)
    assert len(facts) == 1
    assert facts[0]["exception_code"] == "c0000005"
    assert facts[0]["process_id"] == 0x123
    assert "secret-key" not in str(facts)
    assert "private-app.exe" not in str(facts)


async def test_windows_evidence_logs_correlation_without_raw_error(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="hdmi_follow")
    monkeypatch.setattr(exit_evidence, "recent_events", Mock(side_effect=OSError("secret-frame")))
    await exit_evidence.report_unclean_exit({"updated_at": "2026-11-01T13:55:58+00:00"})
    assert "error_type=OSError" in caplog.text
    assert "secret-frame" not in caplog.text


def test_bad_state_timestamp_cannot_inject_an_event_query(monkeypatch):
    monkeypatch.setattr(exit_evidence.sys, "platform", "win32")
    query = Mock()
    monkeypatch.setattr(exit_evidence.subprocess, "run", query)
    with pytest.raises(ValueError):
        exit_evidence.recent_events("'] malicious-secret")
    query.assert_not_called()


def test_event_queries_are_bounded_hidden_and_shell_free(monkeypatch):
    monkeypatch.setattr(exit_evidence.sys, "platform", "win32")
    query = Mock(return_value=Mock(stdout="<Events/>"))
    monkeypatch.setattr(exit_evidence.subprocess, "run", query)
    assert exit_evidence.recent_events("2026-11-01T13:55:58+00:00") == []
    assert query.call_count == 2
    for call in query.call_args_list:
        assert call.args[0][0] == "wevtutil"
        assert "/c:30" in call.args[0]
        assert any("2026-11-01T13:55:28.000Z" in argument for argument in call.args[0])
        assert call.kwargs["timeout"] == 3
        assert call.kwargs["creationflags"] == 0x08000000
        assert "shell" not in call.kwargs


def test_application_query_failure_does_not_lose_system_evidence(monkeypatch):
    monkeypatch.setattr(exit_evidence.sys, "platform", "win32")
    xml = (
        "<Events>"
        + event(
            "Microsoft-Windows-Resource-Exhaustion-Detector",
            2004,
            "<Data>private-memory-body</Data>",
        )
        + "</Events>"
    )
    query = Mock(side_effect=[OSError("private-error"), Mock(stdout=xml)])
    monkeypatch.setattr(exit_evidence.subprocess, "run", query)
    facts = exit_evidence.recent_events("2026-11-01T13:55:58+00:00")
    assert len(facts) == 1 and facts[0]["kind"] == "system_resource_exhaustion"
    assert "private" not in str(facts)
