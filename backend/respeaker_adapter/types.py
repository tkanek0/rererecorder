"""The shapes audio travels in, and the conventions that go with them.

Kept apart from :mod:`respeaker_adapter.capture` so offline work imports neither
PortAudio nor a device. Samples are float32 in [-1, 1], ``(n, channels)``,
oldest first. Times are ``CLOCK_MONOTONIC``, from ``inputBufferAdcTime``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import config

#: Below this a signal is silence rather than a level; well under the raw
#: microphones' noise floor (about -45 dBFS in a quiet room).
SILENCE = 1e-7


@dataclass(frozen=True)
class BlockStamp:
    """When one capture block's first sample entered the converter.

    One per PortAudio callback; a reader fits a line through them rather than
    trusting the nominal rate.

    Attributes:
        sample: Absolute count of samples captured before this block. A jump
            means the ring buffer overwrote audio before a reader reached it.
        monotonic: ADC time of the block's first sample. A jump past the
            previous block's end means the driver dropped input, which leaves
            the sample count continuous.
        frames: Samples in the block.
    """

    sample: int
    monotonic: float
    frames: int

    @property
    def end_sample(self) -> int:
        """Absolute index just past this block's last sample."""
        return self.sample + self.frames

    def end_monotonic(self, rate: int) -> float:
        """When the sample just past this block would have been captured.

        Args:
            rate: Sample rate in Hz.

        Returns:
            The expected ADC time of the next block's first sample.
        """
        return self.monotonic + self.frames / rate


@dataclass(frozen=True)
class Window:
    """A slice of audio, all channels.

    Attributes:
        samples: ``(n, channels)`` float32 in [-1, 1], oldest sample first.
        rate: Sample rate in Hz.
        index: Counter of publishes, used to detect new audio. Zero for a
            window that did not come from a live tap.
        captured_at: ADC time of the newest sample, or zero for a window read
            from a file.
    """

    samples: np.ndarray
    rate: int
    index: int = 0
    captured_at: float = 0.0

    @property
    def duration(self) -> float:
        """Length of the window in seconds."""
        return len(self.samples) / self.rate

    @property
    def channels(self) -> int:
        """Number of channels the window carries."""
        return int(self.samples.shape[1])

    @property
    def processed(self) -> np.ndarray:
        """The beamformed, echo-cancelled channel the chip produces."""
        return self.samples[:, config.CHANNEL_PROCESSED]

    @property
    def mics(self) -> np.ndarray:
        """The four raw microphones as ``(n, 4)``, in board order."""
        return self.samples[:, list(config.CHANNEL_MICS)]

    @property
    def playback(self) -> np.ndarray:
        """Loopback of what was played out; silent unless something is."""
        return self.samples[:, config.CHANNEL_PLAYBACK]

    def tail(self, seconds: float) -> Window:
        """Return the last ``seconds`` of this window.

        Args:
            seconds: How much to keep. More than the window holds returns the
                whole window.

        Returns:
            A new window sharing this one's buffer.
        """
        count = max(1, int(self.rate * seconds))
        if count >= len(self.samples):
            return self
        return Window(
            samples=self.samples[-count:],
            rate=self.rate,
            index=self.index,
            captured_at=self.captured_at,
        )


@dataclass(frozen=True)
class Chunk:
    """Audio read forward from a cursor, for delivery rather than analysis.

    Attributes:
        samples: ``(n, channels)`` float32 in [-1, 1], oldest sample first.
        rate: Sample rate in Hz.
        cursor: Total samples captured once this chunk is consumed. Pass it back
            to continue from here.
        dropped: Samples overwritten before this reader reached them.
        captured_at: ADC time of the chunk's newest sample.
        stamps: One entry per capture block this chunk spans, oldest first;
            empty only for a chunk built by hand in a test.
    """

    samples: np.ndarray
    rate: int
    cursor: int
    dropped: int
    captured_at: float
    stamps: tuple[BlockStamp, ...] = ()

    @property
    def first_sample(self) -> int:
        """Absolute index of this chunk's first sample."""
        return self.cursor - len(self.samples)


def dbfs(amplitude: float) -> float | None:
    """Convert a linear amplitude to dBFS.

    Args:
        amplitude: Linear amplitude, where 1.0 is full scale.

    Returns:
        The level in dBFS, or None for silence (JSON has no -inf).
    """
    if amplitude <= SILENCE:
        return None
    return round(20.0 * math.log10(amplitude), 2)


def rms(samples: np.ndarray) -> np.ndarray:
    """Root mean square of each channel.

    Args:
        samples: ``(n,)`` or ``(n, channels)`` float32.

    Returns:
        A scalar array for mono input, otherwise one value per channel.
    """
    return np.sqrt(np.mean(np.square(samples, dtype=np.float64), axis=0))
