"""Read the XVF-3000's direction of arrival and voice activity over USB.

The wire format follows Seeed's ``usb_4_mic_array`` (Apache 2.0):
https://github.com/respeaker/usb_4_mic_array/blob/master/tuning.py

Needs the libusb-1.0 library and access to the device: on Linux a writable
node (:data:`UDEV_HINT`), on Windows WinUSB bound to the control interface
(:data:`WINUSB_HINT`).
"""

from __future__ import annotations

import struct
import sys

import usb.backend.libusb1
import usb.core
import usb.util

from . import config
from .types import DeviceNotFound

_CTRL_IN = usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE

#: Milliseconds. A timeout means something is wrong, not merely slow.
_TIMEOUT_MS = 1000

#: ``(resource id, offset)`` of the two integer parameters read here.
_DOAANGLE = (21, 0)
_VOICEACTIVITY = (19, 32)


class AccessDenied(RuntimeError):
    """Raised when the device is present but cannot be opened for control.

    Audio capture is unaffected; only the tuning interface needs the access.
    """


class LibusbMissing(RuntimeError):
    """Raised when no libusb-1.0 library can be loaded."""


#: Printed whenever a control transfer is refused: the node is root-only by default.
UDEV_HINT = f"""USB control transfer was refused: the device node is not writable.

Grant access once:

    echo 'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{config.USB_VENDOR_ID:04x}", ATTRS{{idProduct}}=="{config.USB_PRODUCT_ID:04x}", MODE="0666"' \\
        | sudo tee /etc/udev/rules.d/99-respeaker.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --action=add --subsystem-match=usb

The last line re-applies the rule to the array without unplugging it. Audio
capture works without any of this."""

#: Printed on Windows, where libusb can open only an interface bound to WinUSB.
WINUSB_HINT = """USB control transfer failed: the array's control interface has no WinUSB driver.

Bind WinUSB to "SEEED Control (Interface 3)" with Zadig, as an administrator
(docs/windows-native.md "Direction of arrival needs WinUSB"). Never the
composite device or Interface 0, which would take the audio away. Audio
capture works without any of this."""


class Tuning:
    """A handle on the chip's parameter interface.

    Not thread-safe: one control transfer at a time per device.
    """

    def __init__(self, device: usb.core.Device, backend: object) -> None:
        """Wrap an already-located USB device.

        Args:
            device: The array, as returned by :func:`usb.core.find`.
            backend: The libusb context it was found through, kept alive with it.
        """
        self._device = device
        self._backend = backend

    def _read_int(self, parameter: tuple[int, int]) -> int:
        """Read one integer parameter.

        Raises:
            AccessDenied: If the device node is not writable.
        """
        resource, offset = parameter
        # wValue: offset, bit 7 = read, bit 6 = int.
        try:
            response = self._device.ctrl_transfer(
                _CTRL_IN, 0, 0xC0 | offset, resource, 8, _TIMEOUT_MS
            )
        except usb.core.USBError as error:
            raise _translate(error) from error
        return struct.unpack("<ii", response.tobytes())[0]

    @property
    def direction(self) -> int:
        """Direction of arrival in degrees, 0-359."""
        return self._read_int(_DOAANGLE)

    @property
    def voice_activity(self) -> bool:
        """Whether the chip currently hears voice."""
        return bool(self._read_int(_VOICEACTIVITY))

    def close(self) -> None:
        """Release the USB handle and the libusb context."""
        usb.util.dispose_resources(self._device)
        self._backend = None


def _translate(error: usb.core.USBError) -> Exception:
    """Turn a libusb errno into something that says what to do about it."""
    # EACCES on Linux: the node is not ours to write. On Windows libusb reports
    # an interface without a supported driver as NOT_FOUND, which pyusb maps to 2.
    errno = getattr(error, "errno", None)
    if errno == 13:
        return AccessDenied(UDEV_HINT)
    if errno == 2 and sys.platform == "win32":
        return AccessDenied(WINUSB_HINT)
    return error


def _load_libusb() -> object:
    """Load libusb-1.0, from the ``libusb-package`` wheel where it is installed.

    Raises:
        LibusbMissing: If no library can be found.
    """
    try:
        import libusb_package
    except ImportError:
        find_library = None
    else:
        find_library = libusb_package.find_library
    if usb.backend.libusb1.get_backend(find_library=find_library) is None:
        raise LibusbMissing(
            "libusb-1.0 not found: install it (Windows: `uv sync` brings libusb-package)"
        )
    return usb.backend.libusb1._lib


def find_tuning(
    vendor_id: int = config.USB_VENDOR_ID, product_id: int = config.USB_PRODUCT_ID
) -> Tuning:
    """Locate the array and return a handle on its parameter interface.

    Args:
        vendor_id: USB vendor id to match.
        product_id: USB product id to match.

    Returns:
        A :class:`Tuning` bound to the device.

    Raises:
        LibusbMissing: If no libusb-1.0 library can be loaded.
        DeviceNotFound: If no matching device is attached.

    Each call enumerates through a libusb context of its own: pyusb's shared
    one keeps the device list it first saw where no hotplug events arrive, as
    in a container, so a replugged array would stay unreachable
    (docs/decisions.md 29). ``_LibUSB`` is pyusb's private context wrapper.
    """
    backend = usb.backend.libusb1._LibUSB(_load_libusb())
    device = usb.core.find(idVendor=vendor_id, idProduct=product_id, backend=backend)
    if device is None:
        raise DeviceNotFound(
            f"no USB device {vendor_id:04x}:{product_id:04x} - is the array plugged in?"
        )
    return Tuning(device, backend)
