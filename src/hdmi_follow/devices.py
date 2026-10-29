"""Enumerate HID metadata. Never open devices or read input reports."""

from dataclasses import dataclass

import hid

from hdmi_follow.config import Device


@dataclass(frozen=True)
class SeenDevice:
    vendor_id: int
    product_id: int
    serial: str
    name: str

    def matches(self, device: Device) -> bool:
        return (self.vendor_id, self.product_id) == (device.vendor_id, device.product_id) and (
            not device.serial or device.serial == self.serial
        )


def enumerate_devices() -> set[SeenDevice]:
    # Keep all HID calls on the caller's thread, including on macOS.
    return {
        SeenDevice(
            int(item["vendor_id"]),
            int(item["product_id"]),
            str(item.get("serial_number") or ""),
            str(item.get("product_string") or "Unnamed HID device").strip(),
        )
        for item in hid.enumerate()
        if item.get("vendor_id") and item.get("product_id")
    }


def owns_devices(seen: set[SeenDevice], selected: tuple[Device, ...]) -> bool:
    # All selected devices must be present; logical HID interfaces are deduplicated.
    return all(any(item.matches(device) for item in seen) for device in selected)
