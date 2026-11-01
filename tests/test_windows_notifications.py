import asyncio
import ctypes
import struct
import threading

import pytest

from hdmi_follow import windows_notifications as native

HUB = r"USB\VID_0557&PID_2414\A&2648020B&0&9"


class DeferredLoop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append((callback, args))

    def flush(self):
        for callback, args in self.calls:
            callback(*args)
        self.calls.clear()


class FakeAPI:
    def __init__(self):
        self.cm_error = 0
        self.power_error = 0
        self.cm_cleanup_error = 0
        self.power_cleanup_error = 0
        self.cm_calls = 0
        self.power_calls = 0
        self.removed = []
        self.device_callback = None
        self.power_callback = None

    def cm_register(self, filter_pointer, context, callback, output):
        self.cm_calls += 1
        info = ctypes.cast(filter_pointer, ctypes.POINTER(native._CMFilter)).contents
        assert info.size == ctypes.sizeof(native._CMFilter) == 416
        assert info.flags == info.reserved == 0
        assert info.filter_type == 2
        self.filter_instance = bytes(info.data.instance_id).decode("utf-16-le").split("\0")[0]
        assert context is None
        self.device_callback = callback
        if not self.cm_error:
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 123
        return self.cm_error

    def power_register(self, flags, subscribe_pointer, output):
        self.power_calls += 1
        assert flags == 2
        subscribe = ctypes.cast(subscribe_pointer, ctypes.POINTER(native._PowerSubscribe)).contents
        assert subscribe.context is None
        self.power_callback = ctypes.cast(subscribe.callback, native._POWER_CALLBACK)
        if not self.power_error:
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 456
        return self.power_error

    def cm_unregister(self, handle):
        self.removed.append(handle.value)
        return self.cm_cleanup_error

    def power_unregister(self, handle):
        self.removed.append(handle.value)
        return self.power_cleanup_error

    def install(self, monkeypatch):
        monkeypatch.setattr(native, "_native_api", lambda: self)

    def notify(self, action=9, instance=HUB, filter_type=2, reserved=0, data=None, size=None):
        raw = (
            struct.pack("<II", filter_type, reserved) + instance.encode("utf-16-le") + b"\0\0"
            if data is None
            else data
        )
        buffer = ctypes.create_string_buffer(raw)
        return self.device_callback(
            123, None, action, ctypes.addressof(buffer), len(raw) if size is None else size
        )


def test_exact_instance_registration_and_idempotent_close(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    source = native.HubNotifications(HUB.lower(), DeferredLoop())
    assert api.filter_instance == HUB
    assert source.revision == 0
    assert source.power_revision == 0
    assert not source.suspended
    assert api.cm_calls == api.power_calls == 1
    assert source in native._RETAINED
    source.close()
    source.close()
    assert api.removed == [456, 123]
    assert source not in native._RETAINED


@pytest.mark.parametrize("action", [7, 8, 9])
async def test_device_callback_fences_immediately_then_enqueues_on_loop(monkeypatch, action):
    api = FakeAPI()
    api.install(monkeypatch)
    loop = DeferredLoop()
    source = native.HubNotifications(HUB, loop)
    try:
        assert api.notify(action, instance=HUB.lower()) == 0
        assert source.revision == 1
        assert source.power_revision == 0
        assert not source.suspended
        assert source._queue.empty()
        loop.flush()
        assert await source.wait() == native.Signal("device", 1, action)
    finally:
        source.close()


async def test_native_thread_hands_off_to_asyncio_loop(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    source = native.HubNotifications(HUB, asyncio.get_running_loop())
    try:
        worker = threading.Thread(target=api.notify)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert source.revision == 1
        assert await asyncio.wait_for(source.wait(), 1) == native.Signal("device", 1, 9)
    finally:
        source.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": 0},
        {"action": 6},
        {"action": 10},
        {"instance": HUB + "1"},
        {"instance": HUB.replace("0&9", "0&8")},
        {"filter_type": 0},
        {"reserved": 1},
        {"data": b"bad"},
        {"data": struct.pack("<II", 2, 0) + b"X\0"},
        {"data": struct.pack("<II", 2, 0) + b"\x00\xd8\0\0"},
    ],
)
def test_unrelated_malformed_or_unsupported_events_are_ignored(monkeypatch, kwargs):
    api = FakeAPI()
    api.install(monkeypatch)
    loop = DeferredLoop()
    source = native.HubNotifications(HUB, loop)
    try:
        assert api.notify(**kwargs) == 0
        assert source.revision == 0
        assert loop.calls == []
    finally:
        source.close()


def test_null_pointer_is_ignored_without_reading_memory(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    source = native.HubNotifications(HUB, DeferredLoop())
    try:
        assert api.device_callback(123, None, 9, None, 100) == 0
        assert source.revision == 0
    finally:
        source.close()


@pytest.mark.parametrize("power_type", [4, 7, 18])
async def test_power_callback_fences_pending_commands_before_loop_scheduling(
    monkeypatch, power_type
):
    api = FakeAPI()
    api.install(monkeypatch)
    loop = DeferredLoop()
    source = native.HubNotifications(HUB, loop)
    try:
        assert api.power_callback(None, power_type, None) == 0
        assert source.revision == 1
        assert source.power_revision == 1
        assert source.suspended is (power_type == 4)
        assert source._queue.empty()
        loop.flush()
        assert await source.wait() == native.Signal("reset", 1, power_type)
    finally:
        source.close()


def test_unrelated_power_event_does_not_reset(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    source = native.HubNotifications(HUB, DeferredLoop())
    try:
        assert api.power_callback(None, 0x8013, None) == 0
        assert source.revision == 0
        assert source.power_revision == 0
        assert not source.suspended
    finally:
        source.close()


async def test_power_state_changes_synchronously_and_device_events_do_not_change_it(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    loop = DeferredLoop()
    source = native.HubNotifications(HUB, loop)
    try:
        api.power_callback(None, 4, None)
        assert source.suspended and source.power_revision == 1
        api.notify()
        assert source.suspended and source.power_revision == 1
        api.power_callback(None, 18, None)
        assert not source.suspended and source.power_revision == 2
        assert source.revision == 3
        assert source._queue.empty()
        loop.flush()
        assert [await source.wait() for _ in range(3)] == [
            native.Signal("reset", 1, 4),
            native.Signal("device", 2, 9),
            native.Signal("reset", 3, 18),
        ]
    finally:
        source.close()


def test_cm_registration_failure_does_not_register_power(monkeypatch):
    api = FakeAPI()
    api.cm_error = 0x05
    api.install(monkeypatch)
    with pytest.raises(native.NotificationError, match="Hub.*0x00000005"):
        native.HubNotifications(HUB, DeferredLoop())
    assert api.power_calls == 0
    assert api.removed == []


def test_power_registration_failure_cleans_up_registered_hub(monkeypatch):
    api = FakeAPI()
    api.power_error = 5
    api.install(monkeypatch)
    with pytest.raises(native.NotificationError, match="Power.*0x00000005"):
        native.HubNotifications(HUB, DeferredLoop())
    assert api.removed == [123]


def test_failed_unregister_retains_callbacks_until_successful_retry(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    source = native.HubNotifications(HUB, DeferredLoop())
    api.cm_cleanup_error = 5
    with pytest.raises(native.NotificationError, match="cleanup"):
        source.close()
    assert source in native._RETAINED
    assert source._cm_handle.value == 123
    assert source._power_handle.value is None
    # Native callbacks may still execute, but closing suppresses all signals.
    assert api.notify() == 0
    assert source.revision == 0
    api.cm_cleanup_error = 0
    source.close()
    assert source not in native._RETAINED
    assert api.removed == [456, 123, 123]


def test_partial_registration_cleanup_failure_also_retains_owner(monkeypatch):
    api = FakeAPI()
    api.power_error = 5
    api.cm_cleanup_error = 5
    api.install(monkeypatch)
    before = set(native._RETAINED)
    with pytest.raises(native.NotificationError, match="cleanup"):
        native.HubNotifications(HUB, DeferredLoop())
    retained = native._RETAINED - before
    assert len(retained) == 1
    api.cm_cleanup_error = 0
    for source in retained:
        source.close()


async def test_closed_source_ignores_previously_scheduled_callbacks(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    loop = DeferredLoop()
    source = native.HubNotifications(HUB, loop)
    api.notify()
    source.close()
    loop.flush()
    assert source._queue.empty()
    with pytest.raises(RuntimeError, match="closed"):
        await source.wait()


def test_closed_asyncio_loop_never_raises_through_native_callback(monkeypatch):
    api = FakeAPI()
    api.install(monkeypatch)
    loop = asyncio.new_event_loop()
    source = native.HubNotifications(HUB, loop)
    loop.close()
    try:
        assert api.notify() == 0
        assert source.revision == 1
    finally:
        source.close()


@pytest.mark.parametrize("instance", ["", "USB", "HID\\SOMETHING", HUB + "\0", "USB\\" + "X" * 200])
def test_invalid_instance_rejected_before_native_registration(monkeypatch, instance):
    monkeypatch.setattr(native, "_native_api", lambda: pytest.fail("Must validate first"))
    with pytest.raises(ValueError, match="complete USB instance"):
        native.HubNotifications(instance, DeferredLoop())


def test_non_windows_is_explicit_and_does_not_load_native_library(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *args, **kwargs: pytest.fail("Must not load"), raising=False
    )
    with pytest.raises(RuntimeError, match="only available on Windows"):
        native.HubNotifications(HUB, DeferredLoop())
