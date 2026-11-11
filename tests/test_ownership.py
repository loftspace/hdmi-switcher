from hdmi_follow.ownership import OwnershipTracker


def baseline(tracker: OwnershipTracker, present: bool) -> None:
    assert tracker.update(present, 0) is None
    assert tracker.update(present, 1) is None


def test_startup_is_silent_for_present_and_absent_devices() -> None:
    for present in (True, False):
        tracker = OwnershipTracker()
        baseline(tracker, present)
        assert tracker.update(present, 2) is None
        assert tracker.stable is present


def test_arrival_is_debounced_and_emitted_once() -> None:
    tracker = OwnershipTracker()
    baseline(tracker, False)
    assert tracker.update(True, 2) is None
    assert tracker.update(True, 2.9) is None
    event = tracker.update(True, 3)
    assert event is not None and event.present
    assert tracker.current(event)
    assert tracker.update(True, 4) is None


def test_short_disconnect_does_not_trigger_switch() -> None:
    tracker = OwnershipTracker()
    baseline(tracker, True)
    assert tracker.update(False, 2) is None
    assert tracker.update(True, 2.5) is None
    assert tracker.update(True, 3.5) is None
    assert tracker.stable


def test_losing_ownership_invalidates_command_before_debounce() -> None:
    tracker = OwnershipTracker()
    baseline(tracker, False)
    tracker.update(True, 2)
    event = tracker.update(True, 3)
    assert event is not None and tracker.current(event)
    assert tracker.update(False, 3.1) is None
    assert not tracker.current(event)


def test_suspend_gap_reestablishes_silent_baseline() -> None:
    tracker = OwnershipTracker()
    baseline(tracker, False)
    assert tracker.update(True, 20) is None
    assert tracker.update(True, 20.5) is None
    assert tracker.update(True, 21.5) is None
    assert tracker.stable
    assert tracker.update(False, 22) is None
    event = tracker.update(False, 23)
    assert event is not None and not event.present


def test_unknown_inventory_cancels_pending_and_does_not_mean_disconnect() -> None:
    tracker = OwnershipTracker()
    baseline(tracker, False)
    tracker.update(True, 2)
    event = tracker.update(True, 3)
    assert event is not None
    assert tracker.update(None, 3.1) is None
    assert not tracker.current(event)
    assert tracker.update(False, 4) is None
    assert tracker.update(False, 5) is None
    assert tracker.stable is False
