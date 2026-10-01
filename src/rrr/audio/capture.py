"""Reading the array: one open device, one ring buffer, two ways out.

The device is opened once (ALSA allows one process) and read on a background
thread into a ring buffer, with two ways out:

* :meth:`AudioTap.latest` returns the most recent N seconds, for analysis.
* :meth:`AudioTap.stream` walks forward from a cursor and reports what was
  dropped, for delivery and recording.

Times come from PortAudio's ``inputBufferAdcTime``, not the callback's
``time.monotonic()``, which runs one block (64 ms at 1024 samples) late; when
the host API does not fill it in, the callback time is used and logged.
"""

from __future__ import annotations

import collections
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

from . import config
from .types import BlockStamp, Chunk, Window

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


class DeviceNotFound(RuntimeError):
    """Raised when no capture device matches the configured name."""


class AudioTap:
    """Keep a rolling window of the array's audio available.

    Reference counted: the device opens with the first consumer and closes
    shortly after the last leaves. A failure is not retried until
    :meth:`reconnect` (docs/decisions.md 29). Consumers must not modify
    published arrays in place.
    """

    def __init__(
        self,
        device: str = config.DEVICE_NAME,
        rate: int = config.SAMPLE_RATE,
        channels: int = config.CHANNELS,
        block_size: int = config.BLOCK_SIZE,
        window_s: float = config.WINDOW_S,
    ) -> None:
        """Initialise the tap without opening the device.

        Args:
            device: Substring matched against the input device's name.
            rate: Sample rate to ask for.
            channels: Channels to ask for. A device offering fewer is
                reported rather than used.
            block_size: Frames per callback.
            window_s: Seconds of audio to keep.
        """
        self._device = device
        self._rate = rate
        self._channels = channels
        self._block_size = block_size
        self._capacity = max(1, int(window_s * rate))

        self._lock = threading.Lock()
        self._updated = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._users = 0
        self._released_at = 0.0
        #: Set when capture fails, and kept until reconnect().
        self._failed = False
        #: Why capture last failed, cleared along with _failed.
        self._error: str | None = None
        self._overruns = 0

        # _written counts samples ever written, so it doubles as the stream
        # cursor and as a "has anything arrived yet" test.
        self._ring = np.zeros((self._capacity, channels), dtype=np.float32)
        self._position = 0
        self._written = 0
        self._index = 0
        self._captured_at = 0.0

        # One stamp per block, covering the whole ring plus two spare for the
        # block being written and rounding.
        self._stamps: collections.deque[BlockStamp] = collections.deque(
            maxlen=math.ceil(self._capacity / max(1, block_size)) + 2
        )
        #: Whether the missing-ADC-time fallback has been logged (once only).
        self._adc_warned = False
        #: `now - reported` from the first valid blocks, for calibration.
        self._domain_lags: list[float] = []
        #: Whether calibration has decided.
        self._domain_calibrated = False
        #: Correction added to `reported` when the domain is stable-but-offset.
        self._adc_offset = 0.0
        #: Set when calibration finds the ADC clock incoherent; every later
        #: block uses the callback clock (docs/decisions.md 20).
        self._adc_bad_domain = False

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        """Register a consumer, opening the device unless it is open or failed."""
        with self._lock:
            self._users += 1
            self._ensure_thread()

    def reconnect(self) -> None:
        """Clear a failure and, if anyone is waiting, open the device again.

        Nothing calls this automatically (docs/decisions.md 29). A device
        plugged in since PortAudio was initialised needs :func:`rescan` first.
        """
        with self._lock:
            self._failed = False
            self._error = None
            if self._users > 0:
                self._ensure_thread()

    def _ensure_thread(self) -> None:
        """Start the reader thread unless it is running or has failed.

        Holds _lock.
        """
        if self._failed:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="audio-tap", daemon=True
        )
        self._thread.start()

    def release(self) -> None:
        """Deregister a consumer; the device closes after an idle period."""
        with self._lock:
            self._users = max(0, self._users - 1)
            self._released_at = time.monotonic()

    @property
    def active(self) -> bool:
        """Whether the reader thread is currently running."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def failed(self) -> bool:
        """Whether capture failed and is waiting for :meth:`reconnect`."""
        with self._lock:
            return self._failed

    @property
    def error(self) -> str | None:
        """Why capture failed, while :attr:`failed` is set; else None."""
        with self._lock:
            return self._error

    @property
    def overruns(self) -> int:
        """How often the driver reported dropped input since the last open."""
        with self._lock:
            return self._overruns

    @property
    def rate(self) -> int:
        """Sample rate in Hz."""
        return self._rate

    @property
    def channels(self) -> int:
        """Number of channels being captured."""
        return self._channels

    @property
    def block_size(self) -> int:
        """Frames per callback, which is the resolution of a block stamp."""
        return self._block_size

    @property
    def cursor(self) -> int:
        """Total samples captured so far. A starting point for :meth:`stream`."""
        with self._lock:
            return self._written

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop capture now, without waiting out the idle period.

        Call before exiting: the daemon reader thread would otherwise never
        close the device, which can make the next open fail.

        Args:
            timeout: Seconds to wait for the thread to finish.
        """
        with self._lock:
            self._users = 0
            # Backdated so the idle check fires on the reader's next pass.
            self._released_at = 0.0
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def __enter__(self) -> AudioTap:
        """Acquire the tap for the duration of a ``with`` block."""
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        """Release the tap."""
        self.release()

    # -- reading -----------------------------------------------------------

    def latest(
        self, seconds: float | None = None, timeout: float = 5.0, after: int = 0
    ) -> Window | None:
        """Return the most recent audio, waiting for new samples if necessary.

        Args:
            seconds: How much history to return, capped at what is kept. None
                asks for everything available.
            timeout: Seconds to wait for audio newer than ``after``.
            after: Only return a window whose index exceeds this value.

        Returns:
            The window, or None if no new audio arrived within the timeout -
            at once, without waiting, while the tap has failed.
        """
        with self._updated:
            if not self._wait_for(lambda: self._index > after, timeout):
                return None

            available = min(self._written, self._capacity)
            if available == 0:
                return None
            wanted = available if seconds is None else int(seconds * self._rate)
            count = max(1, min(available, wanted))
            return Window(
                samples=self._unwrap(self._written - count, count),
                rate=self._rate,
                index=self._index,
                captured_at=self._captured_at,
            )

    def stream(self, cursor: int, timeout: float = 1.0) -> Chunk | None:
        """Return everything captured since ``cursor``.

        Args:
            cursor: Sample count to continue from, as returned by a previous
                chunk or by :attr:`cursor`.
            timeout: Seconds to wait for samples beyond ``cursor``.

        Returns:
            The chunk, or None if nothing new arrived within the timeout -
            at once, without waiting, while the tap has failed.
        """
        with self._updated:
            if not self._wait_for(lambda: self._written > cursor, timeout):
                return None

            available = min(self._written, self._capacity)
            oldest = self._written - available
            # A reader slower than the ring loses the difference; report it.
            dropped = max(0, oldest - cursor)
            start = max(cursor, oldest)
            count = self._written - start
            if count <= 0:
                return None
            return Chunk(
                samples=self._unwrap(start, count),
                rate=self._rate,
                cursor=self._written,
                dropped=dropped,
                captured_at=self._captured_at,
                # Every block this chunk overlaps, so a gap between two shows.
                stamps=tuple(
                    stamp for stamp in self._stamps if stamp.end_sample > start
                ),
            )

    def _wait_for(self, ready, timeout: float) -> bool:
        """Wait on the condition variable until ``ready()`` or the timeout.

        The caller must hold ``self._updated``. Gives up at once while the tap
        has failed.
        """
        deadline = time.monotonic() + timeout
        while not ready():
            if self._failed:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._updated.wait(remaining)
        return True

    def _unwrap(self, start: int, count: int) -> np.ndarray:
        """Copy ``count`` samples ending at the write head, oldest first.

        Args:
            start: Absolute sample index to begin at.
            count: How many samples to copy.

        Returns:
            A fresh ``(count, channels)`` array. The caller must hold the lock.
        """
        begin = start % self._capacity
        if begin + count <= self._capacity:
            return self._ring[begin : begin + count].copy()
        split = self._capacity - begin
        return np.concatenate((self._ring[begin:], self._ring[: count - split]))

    # -- reader thread -----------------------------------------------------

    def _should_stop(self) -> bool:
        with self._lock:
            if self._users > 0:
                return False
            return time.monotonic() - self._released_at > config.IDLE_SHUTDOWN_S

    def _publish(self, block: np.ndarray, adc_time: float) -> None:
        """Append one callback's worth of samples to the ring buffer.

        Args:
            block: ``(n, channels)`` float32 samples, oldest first.
            adc_time: ADC time of the block's first sample, on CLOCK_MONOTONIC.
        """
        if block.shape[0] == 0:
            return
        # A block larger than the ring keeps its newest part, which starts
        # that much later.
        if block.shape[0] > self._capacity:
            adc_time += (block.shape[0] - self._capacity) / self._rate
            block = block[-self._capacity :]

        with self._updated:
            count = block.shape[0]
            self._stamps.append(
                BlockStamp(sample=self._written, monotonic=adc_time, frames=count)
            )
            end = self._position + count
            if end <= self._capacity:
                self._ring[self._position : end] = block
            else:
                split = self._capacity - self._position
                self._ring[self._position :] = block[:split]
                self._ring[: count - split] = block[split:]
            self._position = end % self._capacity
            self._written += count
            self._index += 1
            self._captured_at = adc_time + count / self._rate
            self._updated.notify_all()

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """PortAudio callback. Runs on PortAudio's thread, so it stays short."""
        if status.input_overflow:
            with self._lock:
                self._overruns += 1
        self._publish(
            indata.astype(np.float32) * _INT16_SCALE,
            self._adc_time(frames, time_info.inputBufferAdcTime),
        )

    def _adc_time(self, frames: int, reported: float) -> float:
        """Return a usable ADC time for a block, calibrating once at the start.

        Args:
            frames: Samples in the block.
            reported: PortAudio's ``inputBufferAdcTime``.

        Returns:
            The ADC time of the block's first sample, on the same clock as
            ``time.monotonic()``.

        The reported value is used as-is, corrected by a fixed offset, or
        replaced by the callback clock, as decided over the first
        :data:`_DOMAIN_CALIBRATION_BLOCKS` valid blocks, which are themselves
        timed from the callback clock. See docs/decisions.md 20 and 26.
        """
        expected_lag = frames / self._rate
        now = time.monotonic()

        if reported <= 0.0:
            if not self._adc_warned:
                logger.warning(
                    "PortAudio did not report inputBufferAdcTime; timing audio "
                    "from the callback instead, which is %.1f ms coarser",
                    expected_lag * 1000.0,
                )
                self._adc_warned = True
            return now - expected_lag

        if not self._domain_calibrated:
            self._domain_lags.append(now - reported)
            if len(self._domain_lags) < _DOMAIN_CALIBRATION_BLOCKS:
                return now - expected_lag
            self._domain_calibrated = True
            lags = np.array(self._domain_lags)
            mean_lag = float(lags.mean())
            spread = float(lags.std())
            if -expected_lag <= mean_lag <= 3.0 * expected_lag:
                logger.info(
                    "inputBufferAdcTime agrees with time.monotonic(): "
                    "callback runs %.1f ms after the block's first sample on "
                    "average over %d blocks (block is %.1f ms)",
                    mean_lag * 1000.0,
                    len(lags),
                    expected_lag * 1000.0,
                )
            elif spread < _DOMAIN_STABILITY_S:
                self._adc_offset = mean_lag - expected_lag
                logger.warning(
                    "inputBufferAdcTime is %.3f s from the callback clock on "
                    "average over %d blocks, but stable there (+/-%.1f ms): "
                    "correcting for the fixed offset rather than discarding "
                    "the ADC clock's own timing",
                    mean_lag,
                    len(lags),
                    spread * 1000.0,
                )
            else:
                self._adc_bad_domain = True
                logger.warning(
                    "inputBufferAdcTime is %.3f s from the callback clock over "
                    "%d blocks and not even stable there (+/-%.3f s): the two "
                    "are not readable as one clock. Falling back to the "
                    "callback clock for the rest of this recording, which is "
                    "%.1f ms coarser",
                    mean_lag,
                    len(lags),
                    spread,
                    expected_lag * 1000.0,
                )

        if self._adc_bad_domain:
            return now - expected_lag
        return reported + self._adc_offset

    def _run(self) -> None:
        logger.info("audio tap starting on device matching %r", self._device)
        # Once per thread lifetime (docs/decisions.md 25).
        com_ready = _com_initialize()
        failure: str | None = None
        try:
            if not self._should_stop():
                try:
                    index = _resolve_device(self._device, self._channels, self._rate)
                    with self._lock:
                        self._overruns = 0
                    # int16 is the endpoint's wire format; convert here.
                    with sd.InputStream(
                        device=index,
                        channels=self._channels,
                        samplerate=self._rate,
                        dtype="int16",
                        blocksize=self._block_size,
                        callback=self._callback,
                    ):
                        logger.info(
                            "capturing %d ch at %d Hz", self._channels, self._rate
                        )
                        while not self._should_stop():
                            time.sleep(0.1)
                except Exception as error:  # noqa: BLE001 - reported, not raised
                    logger.warning("capture failed, not retrying: %s", error)
                    failure = str(error)
        finally:
            if com_ready:
                _com_uninitialize()

        logger.info("audio tap stopped")
        with self._updated:
            if failure is not None:
                self._failed = True
                self._error = failure
                # Detach so a reconnect during return starts a fresh reader.
                if self._thread is threading.current_thread():
                    self._thread = None
            self._updated.notify_all()


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
    every tap stopped: an open stream would be pulled from under its reader.
    Uses ``sounddevice``'s private ``_terminate`` / ``_initialize``, thin
    wrappers around ``Pa_Terminate`` / ``Pa_Initialize``.
    """
    sd._terminate()
    sd._initialize()


def devices() -> list[dict[str, object]]:
    """List the capture devices PortAudio can see.

    Returns:
        One entry per input device with its index, name and channel count.
    """
    return [
        {
            "index": index,
            "name": str(device["name"]),
            "channels": int(device["max_input_channels"]),
            "rate": float(device["default_samplerate"]),
        }
        for index, device in enumerate(sd.query_devices())
        if device["max_input_channels"] > 0
    ]
