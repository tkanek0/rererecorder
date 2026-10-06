"""Where each recorded frame sits on the monotonic clock."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeRange:
    """Half-open interval on the recording's monotonic clock.

    Attributes:
        start: First instant inside, or None for the beginning.
        end: First instant outside, or None for the end.
    """

    start: float | None = None
    end: float | None = None

    @classmethod
    def of_frames(
        cls, times: list[tuple[int, float]], start: int, end: int | None
    ) -> TimeRange:
        """Return the interval covered by frames ``start`` up to ``end``.

        Args:
            times: Frame times, as :func:`frame_times` returns them.
            start: Position of the first frame inside.
            end: Position of the first frame outside, or None for all.

        Returns:
            The interval; a side left open when it is the recording's own edge.

        Raises:
            ValueError: If ``start`` is past the last frame.
        """
        if start and start >= len(times):
            raise ValueError(
                f"start frame {start} is outside an archive of {len(times)} frames"
            )
        return cls(
            start=times[start][1] if start else None,
            end=times[end][1] if end is not None and end < len(times) else None,
        )

    @property
    def selected(self) -> bool:
        """Return whether either side of the recording was trimmed."""
        return self.start is not None or self.end is not None

    def contains(self, seconds: float) -> bool:
        """Return whether a timestamp belongs to this interval."""
        return (self.start is None or seconds >= self.start) and (
            self.end is None or seconds < self.end
        )
