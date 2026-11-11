"""Read bounded Windows evidence after an unclean exit; correlation is not causation."""

import asyncio
import logging
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import PureWindowsPath
from xml.etree import ElementTree as ET

LOG = logging.getLogger(__name__)
NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
HOST = "CodexSandboxService.OpenAI.Codex"
APPS = {"hdmi-follow.exe", "hdmi-follow-background.exe"}


def parse_events(xml: str) -> list[dict[str, str | int]]:
    """Keep only whitelisted facts, never arbitrary event messages or command lines."""
    if len(xml) > 2_000_000:
        raise ValueError("Event response exceeds limit")
    events: list[dict[str, str | int]] = []
    for event in ET.fromstring(xml).findall("e:Event", NS)[:30]:
        system = event.find("e:System", NS)
        if system is None:
            continue
        provider_node = system.find("e:Provider", NS)
        timestamp_node = system.find("e:TimeCreated", NS)
        if provider_node is None or timestamp_node is None:
            continue
        provider = provider_node.get("Name", "")
        stamp = timestamp_node.get("SystemTime", "")
        try:
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC).isoformat()
            event_id = int(system.findtext("e:EventID", "0", NS))
        except ValueError:
            continue
        data = {
            node.get("Name", ""): node.text or ""
            for node in event.findall("e:EventData/e:Data", NS)
        }
        facts: dict[str, str | int] = {"time": stamp, "provider": provider, "event_id": event_id}
        if provider == HOST and event_id in (1000, 1001, 1002):
            body = data.get("", "")
            if body == "The Codex sandbox service was asked to stop.":
                facts["kind"] = "host_service_stop"
            elif body == "The Codex sandbox service is running.":
                facts["kind"] = "host_service_start"
            elif body == "The Codex sandbox service has stopped.":
                facts["kind"] = "host_service_stopped"
            else:
                continue
        elif provider in {"Application Error", "Windows Error Reporting"}:
            app = PureWindowsPath(data.get("AppName", data.get("P1", ""))).name.lower()
            if app not in APPS or event_id not in (1000, 1001):
                continue
            facts.update(kind="helper_crash_report", app=app)
            code = data.get("ExceptionCode", "")
            if re.fullmatch(r"(?:0x)?[0-9a-fA-F]{8}", code):
                facts["exception_code"] = code.lower()
            process = data.get("ProcessId", "")
            try:
                facts["process_id"] = int(process, 16 if process.startswith("0x") else 10)
            except ValueError:
                pass
        elif provider == "Microsoft-Windows-Resource-Exhaustion-Detector" and event_id == 2004:
            facts["kind"] = "system_resource_exhaustion"
        else:
            continue
        events.append(facts)
    return events


def recent_events(since: str) -> list[dict[str, str | int]]:
    if sys.platform != "win32":
        return []
    # Parse first so a state-file value can never inject an XPath expression.
    lower = datetime.fromisoformat(since.replace("Z", "+00:00")) - timedelta(seconds=30)
    if lower.tzinfo is None:
        raise ValueError("Expected UTC timestamp")
    stamp = lower.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    facts: list[dict[str, str | int]] = []
    for channel, providers in (
        (
            "Application",
            f"Provider[@Name='{HOST}'] or Provider[@Name='Application Error'] "
            "or Provider[@Name='Windows Error Reporting']",
        ),
        ("System", "Provider[@Name='Microsoft-Windows-Resource-Exhaustion-Detector']"),
    ):
        query = (
            f"*[System[({providers}) and "
            "(EventID=1000 or EventID=1001 or EventID=1002 or EventID=2004) "
            f"and TimeCreated[@SystemTime >= '{stamp}']]]"
        )
        try:
            result = subprocess.run(
                [
                    "wevtutil",
                    "qe",
                    channel,
                    f"/q:{query}",
                    "/f:xml",
                    "/e:Events",
                    "/rd:true",
                    "/c:30",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=True,
                creationflags=0x08000000,
            )
            facts.extend(parse_events(result.stdout))
        except (subprocess.SubprocessError, OSError, ET.ParseError, ValueError) as exc:
            LOG.warning(
                "Windows evidence channel=%s unavailable error_type=%s", channel, type(exc).__name__
            )
    return facts


async def report_unclean_exit(previous: dict[str, object]) -> None:
    since = previous.get("updated_at")
    if not isinstance(since, str):
        return
    try:
        facts = await asyncio.to_thread(recent_events, since)
        if not facts:
            LOG.warning("Unclean exit evidence: no matching Windows records; cause remains unknown")
        for fact in facts:
            LOG.warning("Related Windows event (correlation only): %s", fact)
    except Exception as exc:
        LOG.warning("Unclean exit evidence unavailable error_type=%s", type(exc).__name__)
