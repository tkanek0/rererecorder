"""Opening the array for capture, and finding it.

Times come from PortAudio's ``inputBufferAdcTime``, not the callback's
``time.monotonic()``, which runs one block (16 ms at 256 samples) late; when
the host API does not fill it in, the callback time is used and logged.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

import numpy as np

from . import config
from .types import DeviceNotFound

try:
    import sounddevice as sd
except OSError as _error:  # pragma: no cover - environment, not logic
    raise OSError(
        "PortAudio could not be loaded, so the array cannot be read. On "
        "Debian or Ubuntu: sudo apt install -y libportaudio2"
    ) from _error

logger = logging.getLogger(__name__)

#: Scale from int16 to the [-1, 1] convention every processor works in.
_INT16_SCALE = 1.0 / 32768.0

#: Valid blocks of `now - reported` collected before judging the ADC clock's
#: domain (~0.3 s). See docs/decisions.md 26.
_DOMAIN_CALIBRATION_BLOCKS = 20

#: Largest spread of `now - reported` still judged a fixed offset rather than
#: an incoherent clock. See docs/decisions.md 26.
_DOMAIN_STABILITY_S = 0.25


# -- COM, for WASAPI's callback mode ------------------------------------------
#
# A WASAPI callback stream opened from a thread without COM initialised fails
# (PaErrorCode -9999). See docs/decisions.md 25.

if sys.platform == "win32":
    import ctypes

    #: Apartment model for a worker thread with no message pump.
    _COINIT_MULTITHREADED = 0x0

    def _com_initialize() -> bool:
        """Initialise COM on the calling thread, once, for its lifetime.

        Returns:
            Whether COM is now usable on this thread. False (logged, not
            raised) if the thread is already in an incompatible apartment.
        """
        result = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        if result < 0:
            logger.warning(
                "CoInitializeEx failed (%#x); a WASAPI callback stream on this "
                "thread may fail as a result",
                result & 0xFFFFFFFF,
            )
            return False
        return True

    def _com_uninitialize() -> None:
        """Release what a successful :func:`_com_initialize` set up."""
        ctypes.windll.ole32.CoUninitialize()

else:

    def _com_initialize() -> bool:
        """No-op off Windows."""
        return False

    def _com_uninitialize() -> None:
        """No-op off Windows, matching :func:`_com_initialize`."""


class AdcClock:
    """Places each block on CLOCK_MONOTONIC from PortAudio's ``inputBufferAdcTime``.

    The reported time is used as-is, corrected by a fixed offset, or replaced
    by the callback clock, as decided once over the first
    :data:`_DOMAIN_CALIBRATION_BLOCKS` valid blocks. Those blocks are held until
    the decision and then stamped by it: the callback clock arrives in bursts,
    and stamping them from it would read as gaps (docs/decisions.md 20, 26).
    """

    def __init__(self, rate: int) -> None:
        """Start undecided.

        Args:
            rate: Sample rate in Hz.
        """
        self._rate = rate
        self._warned = False
        self._held: list[tuple[np.ndarray, float, float]] = []
        self._decided = False
        #: Added to the reported time when it is stable but on another epoch.
        self.offset = 0.0
        #: Set when the reported time is not one clock; the callback's is used.
        self.incoherent = False

    def add(self, block: np.ndarray, reported: float) -> list[tuple[np.ndarray, float]]:
        """Take one block and return those now ready, each with its ADC time.

        Args:
            block: The block's samples.
            reported: PortAudio's ``inputBufferAdcTime``; zero when the host
                API does not fill it in.

        Returns:
            ``(block, time)`` pairs in order: none while the clock is still
            being judged, then every held block at once, then one per call.
        """
        now = time.monotonic()
        if reported <= 0.0:
            if not self._warned:
                logger.warning(
                    "PortAudio did not report inputBufferAdcTime; timing audio "
                    "from the callback instead, which is a block coarser"
                )
                self._warned = True
            return [*self.flush(), (block, self._callback_time(block, now))]
        if self._decided:
            return [(block, self._stamp(block, reported, now))]
        self._held.append((block, reported, now))
        if len(self._held) < _DOMAIN_CALIBRATION_BLOCKS:
            return []
        self._decide()
        held, self._held = self._held, []
        return [(b, self._stamp(b, r, n)) for b, r, n in held]

    def flush(self) -> list[tuple[np.ndarray, float]]:
        """Release blocks still held before a decision, timed by the callback."""
        held, self._held = self._held, []
        return [(b, self._callback_time(b, n)) for b, _, n in held]

    def _callback_time(self, block: np.ndarray, now: float) -> float:
        return now - len(block) / self._rate

    def _stamp(self, block: np.ndarray, reported: float, now: float) -> float:
        if self.incoherent:
            return self._callback_time(block, now)
        return reported + self.offset

    def _decide(self) -> None:
        """Judge the reported clock from the held blocks."""
        self._decided = True
        expected_lag = len(self._held[0][0]) / self._rate
        lags = np.array([now - reported for _, reported, now in self._held])
        mean_lag, spread = float(lags.mean()), float(lags.std())
        if -expected_lag <= mean_lag <= 3.0 * expected_lag:
            logger.info(
                "inputBufferAdcTime agrees with time.monotonic(): the callback "
                "runs %.1f ms after a block's first sample (block %.1f ms)",
                mean_lag * 1000.0,
                expected_lag * 1000.0,
            )
        elif spread < _DOMAIN_STABILITY_S:
            self.offset = mean_lag - expected_lag
            logger.warning(
                "inputBufferAdcTime is %.3f s off the callback clock but stable "
                "(+/-%.1f ms): correcting for the fixed offset",
                mean_lag,
                spread * 1000.0,
            )
        else:
            self.incoherent = True
            logger.warning(
                "inputBufferAdcTime is %.3f s off the callback clock and not "
                "stable (+/-%.3f s): not readable as one clock, so timing from "
                "the callback instead",
                mean_lag,
                spread,
            )


class Capture:
    """The array open for capture.

    Every block goes to ``on_block`` as float32 in [-1, 1] with the ADC time of
    its first sample, on PortAudio's thread. Open on the thread that will
    close it; a WASAPI stream needs COM there (docs/decisions.md 25).
    """

    def __init__(
        self,
        on_block: Callable[[np.ndarray, float], None],
        device: str = config.DEVICE_NAME,
        rate: int = config.SAMPLE_RATE,
        channels: int = config.CHANNELS,
        block_size: int = config.BLOCK_SIZE,
    ) -> None:
        """Prepare a capture without opening the device.

        Args:
            on_block: Receives ``(samples, adc_time)`` for every block.
            device: Substring matched against the input device's name.
            rate: Sample rate to ask for.
            channels: Channels to ask for. A device offering fewer is reported
                rather than used.
            block_size: Frames per callback.
        """
        self._on_block = on_block
        self._device = device
        self._rate = rate
        self._channels = channels
        self._block_size = block_size
        self._clock = AdcClock(rate)
        self._stream: sd.InputStream | None = None
        self._com = False
        #: Input overflows the driver reported since opening.
        self.overruns = 0
        #: ``time.monotonic()`` of the newest block, or of opening.
        self.last_block_at = 0.0

    def __enter__(self) -> Self:
        """Open the device and start capturing.

        Raises:
            DeviceNotFound: If no usable device matches.
            sounddevice.PortAudioError: If PortAudio refuses to open it.
        """
        self._com = _com_initialize()
        index = _resolve_device(self._device, self._channels, self._rate)
        # int16 is the endpoint's wire format; converted in the callback.
        self._stream = sd.InputStream(
            device=index,
            channels=self._channels,
            samplerate=self._rate,
            dtype="int16",
            blocksize=self._block_size,
            callback=self._callback,
        )
        self.last_block_at = time.monotonic()
        self._stream.start()
        logger.info("capturing %d ch at %d Hz", self._channels, self._rate)
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop capturing and close the device."""
        stream, self._stream = self._stream, None
        try:
            if stream is not None:
                # Abort, not stop: a stalled stream has nothing left to drain.
                stream.abort()
                stream.close()
            for block, at in self._clock.flush():
                self._on_block(block, at)
        finally:
            if self._com:
                _com_uninitialize()

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """PortAudio's callback. Runs on PortAudio's thread, so it stays short."""
        self.last_block_at = time.monotonic()
        if status.input_overflow:
            self.overruns += 1
        block = indata.astype(np.float32) * _INT16_SCALE
        for ready, at in self._clock.add(block, time_info.inputBufferAdcTime):
            self._on_block(ready, at)


#: Host API preferred on Windows, only together with a matching rate.
#: See docs/decisions.md 24.
_PREFERRED_HOST_API = "Windows WASAPI"

#: Tolerance for a reported `default_samplerate` to match the requested rate:
#: float rounding, not 44100 against 16000.
_RATE_MATCH_TOLERANCE_HZ = 1.0


def _resolve_device(name: str, channels: int, rate: int) -> int:
    """Find the input device whose name contains ``name``.

    Args:
        name: Substring to look for, case-insensitively.
        channels: Channel count the caller intends to open.
        rate: Sample rate the caller intends to open at. Used only to rank
            candidates that share a name, not to filter them.

    Returns:
        The PortAudio device index.

    Raises:
        DeviceNotFound: If nothing matches, or if no match can supply the
            requested channels (the 1-channel firmware).

    Matches are ranked: preferred host API with a matching rate, then any
    matching rate, then the first match; enumeration order breaks ties.
    See docs/decisions.md 24.
    """
    needle = name.lower()
    matches = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if device["max_input_channels"] > 0 and needle in str(device["name"]).lower()
    ]
    usable = [
        (index, device) for index, device in matches
        if device["max_input_channels"] >= channels
    ]
    if not usable:
        if matches:
            _, device = matches[0]
            raise DeviceNotFound(
                f"{device['name']!r} offers {device['max_input_channels']} input "
                f"channels, not {channels}. The 1-channel firmware looks like "
                "this; flash the 6-channel one to get the raw microphones."
            )
        raise DeviceNotFound(
            f"no capture device whose name contains {name!r}. Plugged in? "
            "`arecord -l` lists what the system can see."
        )

    hostapis = sd.query_hostapis()

    def rate_matches(device: dict) -> bool:
        return abs(device["default_samplerate"] - rate) < _RATE_MATCH_TOLERANCE_HZ

    def host_api_name(device: dict) -> str:
        return str(hostapis[device["hostapi"]]["name"])

    for index, device in usable:
        if host_api_name(device) == _PREFERRED_HOST_API and rate_matches(device):
            return index
    for index, device in usable:
        if rate_matches(device):
            return index
    return usable[0][0]


@dataclass(frozen=True)
class DeviceStatus:
    """What PortAudio can currently see of the configured capture device.

    Built by enumerating devices only; nothing is opened.

    Attributes:
        connected: Whether a matching device is present.
        name: The device's name, as PortAudio reports it.
        host_api: Which host API it would be opened through.
        channels: Input channels it offers.
        rate: Its default sample rate.
        error: Why no device was found, when ``connected`` is False.
    """

    connected: bool
    name: str | None = None
    host_api: str | None = None
    channels: int | None = None
    rate: float | None = None
    error: str | None = None


def probe(
    name: str = config.DEVICE_NAME,
    channels: int = config.CHANNELS,
    rate: int = config.SAMPLE_RATE,
) -> DeviceStatus:
    """Check whether the configured array is visible, without opening it.

    Args:
        name: Substring matched against the device's name.
        channels: Channels a real open would ask for.
        rate: Sample rate a real open would ask for.

    Returns:
        What :func:`_resolve_device` would choose right now.
    """
    try:
        index = _resolve_device(name, channels, rate)
    except DeviceNotFound as error:
        return DeviceStatus(connected=False, error=str(error))
    device = sd.query_devices(index)
    host_api = str(sd.query_hostapis()[device["hostapi"]]["name"])
    return DeviceStatus(
        connected=True,
        name=str(device["name"]),
        host_api=host_api,
        channels=int(device["max_input_channels"]),
        rate=float(device["default_samplerate"]),
    )


def rescan() -> None:
    """Make PortAudio enumerate devices again.

    PortAudio takes its device list once, at initialisation. Call only with
    no capture open: an open stream would be pulled from under its reader.
    Uses ``sounddevice``'s private ``_terminate`` / ``_initialize``, thin
    wrappers around ``Pa_Terminate`` / ``Pa_Initialize``.
    """
    sd._terminate()
    sd._initialize()
