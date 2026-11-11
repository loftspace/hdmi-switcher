import ctypes
from types import SimpleNamespace

import pytest

from hdmi_follow import windows_usb

HUB = r"USB\VID_0557&PID_2414\TEST_INSTANCE"


class FakeNative:
    def __init__(self, instances=(), size_results=(), list_results=()):
        self.instances = instances
        self.data = "\0".join(instances) + "\0\0"
        self.size_results = list(size_results)
        self.list_results = list(list_results)
        self.size_calls = 0
        self.list_calls = 0

    def size(self, length_pointer, filter_text, flags):
        self.size_calls += 1
        assert filter_text == "USB"
        assert flags == 0x101
        ctypes.cast(length_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(self.data)
        return self.size_results.pop(0) if self.size_results else 0

    def listing(self, filter_text, buffer, length, flags):
        self.list_calls += 1
        assert filter_text == "USB"
        assert flags == 0x101
        assert length == len(buffer) == len(self.data)
        result = self.list_results.pop(0) if self.list_results else 0
        if result == 0:
            for index, char in enumerate(self.data):
                buffer[index] = char
        return result

    def install(self, monkeypatch):
        monkeypatch.setattr(windows_usb, "_native_functions", lambda: (self.size, self.listing))


def test_present_instances_normalizes_case_and_deduplicates(monkeypatch):
    fake = FakeNative((HUB.lower(), HUB, r"USB\ROOT_HUB30\OTHER"))
    fake.install(monkeypatch)
    assert windows_usb.present_instances() == {HUB, r"USB\ROOT_HUB30\OTHER"}
    assert fake.size_calls == fake.list_calls == 1


def test_exact_instance_match_does_not_confuse_other_same_vid_device(monkeypatch):
    FakeNative((HUB,)).install(monkeypatch)
    assert windows_usb.is_present(HUB.lower())
    assert not windows_usb.is_present(HUB + "1")


def test_hardware_id_matches_after_port_change_and_rejects_ambiguity(monkeypatch):
    hardware_id = r"USB\VID_0557&PID_2414"
    moved = hardware_id + r"\DIFFERENT_PORT"
    FakeNative((moved, hardware_id + r"&MI_00\INTERFACE")).install(monkeypatch)
    assert windows_usb.is_present(hardware_id.lower())
    assert not windows_usb.is_present(HUB)
    FakeNative((HUB, moved)).install(monkeypatch)
    with pytest.raises(windows_usb.USBEnumerationError, match="Multiple matching USB hubs"):
        windows_usb.is_present(hardware_id)
    assert windows_usb.is_present(HUB)
    assert not windows_usb.is_present(HUB.replace("TEST_INSTANCE", "OTHER_INSTANCE"))


def test_empty_present_list_is_valid(monkeypatch):
    FakeNative().install(monkeypatch)
    assert windows_usb.present_instances() == set()


def test_empty_list_single_terminator_is_valid(monkeypatch):
    fake = FakeNative()
    fake.data = "\0"
    fake.install(monkeypatch)
    assert windows_usb.present_instances() == set()


def test_growth_race_repeats_size_and_list(monkeypatch):
    fake = FakeNative((HUB,), list_results=(0x1A, 0))
    fake.install(monkeypatch)
    assert windows_usb.present_instances() == {HUB}
    assert fake.size_calls == fake.list_calls == 2


def test_repeated_growth_is_bounded_and_not_reported_as_unplug(monkeypatch):
    fake = FakeNative((HUB,), list_results=(0x1A, 0x1A, 0x1A))
    fake.install(monkeypatch)
    with pytest.raises(windows_usb.USBEnumerationError, match="three"):
        windows_usb.is_present(HUB)
    assert fake.size_calls == fake.list_calls == 3


def test_size_race_is_also_bounded(monkeypatch):
    fake = FakeNative(size_results=(0x1A, 0x1A, 0x1A))
    fake.install(monkeypatch)
    with pytest.raises(windows_usb.USBEnumerationError):
        windows_usb.present_instances()
    assert fake.size_calls == 3
    assert fake.list_calls == 0


@pytest.mark.parametrize("stage", ["size", "list"])
def test_unexpected_native_failure_is_error_not_absence(monkeypatch, stage):
    fake = FakeNative(
        (HUB,),
        size_results=(0x05,) if stage == "size" else (),
        list_results=(0x05,) if stage == "list" else (),
    )
    fake.install(monkeypatch)
    with pytest.raises(windows_usb.USBEnumerationError, match="0x00000005"):
        windows_usb.is_present(HUB)
    assert fake.size_calls == 1
    assert fake.list_calls == (1 if stage == "list" else 0)


def test_native_bindings_have_explicit_four_byte_lengths_and_wide_buffers(monkeypatch):
    size = SimpleNamespace()
    listing = SimpleNamespace()
    library = SimpleNamespace(CM_Get_Device_ID_List_SizeW=size, CM_Get_Device_ID_ListW=listing)
    monkeypatch.setattr(windows_usb.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: library, raising=False)
    windows_usb._native_functions.cache_clear()
    try:
        assert windows_usb._native_functions() == (size, listing)
        assert size.argtypes == [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p, ctypes.c_uint32]
        assert listing.argtypes == [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        assert ctypes.sizeof(size.restype) == ctypes.sizeof(listing.restype) == 4
    finally:
        windows_usb._native_functions.cache_clear()


def test_unsupported_platform_fails_explicitly_without_loading_library(monkeypatch):
    monkeypatch.setattr(windows_usb.sys, "platform", "darwin")
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *args, **kwargs: pytest.fail("Must not load"), raising=False
    )
    windows_usb._native_functions.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="only available on Windows"):
            windows_usb.present_instances()
    finally:
        windows_usb._native_functions.cache_clear()


def test_invalid_native_buffer_length_is_error_not_absence(monkeypatch):
    def size(length_pointer, filter_text, flags):
        ctypes.cast(length_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = 0
        return 0

    monkeypatch.setattr(windows_usb, "_native_functions", lambda: (size, None))
    with pytest.raises(windows_usb.USBEnumerationError, match="buffer length"):
        windows_usb.present_instances()


def test_unterminated_native_list_is_error_not_partial_inventory(monkeypatch):
    fake = FakeNative()
    fake.data = "USB\\TRUNCATED"
    fake.install(monkeypatch)
    with pytest.raises(windows_usb.USBEnumerationError, match="unterminated"):
        windows_usb.present_instances()
