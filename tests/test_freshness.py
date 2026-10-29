from hdmi_follow import runner
from hdmi_follow.config import Config, Device
from hdmi_follow.devices import SeenDevice
from hdmi_follow.ownership import OwnershipTracker


def event():
    tracker = OwnershipTracker(debounce=1)
    tracker.update(True, 0)
    tracker.update(True, 1)
    tracker.update(False, 2)
    transition = tracker.update(False, 3)
    assert transition
    return tracker, transition


def test_wake_before_next_poll_invalidates_pending_switch(monkeypatch):
    tracker, transition = event()
    monkeypatch.setattr(runner.time, "time", lambda: 300)
    monkeypatch.setattr(runner, "enumerate_devices", lambda: set())
    assert not runner.still_current(
        tracker, transition, Config("192.168.0.20", "HDMI_1", (Device(1, 2),))
    )


def test_usb_rechecked_immediately_before_network_command(monkeypatch):
    tracker, transition = event()
    monkeypatch.setattr(runner.time, "time", lambda: 3.1)
    monkeypatch.setattr(runner, "enumerate_devices", lambda: {SeenDevice(1, 2, "", "Keyboard")})
    assert not runner.still_current(
        tracker, transition, Config("192.168.0.20", "HDMI_1", (Device(1, 2),))
    )


def test_backward_wall_clock_jump_rebaselines_silently():
    tracker, transition = event()
    assert tracker.update(True, -50) is None
    assert not tracker.current(transition)
    assert tracker.update(True, -49) is None
    assert tracker.update(True, -48) is None
    assert tracker.stable is True
