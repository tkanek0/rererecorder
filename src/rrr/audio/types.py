"""The shapes audio travels in, and the conventions that go with them.

Kept apart from :mod:`respeaker.capture` so that analysis and offline work
import neither PortAudio nor a device: a ``Window`` built from a WAV file is
the same thing a live one is, and every processor takes both without knowing
which it got.

Samples are float32 in [-1, 1] throughout, laid out ``(n, channels)`` with the
oldest sample first.

Every time in here is ``CLOCK_MONOTONIC``, and comes from PortAudio's
``inputBufferAdcTime`` rather than from ``time.monotonic()`` at the moment the
callback ran. The two differ by a whole block - measured at 64.0 ms for 1024
samples at 16 kHz, with 0.35 ms of jitter - and the difference is the whole
reason a recording can be lined up against video at all. The upstream
respeaker-playground stamps the callback instead, which is 64 ms late and says
nothing about where inside the block a sample sits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import config

#: Below this a signal is called silence rather than assigned a level. It sits
#: well under the array's own noise floor, which measures around -45 dBFS on the
#: raw microphones in a quiet room.
SILENCE = 1e-7


@dataclass(frozen=True)
class BlockStamp:
    """When one capture block's first sample entered the converter.

    One of these per PortAudio callback. They are what turns a position in a
    file into a time: the recorder writes them out beside the audio, and
    anything reading the recording fits a line through them rather than trusting
    the nominal sample rate.

    Attributes:
        sample: Absolute count of samples the tap had captured before this
            block. Continuous while capture is; a jump means the ring buffer
            overwrote audio before a reader reached it.
        monotonic: ``inputBufferAdcTime`` of the block's first sample. A jump
            relative to the previous block's end means the driver dropped input
            - the callback simply is not called for what was lost, so the sample
            count stays continuous while time does not. Both failures have to be
            detectable, which is why both fields are here.
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
            The expected ADC time of the next block's first sample. Comparing
            this with the next block's actual ``monotonic`` is how dropped input
            is found.
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
        dropped: Samples overwritten before this reader reached them. Non-zero
            means the reader is not keeping up.
        captured_at: ADC time of the chunk's newest sample.
        stamps: One entry per capture block this chunk spans, oldest first.
            Empty only for a chunk built by hand in a test. A recorder needs
            these rather than ``captured_at``: a chunk can span many blocks, and
            a single time for all of them cannot describe a gap in the middle.
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
        The level in dBFS, or None for silence - JSON has no way to spell
        negative infinity, and a meter needs to know the difference between
        "very quiet" and "nothing at all".
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
