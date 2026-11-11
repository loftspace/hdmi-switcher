from hdmi_follow import devices
from hdmi_follow.config import Device
from hdmi_follow.devices import SeenDevice, owns_devices


def test_all_selected_devices_must_be_present() -> None:
    keyboard = SeenDevice(1, 2, "", "Keyboard")
    mouse = SeenDevice(3, 4, "", "Mouse")
    selected = (Device(1, 2), Device(3, 4))
    assert not owns_devices({keyboard}, selected)
    assert owns_devices({keyboard, mouse}, selected)


def test_serial_can_disambiguate_identical_receivers() -> None:
    seen = {SeenDevice(1, 2, "receiver-a", "Keyboard")}
    assert owns_devices(seen, (Device(1, 2),))
    assert owns_devices(seen, (Device(1, 2, "receiver-a"),))
    assert not owns_devices(seen, (Device(1, 2, "receiver-b"),))


def test_enumeration_deduplicates_logical_interfaces_without_opening_device(monkeypatch) -> None:
    interface = {
        "vendor_id": 1,
        "product_id": 2,
        "product_string": " Keyboard ",
        "serial_number": "receiver-a",
    }
    monkeypatch.setattr(devices.hid, "enumerate", lambda: [interface, interface, {}])
    monkeypatch.setattr(
        devices.hid,
        "device",
        lambda: (_ for _ in ()).throw(AssertionError("Enumeration must not open a keyboard")),
    )
    assert devices.enumerate_devices() == {SeenDevice(1, 2, "receiver-a", "Keyboard")}


def test_unrelated_usb_device_does_not_establish_ownership() -> None:
    assert not owns_devices({SeenDevice(1, 3, "", "Other")}, (Device(1, 2),))
