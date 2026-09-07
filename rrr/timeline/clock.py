"""How the host's two clocks relate, measured rather than assumed.

Two devices are being recorded and they hand out times on different clocks:

* The camera's frame timestamps are **epoch milliseconds**. librealsense enables
  ``global_time`` by default and fits the device's own clock onto the host's
  ``CLOCK_REALTIME``, so what comes out of ``frame.get_timestamp()`` is directly
  comparable to ``time.time()``. Measured on a D455: the fit is re-estimated
  while streaming, which moves the mapping by about 10 ms over a minute.
* The array's audio timestamps are ``CLOCK_MONOTONIC``. PortAudio's
  ``inputBufferAdcTime`` shares an origin with ``time.monotonic()`` - measured on
  a ReSpeaker at 16 kHz with 1024-sample blocks, the two differ by exactly one
  block (64.0 ms, std 0.35 ms), which is the block's own length and not an
  offset between clocks.

``CLOCK_MONOTONIC`` is the axis everything is converted to. It is the one that
cannot be moved: an NTP step correction changes ``CLOCK_REALTIME`` underneath a
running recording, and a session whose timeline jumps backwards halfway through
is not repairable afterwards. Realtime is still recorded - it is what says when
the session happened - but as a series of measured pairs rather than as the axis
itself.

Nothing here imports a device, numpy or a web framework. It is the piece most
worth testing and the piece that has to be right for anything else to mean
anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ClockPair:
    """Both host clocks, read as close together as the interpreter allows.

    Attributes:
        monotonic: ``time.monotonic()`` in seconds.
        realtime: ``time.time()`` in seconds since the epoch.
        uncertainty: How long the pair of reads took, in seconds. The two
            clocks cannot be read at the same instant, so this bounds how wrong
            the pairing can be. Recorded rather than dropped because a pair
            taken while the process was descheduled is worth distrusting.
    """

    monotonic: float
    realtime: float
    uncertainty: float = 0.0

    @property
    def offset(self) -> float:
        """Seconds to add to a monotonic reading to get a realtime one."""
        return self.realtime - self.monotonic

    def to_monotonic(self, realtime: float) -> float:
        """Convert a realtime reading onto the monotonic axis.

        Args:
            realtime: Seconds since the epoch.

        Returns:
            The same instant as ``time.monotonic()`` would have reported it.
        """
        return realtime - self.offset

    def to_realtime(self, monotonic: float) -> float:
        """Convert a monotonic reading to seconds since the epoch.

        Args:
            monotonic: A ``time.monotonic()`` reading.

        Returns:
            The same instant as ``time.time()`` would have reported it.
        """
        return monotonic + self.offset

    def epoch_ms_to_monotonic(self, epoch_ms: float) -> float:
        """Convert a camera frame timestamp onto the monotonic axis.

        Args:
            epoch_ms: Milliseconds since the epoch, as
                ``frame.get_timestamp()`` reports them while the timestamp
                domain is ``global_time``.

        Returns:
            The frame's instant on the monotonic axis.
        """
        return self.to_monotonic(epoch_ms / 1000.0)

    def as_dict(self) -> dict[str, float]:
        """Return a JSON-serialisable view of this pair."""
        return {
            "monotonic": self.monotonic,
            "realtime": self.realtime,
            "uncertainty": self.uncertainty,
        }

    @staticmethod
    def from_dict(raw: dict[str, float]) -> ClockPair:
        """Rebuild a pair from its stored form.

        Args:
            raw: A mapping as :meth:`as_dict` produced.

        Returns:
            The pair.
        """
        return ClockPair(
            monotonic=float(raw["monotonic"]),
            realtime=float(raw["realtime"]),
            uncertainty=float(raw.get("uncertainty", 0.0)),
        )


def read_clocks() -> ClockPair:
    """Read both host clocks, bounding how far apart the readings were taken.

    The realtime read is sandwiched between two monotonic reads and the midpoint
    is kept, so the pairing error is measured - it lands in ``uncertainty`` -
    rather than assumed to be zero. Typically well under 10 microseconds, and
    occasionally much worse if the scheduler intervenes, which is exactly the
    case worth being able to see.

    Returns:
        The pair.
    """
    before = time.monotonic()
    realtime = time.time()
    after = time.monotonic()
    return ClockPair(
        monotonic=(before + after) / 2.0,
        realtime=realtime,
        uncertainty=after - before,
    )


class ClockTrack:
    """A series of clock pairs taken across a session.

    A single pair, taken when the recording started, is enough to convert
    between the axes - but only if the offset holds still, and it does not: NTP
    slews ``CLOCK_REALTIME`` continuously, and can step it. Sampling throughout
    means the camera's epoch timestamps can be converted using the offset that
    was in force at the time, and means a step correction is visible afterwards
    instead of silently smearing the timeline.

    Not thread safe. One recorder owns one track.
    """

    def __init__(self, interval_s: float = 1.0) -> None:
        """Start an empty track.

        Args:
            interval_s: Minimum seconds between kept samples. One second costs
                3.6k entries an hour - negligible beside the frames - and is
                fine enough to catch a step correction.
        """
        self._interval_s = interval_s
        self._samples: list[ClockPair] = []

    def sample(self, force: bool = False) -> ClockPair:
        """Read the clocks, keeping the reading if enough time has passed.

        Args:
            force: Keep the reading whatever the interval says. Used for the
                first and last samples of a session, which anchor it.

        Returns:
            The pair just read, whether or not it was kept.
        """
        pair = read_clocks()
        if force or not self._samples:
            self._samples.append(pair)
        elif pair.monotonic - self._samples[-1].monotonic >= self._interval_s:
            self._samples.append(pair)
        return pair

    @property
    def samples(self) -> list[ClockPair]:
        """The kept samples, oldest first."""
        return list(self._samples)

    @property
    def latest(self) -> ClockPair | None:
        """The most recently kept sample, or None if there are none."""
        return self._samples[-1] if self._samples else None

    def at(self, monotonic: float) -> ClockPair | None:
        """Return the kept sample in force at a monotonic instant.

        Args:
            monotonic: The instant to look up.

        Returns:
            The latest sample taken at or before ``monotonic``, falling back to
            the earliest sample if the instant precedes all of them, or None if
            the track is empty.

        A step correction makes interpolation across it meaningless, so this
        picks a sample rather than blending two.
        """
        if not self._samples:
            return None
        chosen = self._samples[0]
        for pair in self._samples:
            if pair.monotonic > monotonic:
                break
            chosen = pair
        return chosen

    @property
    def drift_ppm(self) -> float | None:
        """How fast the offset moved across the track, in parts per million.

        Returns:
            The change in offset divided by the monotonic span, or None if the
            track is too short or too brief to say. A value of a few tens is
            ordinary NTP slew; a large one means the realtime clock was stepped
            and epoch timestamps either side of it should not be compared.
        """
        if len(self._samples) < 2:
            return None
        span = self._samples[-1].monotonic - self._samples[0].monotonic
        if span <= 0:
            return None
        drift = self._samples[-1].offset - self._samples[0].offset
        return drift / span * 1e6

    def as_list(self) -> list[dict[str, float]]:
        """Return a JSON-serialisable view of every kept sample."""
        return [pair.as_dict() for pair in self._samples]

    @staticmethod
    def from_list(raw: list[dict[str, float]], interval_s: float = 1.0) -> ClockTrack:
        """Rebuild a track from its stored form.

        Args:
            raw: A list as :meth:`as_list` produced.
            interval_s: Interval to report for the rebuilt track.

        Returns:
            The track.
        """
        track = ClockTrack(interval_s=interval_s)
        track._samples = [ClockPair.from_dict(entry) for entry in raw]
        return track
