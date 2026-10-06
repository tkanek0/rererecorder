"""The array's audio, kept in a ring buffer with two ways out.

* :meth:`AudioTap.latest` returns the most recent N seconds, for a meter.
* :meth:`AudioTap.stream` walks forward from a cursor and reports what was
  dropped, for recording.

The device is opened once (ALSA allows one process) and read through
:class:`respeaker_adapter.Capture`.
"""

from __future__ import annotations

import collections
import math
import time

import numpy as np
from respeaker_adapter import BlockStamp, Capture, Chunk, Window

from .worker import SharedWorker

#: Seconds without a block after which capture counts as stalled. The array
#: has been seen to stop delivering while its PCM still reads RUNNING
#: (docs/features.md "The array").
STALL_S = 2.0


class AudioTap(SharedWorker[Window]):
    """Keep a rolling window of the array's audio available.

    Consumers must not modify published arrays in place.
    """

    name = "audio-tap"

    def __init__(
        self,
        idle_shutdown_s: float,
        *,
        device: str,
        rate: int,
        channels: int,
        block_size: int,
        window_s: float,
    ) -> None:
        """Prepare the tap without opening the device.

        Args:
            idle_shutdown_s: Seconds the device stays open with no consumers.
            device: Substring matched against the input device's name.
            rate: Sample rate to ask for.
            channels: Channels to ask for.
            block_size: Frames per callback.
            window_s: Seconds of audio to keep.
        """
        super().__init__(idle_shutdown_s)
        self._device = device
        self._rate = rate
        self._channels = channels
        self._block_size = block_size
        self._capacity = max(1, int(window_s * rate))
        self._capture: Capture | None = None
        # _written counts samples ever written, so it doubles as the cursor.
        self._ring = np.zeros((self._capacity, channels), dtype=np.float32)
        self._position = 0
        self._written = 0
        self._captured_at = 0.0
        # One stamp per block in the ring, plus the one being written.
        self._stamps: collections.deque[BlockStamp] = collections.deque(
            maxlen=math.ceil(self._capacity / max(1, block_size)) + 2
        )

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
    def overruns(self) -> int:
        """Input overflows the driver reported since the device was opened."""
        capture = self._capture
        return capture.overruns if capture is not None else 0

    @property
    def cursor(self) -> int:
        """Total samples captured so far. A starting point for :meth:`stream`."""
        with self._lock:
            return self._written

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
            The window, or None if no new audio arrived in time - at once while
            the tap has failed.
        """
        with self._updated:
            if not self._wait(lambda: self._index > max(after, self._floor), timeout):
                return None
            available = min(self._written, self._capacity)
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
            cursor: Sample count to continue from, as a previous chunk or
                :attr:`cursor` returned it.
            timeout: Seconds to wait for samples beyond ``cursor``.

        Returns:
            The chunk, or None if nothing new arrived in time - at once while
            the tap has failed.
        """
        with self._updated:
            if not self._wait(lambda: self._written > cursor, timeout):
                return None
            oldest = self._written - min(self._written, self._capacity)
            start = max(cursor, oldest)
            return Chunk(
                samples=self._unwrap(start, self._written - start),
                rate=self._rate,
                cursor=self._written,
                # A reader slower than the ring loses the difference.
                dropped=max(0, oldest - cursor),
                captured_at=self._captured_at,
                stamps=tuple(s for s in self._stamps if s.end_sample > start),
            )

    def _unwrap(self, start: int, count: int) -> np.ndarray:
        """Copy ``count`` samples from absolute index ``start``. Holds the lock."""
        begin = start % self._capacity
        if begin + count <= self._capacity:
            return self._ring[begin : begin + count].copy()
        split = self._capacity - begin
        return np.concatenate((self._ring[begin:], self._ring[: count - split]))

    def _on_block(self, block: np.ndarray, adc_time: float) -> None:
        """Append one block to the ring. Runs on PortAudio's thread."""
        if block.shape[0] > self._capacity:
            # Keep the newest part, which starts that much later.
            adc_time += (block.shape[0] - self._capacity) / self._rate
            block = block[-self._capacity :]
        count = block.shape[0]
        if count == 0:
            return
        with self._updated:
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

    def _serve(self) -> None:
        with Capture(
            self._on_block,
            device=self._device,
            rate=self._rate,
            channels=self._channels,
            block_size=self._block_size,
        ) as capture:
            self._capture = capture
            while not self._should_stop():
                time.sleep(0.1)
                silent = time.monotonic() - capture.last_block_at
                if silent > STALL_S:
                    # Reported before closing, so readers learn of it even if
                    # closing a stalled stream is slow.
                    self._fail(f"the array delivered no audio for {silent:.1f} s")
                    return
