import asyncio
import ctypes
import gc
import sys
import weakref
from pathlib import Path
from unittest.mock import Mock

import pytest

from hdmi_follow import windows_stop


class Native:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.error = 5
        self.fail_create = False
        self.fail_register = False
        self.fail_unregister = False
        self.fail_signal = False
        self.fail_open = False
        self.owner = None

    def create(self, security, manual_reset, initial, name):
        self.calls.append(("create", security, manual_reset, initial, name))
        return None if self.fail_create else 0x100000042

    def opening(self, rights, inherit, name):
        self.calls.append(("open", rights, inherit, name))
        return None if self.fail_open else 0x100000084

    def register(self, result, event, callback, context, timeout, flags):
        self.calls.append(("register", event, context, timeout, flags))
        self.callback = callback
        if self.fail_register:
            return False
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x100000777
        return True

    def unregister(self, wait, completion):
        self.calls.append(("unregister", wait, completion))
        if self.owner is not None:
            assert self.owner._callback is not None
            assert self.owner._event is not None
        return not self.fail_unregister

    def signal(self, event):
        self.calls.append(("signal", event))
        return not self.fail_signal

    def close(self, handle):
        self.calls.append(("close", handle))
        return True

    def install(self, monkeypatch):
        functions = windows_stop._Functions(
            self.create,
            self.opening,
            self.signal,
            self.register,
            self.unregister,
            self.close,
            lambda callback: callback,
            lambda: self.error,
        )
        monkeypatch.setattr(windows_stop, "_native_functions", lambda: functions)


def test_manual_reset_event_and_one_shot_wait_flags_and_cleanup(monkeypatch, tmp_path):
    native = Native()
    native.install(monkeypatch)
    notification = windows_stop.StopNotification(tmp_path / "stop", Mock())
    native.owner = notification
    assert native.calls[0][:4] == ("create", None, True, False)
    assert native.calls[0][4].startswith("Local\\HDMISwitcherStop-")
    assert native.calls[1] == ("register", 0x100000042, None, 0xFFFFFFFF, 8)
    notification.close()
    assert native.calls[-2:] == [
        ("unregister", 0x100000777, ctypes.c_void_p(-1).value),
        ("close", 0x100000042),
    ]
    assert notification._callback is None
    notification.close()
    assert len(native.calls) == 4


async def test_native_callback_handoff_uses_loop_threadsafe_queue(monkeypatch, tmp_path):
    native = Native()
    native.install(monkeypatch)
    loop = asyncio.get_running_loop()
    notification = windows_stop.StopNotification(tmp_path / "stop", loop)
    loop_spy = Mock(wraps=loop.call_soon_threadsafe)
    monkeypatch.setattr(loop, "call_soon_threadsafe", loop_spy)
    native.callback(None, 0)
    await asyncio.wait_for(notification.wait(), 1)
    loop_spy.assert_called_once_with(notification._signal.set)
    notification.close()
    native.callback(None, 0)
    assert loop_spy.call_count == 1


async def test_existing_marker_is_seen_once_without_resetting_native_event(monkeypatch, tmp_path):
    native = Native()
    native.install(monkeypatch)
    stop_file = tmp_path / "stop"
    stop_file.touch()
    notification = windows_stop.StopNotification(stop_file, asyncio.get_running_loop())
    await asyncio.wait_for(notification.wait(), 0.1)
    notification.close()
    assert [call[0] for call in native.calls] == ["create", "register", "unregister", "close"]


def test_registration_failure_closes_partial_event(monkeypatch, tmp_path):
    native = Native()
    native.fail_register = True
    native.install(monkeypatch)
    with pytest.raises(OSError, match="RegisterWaitForSingleObject"):
        windows_stop.StopNotification(tmp_path / "stop", Mock())
    assert native.calls[-1] == ("close", 0x100000042)
    assert not any(call[0] == "unregister" for call in native.calls)


def test_create_failure_does_not_register_or_close_invalid_handle(monkeypatch, tmp_path):
    native = Native()
    native.fail_create = True
    native.install(monkeypatch)
    with pytest.raises(OSError, match="CreateEventW"):
        windows_stop.StopNotification(tmp_path / "stop", Mock())
    assert len(native.calls) == 1


def test_unregister_failure_retains_callback_and_event_until_success(monkeypatch, tmp_path):
    native = Native()
    native.install(monkeypatch)
    notification = windows_stop.StopNotification(tmp_path / "stop", Mock())
    native.fail_unregister = True
    with pytest.raises(OSError, match="UnregisterWaitEx"):
        notification.close()
    assert notification._callback is not None
    assert notification._event == 0x100000042
    assert not any(call[0] == "close" for call in native.calls)
    native.fail_unregister = False
    notification.close()


def test_signal_uses_modify_only_noninheritable_handle_and_closes(monkeypatch, tmp_path):
    native = Native()
    native.install(monkeypatch)
    path = tmp_path / "stop"
    assert windows_stop.signal_stop(path)
    assert native.calls == [
        ("open", 2, False, windows_stop._event_name(path)),
        ("signal", 0x100000084),
        ("close", 0x100000084),
    ]


def test_signal_returns_false_only_for_missing_event(monkeypatch, tmp_path):
    native = Native()
    native.fail_open = True
    native.error = 2
    native.install(monkeypatch)
    assert windows_stop.signal_stop(tmp_path / "stop") is False
    assert len(native.calls) == 1
    native.error = 5
    with pytest.raises(OSError, match="OpenEventW.*5"):
        windows_stop.signal_stop(tmp_path / "stop")


def test_set_event_failure_still_closes_open_handle(monkeypatch, tmp_path):
    native = Native()
    native.fail_signal = True
    native.install(monkeypatch)
    with pytest.raises(OSError, match="SetEvent"):
        windows_stop.signal_stop(tmp_path / "stop")
    assert native.calls[-1] == ("close", 0x100000084)


def test_event_name_resolves_relative_components_and_casefolds(tmp_path):
    path = tmp_path / "Config" / "stop"
    assert windows_stop._event_name(path) == windows_stop._event_name(
        tmp_path / "Config" / "child" / ".." / "stop"
    )
    assert windows_stop._event_name(path) == windows_stop._event_name(Path(str(path).upper()))
    assert windows_stop._event_name(path) != windows_stop._event_name(path.with_name("other"))
    assert "Config" not in windows_stop._event_name(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows event integration")
async def test_live_windows_event_signal_and_unregister(tmp_path):
    stop_file = tmp_path / "unique-native-stop"
    notification = windows_stop.StopNotification(stop_file, asyncio.get_running_loop())
    try:
        assert windows_stop.signal_stop(stop_file)
        await asyncio.wait_for(notification.wait(), 1)
    finally:
        notification.close()
    assert windows_stop.signal_stop(stop_file) is False


def test_failed_unregister_retains_owner_after_references_are_dropped(monkeypatch, tmp_path):
    native = Native()
    native.install(monkeypatch)
    notification = windows_stop.StopNotification(tmp_path / "stop", Mock())
    reference = weakref.ref(notification)
    native.fail_unregister = True
    with pytest.raises(OSError, match="UnregisterWaitEx"):
        notification.close()
    # Our fake native API must not accidentally serve as a Python lifetime root.
    native.callback = None
    del notification
    gc.collect()
    retained = reference()
    assert retained is not None
    assert retained in windows_stop._RETAINED
    native.fail_unregister = False
    retained.close()
    del retained
    gc.collect()
    assert reference() is None


def test_partial_initialization_cleanup_failure_retains_native_callback(monkeypatch, tmp_path):
    native = Native()
    native.fail_unregister = True
    native.install(monkeypatch)
    before = windows_stop._RETAINED.copy()

    def inaccessible_marker(path):
        raise PermissionError("cannot inspect marker")

    monkeypatch.setattr(Path, "exists", inaccessible_marker)
    with pytest.raises(OSError, match="UnregisterWaitEx"):
        windows_stop.StopNotification(tmp_path / "stop", Mock())
    retained = (windows_stop._RETAINED - before).pop()
    reference = weakref.ref(retained)
    assert retained._callback is not None
    native.callback = None
    del retained
    gc.collect()
    assert reference() is not None
    native.fail_unregister = False
    reference().close()
    gc.collect()
    assert reference() is None
