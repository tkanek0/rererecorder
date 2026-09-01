"""Reading the array: one open device, one ring buffer, two ways out.

The device is opened once and read on a background thread into a ring buffer.
Two ways out of that buffer exist, because the two consumers want different
things:

* :meth:`AudioTap.latest` hands back the most recent N seconds, whatever has
  been read since last time. Analysis wants this - it does not care about the
  audio it missed while it was busy.
* :meth:`AudioTap.stream` walks forward from a cursor and reports how much was
  dropped if the reader fell behind. Delivery wants this - a gap that goes
  unmentioned becomes a click in the browser and a mystery in the log.

Both are fed by the same reader, so opening the device twice is never needed:
ALSA hands it to one process at a time.

Times come from PortAudio's ``inputBufferAdcTime``, not from ``time.monotonic()``
in the callback. Measured on a ReSpeaker at 16 kHz with 1024-sample blocks: the
callback runs 64.0 ms after the block's first sample entered the converter -
exactly one block, with 0.35 ms of jitter - and PortAudio's ALSA backend shares
its clock origin with ``time.monotonic()``. Stamping the callback instead, as the
upstream respeaker-playground does, puts every sample 64 ms late and offers no
way to say where inside a block a sample sits. Neither is acceptable when the
audio has to line up with video that timestamps its own frames.

Not every PortAudio host API fills that field in. When it is missing the callback
time is used instead, corrected by the block length, and the substitution is
logged rather than passed off as a measurement.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time

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


class DeviceNotFound(RuntimeError):
    """Raised when no capture device matches the configured name."""


class AudioTap:
    """Keep a rolling window of the array's audio available.

    Reference counted: the device is opened when the first consumer arrives and
    closed shortly after the last one leaves, so an idle process does not hold
    a device that only one process may have.

    The published arrays are owned by nobody and read by everyone; consumers
    must not modify them in place.
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
            channels: Channels to ask for. The 6-channel firmware offers six;
                a device offering fewer is reported rather than silently used,
                because every channel index downstream would be wrong.
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
        self._error: str | None = None
        self._overruns = 0

        # _written counts samples ever written, so it doubles as the stream
        # cursor and as a "has anything arrived yet" test.
        self._ring = np.zeros((self._capacity, channels), dtype=np.float32)
        self._position = 0
        self._written = 0
        self._index = 0
        self._captured_at = 0.0

        # One stamp per block, covering at least as much history as the ring
        # itself so that a reader who gets the oldest available samples still
        # gets the times that go with them. Two spare for the block being
        # written and rounding.
        self._stamps: collections.deque[BlockStamp] = collections.deque(
            maxlen=math.ceil(self._capacity / max(1, block_size)) + 2
        )
        #: Whether inputBufferAdcTime has been checked against time.monotonic().
        self._adc_checked = False
        #: Whether the fallback has already been reported. Logged once: it would
        #: otherwise repeat 15 times a second.
        self._adc_warned = False

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        """Register a consumer, opening the device if it is not open."""
        with self._lock:
            self._users += 1
            if self._thread is None or not self._thread.is_alive():
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
    def error(self) -> str | None:
        """The most recent capture error, if any."""
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

        The reader runs on a daemon thread, so a process that exits while it
        is still open never closes the device, which can leave it in a state where the next open fails. Long-running consumers can rely on
        the idle timeout; anything that is about to exit should call this.

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
            after: Only return a window whose index exceeds this value. Pass the
                index you last processed to avoid analysing the same audio
                twice.

        Returns:
            The window, or None if no new audio arrived within the timeout.
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
            The chunk, or None if nothing new arrived within the timeout. A
            chunk's ``dropped`` says how many samples were overwritten before
            this reader reached them, and its ``stamps`` say when each block it
            spans was captured.
        """
        with self._updated:
            if not self._wait_for(lambda: self._written > cursor, timeout):
                return None

            available = min(self._written, self._capacity)
            oldest = self._written - available
            # A reader slower than the ring is long loses the difference. Say
            # how much rather than papering over the seam.
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
                # Every block this chunk overlaps, so a recorder can place each
                # one and see a gap between two of them. A chunk can span many
                # blocks when the reader was busy, and one time for the whole
                # chunk could not describe that.
                stamps=tuple(
                    stamp for stamp in self._stamps if stamp.end_sample > start
                ),
            )

    def _wait_for(self, ready, timeout: float) -> bool:
        """Wait on the condition variable until ``ready()`` or the timeout.

        The caller must hold ``self._updated``.
        """
        deadline = time.monotonic() + timeout
        while not ready():
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
        # Only possible if a driver hiccup delivers more than the whole window
        # at once; keep the newest part of it. The discarded samples are the
        # oldest, so the surviving block starts that much later.
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
        """Return a usable ADC time for a block, checking the reported one once.

        Args:
            frames: Samples in the block.
            reported: PortAudio's ``inputBufferAdcTime``.

        Returns:
            The ADC time of the block's first sample, on the same clock as
            ``time.monotonic()``.

        Two things can be wrong with what PortAudio reports, and both are
        checked here rather than discovered later in a recording that will not
        line up:

        * the field is not filled in at all, which some host APIs do,
        * it is filled in from a clock with a different origin, which would put
          every audio sample somewhere else entirely.

        The check runs on the first block only. It compares the reported time
        against the callback's own clock, which must agree to within about one
        block; measured on a ReSpeaker the difference is the block length to
        within 0.35 ms.
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

        if not self._adc_checked:
            self._adc_checked = True
            lag = now - reported
            # One block of slack either side: the callback runs after the block
            # is complete, and the scheduler can add to that.
            if not (-expected_lag <= lag <= 3.0 * expected_lag):
                logger.warning(
                    "inputBufferAdcTime is %.3f s from the callback clock, not "
                    "the expected %.3f s: the two are probably not the same "
                    "clock. Audio times cannot be compared with video times "
                    "until this is understood",
                    lag,
                    expected_lag,
                )
            else:
                logger.info(
                    "inputBufferAdcTime agrees with time.monotonic(): "
                    "callback runs %.1f ms after the block's first sample "
                    "(block is %.1f ms)",
                    lag * 1000.0,
                    expected_lag * 1000.0,
                )
        return reported

    def _run(self) -> None:
        logger.info("audio tap starting on device matching %r", self._device)
        while not self._should_stop():
            try:
                index = _resolve_device(self._device, self._channels)
                with self._lock:
                    self._error = None
                    self._overruns = 0
                # int16 rather than float32: the array's endpoint is 16 bit, so
                # this is the format on the wire and the conversion is ours to
                # see rather than PortAudio's to hide.
                with sd.InputStream(
                    device=index,
                    channels=self._channels,
                    samplerate=self._rate,
                    dtype="int16",
                    blocksize=self._block_size,
                    callback=self._callback,
                ):
                    logger.info("capturing %d ch at %d Hz", self._channels, self._rate)
                    while not self._should_stop():
                        time.sleep(0.1)
            except Exception as error:  # noqa: BLE001 - any device failure retries
                logger.warning("capture failed: %s", error)
                with self._lock:
                    self._error = str(error)
                if self._should_stop():
                    break
                time.sleep(config.RECONNECT_DELAY_S)

        logger.info("audio tap stopped")
        with self._updated:
            self._updated.notify_all()


def _resolve_device(name: str, channels: int) -> int:
    """Find the input device whose name contains ``name``.

    Args:
        name: Substring to look for, case-insensitively.
        channels: Channel count the caller intends to open.

    Returns:
        The PortAudio device index.

    Raises:
        DeviceNotFound: If nothing matches, or if the match cannot supply the
            requested channels - which is what a 1-channel firmware looks like
            from here.
    """
    needle = name.lower()
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] <= 0:
            continue
        if needle not in str(device["name"]).lower():
            continue
        if device["max_input_channels"] < channels:
            raise DeviceNotFound(
                f"{device['name']!r} offers {device['max_input_channels']} input "
                f"channels, not {channels}. The 1-channel firmware looks like "
                "this; flash the 6-channel one to get the raw microphones."
            )
        return index
    raise DeviceNotFound(
        f"no capture device whose name contains {name!r}. Plugged in? "
        "`arecord -l` lists what the system can see."
    )


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
