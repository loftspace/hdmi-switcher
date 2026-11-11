from xml.etree import ElementTree as ET

from hdmi_follow import autostart


def test_owned_windows_task_restarts_failures_with_bounded_backoff():
    root = ET.fromstring(autostart._windows_xml(["helper.exe", "run", "--config", "profile.json"]))
    namespace = {"t": autostart.TASK_NS}
    assert root.findtext("t:Settings/t:RestartOnFailure/t:Interval", namespaces=namespace) == "PT1M"
    assert root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=namespace) == "3"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=namespace) == "PT0S"
