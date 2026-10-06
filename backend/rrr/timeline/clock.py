"""How the host's two clocks relate, measured rather than assumed.

Camera frames carry epoch milliseconds, audio carries ``CLOCK_MONOTONIC``;
``CLOCK_MONOTONIC`` is the axis, and realtime is kept as measured pairs. See
docs/design.md "The one idea".
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
        uncertainty: How long the pair of reads took, in seconds, which
            bounds how wrong the pairing can be.
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
            uncertainty=float(raw["uncertainty"]),
        )


def read_clocks() -> ClockPair:
    """Read both host clocks, bounding how far apart the readings were taken.

    The realtime read sits between two monotonic reads; their midpoint is kept
    and their gap (typically under 10 us) becomes ``uncertainty``.

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

    Sampled throughout because NTP slews and can step ``CLOCK_REALTIME``, so
    the offset in force at each instant is known and a step stays visible.

    Not thread safe. One recorder owns one track.
    """

    def __init__(self, interval_s: float = 1.0) -> None:
        """Start an empty track.

        Args:
            interval_s: Minimum seconds between kept samples.
        """
        self._interval_s = interval_s
        self._samples: list[ClockPair] = []

    def sample(self, force: bool = False) -> ClockPair:
        """Read the clocks, keeping the reading if enough time has passed.

        Args:
            force: Keep the reading whatever the interval says, as for a
                session's first and last samples.

        Returns:
            The pair just read, whether or not it was kept.
        """
        pair = read_clocks()
        if force or not self._samples or pair.monotonic - self._samples[-1].monotonic >= self._interval_s:
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


def drift_ppm(samples: list[ClockPair]) -> float | None:
    """How fast the realtime-monotonic offset moved, in parts per million.

    Args:
        samples: Clock pairs, oldest first.

    Returns:
        The change in offset divided by the monotonic span, or None if the
        samples are too few to say. A few tens is ordinary NTP slew; a large
        value means the realtime clock was stepped.
    """
    if len(samples) < 2:
        return None
    span = samples[-1].monotonic - samples[0].monotonic
    if span <= 0:
        return None
    return (samples[-1].offset - samples[0].offset) / span * 1e6
