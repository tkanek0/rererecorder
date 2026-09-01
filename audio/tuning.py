"""Read and write the XVF-3000's DSP parameters over USB control transfer.

The array carries a second, non-audio interface through which the chip exposes
its whole signal chain: echo cancellation, noise suppression, gain control, the
beamformer, and - the reason this module exists - the direction it currently
believes sound is coming from.

The parameter table is transcribed from Seeed's ``usb_4_mic_array`` (Apache
2.0), which in turn follows the XVF-3000 datasheet:
https://github.com/respeaker/usb_4_mic_array/blob/master/tuning.py

Reading needs write access to the USB device node, which the desktop user does
not have by default. See :class:`AccessDenied` for the udev rule that grants it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Literal

import usb.core
import usb.util

from . import config

#: Vendor-specific control transfer directed at the device itself.
_CTRL_IN = usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE
_CTRL_OUT = (
    usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE
)

#: Milliseconds. Generous: the chip answers in well under a millisecond, and a
#: timeout here means something is wrong rather than merely slow.
_TIMEOUT_MS = 1000


class DeviceNotFound(RuntimeError):
    """Raised when no ReSpeaker array is attached."""


class AccessDenied(RuntimeError):
    """Raised when the device is present but its node cannot be written to.

    Audio capture is unaffected either way: that goes through ALSA, which has
    no such restriction. Only the tuning interface needs the rule.
    """


#: Printed whenever a control transfer is refused. The device node is root-only
#: out of the box, and there is no way to discover that from the errno alone.
UDEV_HINT = f"""USB control transfer was refused: the device node is not writable.

Grant access once:

    echo 'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{config.USB_VENDOR_ID:04x}", ATTRS{{idProduct}}=="{config.USB_PRODUCT_ID:04x}", MODE="0666"' \\
        | sudo tee /etc/udev/rules.d/99-respeaker.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --action=add --subsystem-match=usb

The last line re-applies the rule to the array without unplugging it. Audio
capture works without any of this."""


@dataclass(frozen=True)
class Parameter:
    """One tunable value in the chip's signal chain.

    Attributes:
        id: Resource id the parameter lives in, sent as ``wIndex``.
        offset: Index of the parameter within that resource.
        kind: ``int`` or ``float``; decides both the wire format and how a
            read response is decoded.
        maximum: Largest accepted value.
        minimum: Smallest accepted value.
        access: ``ro`` for read-only, ``rw`` for writable.
        description: What the datasheet says it does.
    """

    id: int
    offset: int
    kind: Literal["int", "float"]
    maximum: float
    minimum: float
    access: Literal["ro", "rw"]
    description: str

    @property
    def writable(self) -> bool:
        """Whether this parameter can be set."""
        return self.access == "rw"


def _p(
    id: int,
    offset: int,
    kind: str,
    maximum: float,
    minimum: float,
    access: str,
    description: str,
) -> Parameter:
    """Build a Parameter, keeping the table below readable."""
    return Parameter(
        id=id,
        offset=offset,
        kind=kind,  # type: ignore[arg-type]
        maximum=maximum,
        minimum=minimum,
        access=access,  # type: ignore[arg-type]
        description=description,
    )


#: The chip's parameters by name.
PARAMETERS: dict[str, Parameter] = {
    # -- resource 18: acoustic echo canceller --------------------------------
    "AECFREEZEONOFF": _p(18, 7, "int", 1, 0, "rw",
        "Adaptive echo canceller updates inhibit. 0 = adaptation enabled, "
        "1 = freeze adaptation, filter only"),
    "AECNORM": _p(18, 19, "float", 16, 0.25, "rw",
        "Limit on norm of AEC filter coefficients"),
    "AECPATHCHANGE": _p(18, 25, "int", 1, 0, "ro",
        "AEC path change detection. 0 = no path change, 1 = path change"),
    "RT60": _p(18, 26, "float", 0.9, 0.25, "ro",
        "Current RT60 reverberation time estimate in seconds"),
    "HPFONOFF": _p(18, 27, "int", 3, 0, "rw",
        "High-pass filter on the microphone signals. 0 = off, 1 = 70 Hz, "
        "2 = 125 Hz, 3 = 180 Hz cut-off"),
    "RT60ONOFF": _p(18, 28, "int", 1, 0, "rw",
        "RT60 estimation for AES. 0 = off, 1 = on"),
    "AECSILENCELEVEL": _p(18, 30, "float", 1, 1e-09, "rw",
        "Threshold for signal detection in AEC, [-inf .. 0] dBov "
        "(default -80 dBov)"),
    "AECSILENCEMODE": _p(18, 31, "int", 1, 0, "ro",
        "AEC far-end silence detection status. 0 = signal detected, "
        "1 = silence detected"),
    # -- resource 19: gain, noise suppression, beamformer, VAD ---------------
    "AGCONOFF": _p(19, 0, "int", 1, 0, "rw",
        "Automatic gain control. 0 = off, 1 = on"),
    "AGCMAXGAIN": _p(19, 1, "float", 1000, 1, "rw",
        "Maximum AGC gain factor, [0 .. 60] dB (default 30 dB)"),
    "AGCDESIREDLEVEL": _p(19, 2, "float", 0.99, 1e-08, "rw",
        "Target power level of the output signal, [-inf .. 0] dBov "
        "(default -23 dBov)"),
    "AGCGAIN": _p(19, 3, "float", 1000, 1, "rw",
        "Current AGC gain factor, [0 .. 60] dB (default 0 dB)"),
    "AGCTIME": _p(19, 4, "float", 1, 0.1, "rw",
        "AGC ramp-up / ramp-down time constant in seconds"),
    "CNIONOFF": _p(19, 5, "int", 1, 0, "rw",
        "Comfort noise insertion. 0 = off, 1 = on"),
    "FREEZEONOFF": _p(19, 6, "int", 1, 0, "rw",
        "Adaptive beamformer updates. 0 = adaptation enabled, 1 = freeze "
        "adaptation, filter only"),
    "STATNOISEONOFF": _p(19, 8, "int", 1, 0, "rw",
        "Stationary noise suppression. 0 = off, 1 = on"),
    "GAMMA_NS": _p(19, 9, "float", 3, 0, "rw",
        "Over-subtraction factor of stationary noise"),
    "MIN_NS": _p(19, 10, "float", 1, 0, "rw",
        "Gain floor for stationary noise suppression, [-inf .. 0] dB "
        "(default -16 dB)"),
    "NONSTATNOISEONOFF": _p(19, 11, "int", 1, 0, "rw",
        "Non-stationary noise suppression. 0 = off, 1 = on"),
    "GAMMA_NN": _p(19, 12, "float", 3, 0, "rw",
        "Over-subtraction factor of non-stationary noise"),
    "MIN_NN": _p(19, 13, "float", 1, 0, "rw",
        "Gain floor for non-stationary noise suppression, [-inf .. 0] dB "
        "(default -10 dB)"),
    "ECHOONOFF": _p(19, 14, "int", 1, 0, "rw",
        "Echo suppression. 0 = off, 1 = on"),
    "GAMMA_E": _p(19, 15, "float", 3, 0, "rw",
        "Over-subtraction factor of echo, direct and early components"),
    "GAMMA_ETAIL": _p(19, 16, "float", 3, 0, "rw",
        "Over-subtraction factor of echo, tail components"),
    "GAMMA_ENL": _p(19, 17, "float", 5, 0, "rw",
        "Over-subtraction factor of non-linear echo"),
    "NLATTENONOFF": _p(19, 18, "int", 1, 0, "rw",
        "Non-linear echo attenuation. 0 = off, 1 = on"),
    "NLAEC_MODE": _p(19, 20, "int", 2, 0, "rw",
        "Non-linear AEC training mode. 0 = off, 1 = phase 1, 2 = phase 2"),
    "SPEECHDETECTED": _p(19, 22, "int", 1, 0, "ro",
        "Speech detection status. 0 = no speech, 1 = speech detected"),
    "FSBUPDATED": _p(19, 23, "int", 1, 0, "ro",
        "Fixed super-directive beamformer update decision. 0 = not updated, "
        "1 = updated"),
    "FSBPATHCHANGE": _p(19, 24, "int", 1, 0, "ro",
        "Fixed super-directive beamformer path change detection. 0 = no "
        "change, 1 = change detected"),
    "TRANSIENTONOFF": _p(19, 29, "int", 1, 0, "rw",
        "Transient echo suppression. 0 = off, 1 = on"),
    "VOICEACTIVITY": _p(19, 32, "int", 1, 0, "ro",
        "VAD voice activity status. 0 = no voice activity, 1 = voice activity"),
    "STATNOISEONOFF_SR": _p(19, 33, "int", 1, 0, "rw",
        "Stationary noise suppression for ASR. 0 = off, 1 = on"),
    "NONSTATNOISEONOFF_SR": _p(19, 34, "int", 1, 0, "rw",
        "Non-stationary noise suppression for ASR. 0 = off, 1 = on"),
    "GAMMA_NS_SR": _p(19, 35, "float", 3, 0, "rw",
        "Over-subtraction factor of stationary noise for ASR (default 1.0)"),
    "GAMMA_NN_SR": _p(19, 36, "float", 3, 0, "rw",
        "Over-subtraction factor of non-stationary noise for ASR "
        "(default 1.1)"),
    "MIN_NS_SR": _p(19, 37, "float", 1, 0, "rw",
        "Gain floor for stationary noise suppression for ASR (default -16 dB)"),
    "MIN_NN_SR": _p(19, 38, "float", 1, 0, "rw",
        "Gain floor for non-stationary noise suppression for ASR "
        "(default -10 dB)"),
    "GAMMAVAD_SR": _p(19, 39, "float", 1000, 0, "rw",
        "Threshold for voice activity detection, [-inf .. 60] dB "
        "(default 3.5 dB)"),
    # -- resource 21: direction of arrival ------------------------------------
    "DOAANGLE": _p(21, 0, "int", 359, 0, "ro",
        "Direction of arrival in degrees. Current value; orientation depends "
        "on the build configuration"),
}


class Tuning:
    """A handle on the chip's parameter interface.

    Not thread-safe: one control transfer at a time per device. The pollers in
    :mod:`respeaker.doa` own their handle rather than sharing one.
    """

    def __init__(self, device: usb.core.Device) -> None:
        """Wrap an already-located USB device.

        Args:
            device: The array, as returned by :func:`usb.core.find`.
        """
        self._device = device

    # -- parameters --------------------------------------------------------

    def read(self, name: str) -> int | float:
        """Read one parameter.

        Args:
            name: Key into :data:`PARAMETERS`.

        Returns:
            The current value, as an int or a float according to the
            parameter's declared kind.

        Raises:
            KeyError: If the name is not a known parameter.
            AccessDenied: If the device node is not writable.
        """
        parameter = PARAMETERS[name]

        # The read command packs the offset into wValue: bit 7 marks it a read,
        # bit 6 an integer. The chip answers with two words whose meaning
        # depends on the kind - a plain value, or a mantissa and an exponent.
        command = 0x80 | parameter.offset
        if parameter.kind == "int":
            command |= 0x40

        try:
            response = self._device.ctrl_transfer(
                _CTRL_IN, 0, command, parameter.id, 8, _TIMEOUT_MS
            )
        except usb.core.USBError as error:
            raise _translate(error) from error

        first, second = struct.unpack("<ii", response.tobytes())
        if parameter.kind == "int":
            return first
        return first * (2.0**second)

    def write(self, name: str, value: float) -> None:
        """Set one parameter.

        Args:
            name: Key into :data:`PARAMETERS`.
            value: New value, clamped to the parameter's declared range.

        Raises:
            KeyError: If the name is not a known parameter.
            ValueError: If the parameter is read-only.
            AccessDenied: If the device node is not writable.
        """
        parameter = PARAMETERS[name]
        if not parameter.writable:
            raise ValueError(f"{name} is read-only")

        clamped = min(parameter.maximum, max(parameter.minimum, float(value)))
        # Offset, value, and a flag saying which of the two the value is.
        if parameter.kind == "int":
            payload = struct.pack("<iii", parameter.offset, int(clamped), 1)
        else:
            payload = struct.pack("<ifi", parameter.offset, clamped, 0)

        try:
            self._device.ctrl_transfer(
                _CTRL_OUT, 0, 0, parameter.id, payload, _TIMEOUT_MS
            )
        except usb.core.USBError as error:
            raise _translate(error) from error

    # -- shorthands --------------------------------------------------------

    @property
    def direction(self) -> int:
        """Direction of arrival in degrees, 0-359."""
        return int(self.read("DOAANGLE"))

    @property
    def voice_activity(self) -> bool:
        """Whether the chip currently hears voice."""
        return bool(self.read("VOICEACTIVITY"))

    @property
    def speech_detected(self) -> bool:
        """Whether the chip's speech detector has fired."""
        return bool(self.read("SPEECHDETECTED"))

    @property
    def firmware_version(self) -> int:
        """Firmware version reported by the control interface."""
        try:
            return int(
                self._device.ctrl_transfer(_CTRL_IN, 0, 0x80, 0, 1, _TIMEOUT_MS)[0]
            )
        except usb.core.USBError as error:
            raise _translate(error) from error

    def close(self) -> None:
        """Release the USB handle."""
        usb.util.dispose_resources(self._device)


def _translate(error: usb.core.USBError) -> Exception:
    """Turn a libusb errno into something that says what to do about it."""
    # errno 13 is EACCES: the device is there, the node is not ours to write.
    if getattr(error, "errno", None) == 13:
        return AccessDenied(UDEV_HINT)
    return error


def find(
    vendor_id: int = config.USB_VENDOR_ID, product_id: int = config.USB_PRODUCT_ID
) -> Tuning:
    """Locate the array and return a handle on its parameter interface.

    Args:
        vendor_id: USB vendor id to match.
        product_id: USB product id to match.

    Returns:
        A :class:`Tuning` bound to the device.

    Raises:
        DeviceNotFound: If no matching device is attached.
    """
    device = usb.core.find(idVendor=vendor_id, idProduct=product_id)
    if device is None:
        raise DeviceNotFound(
            f"no USB device {vendor_id:04x}:{product_id:04x} - is the array "
            "plugged in?"
        )
    return Tuning(device)
