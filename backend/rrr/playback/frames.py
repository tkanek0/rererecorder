"""Where each recorded frame sits on the monotonic clock."""

from __future__ import annotations

from dataclasses import dataclass

from rrr.video import ArchiveSource


def frame_times(
    archive: ArchiveSource, nominal_fps: float | None
) -> tuple[list[tuple[int, float]], bool]:
    """Return every frame's index and monotonic time, measured if possible.

    Args:
        archive: The open archive.
        nominal_fps: The rate the manifest recorded, used only when the archive
            carries no frame times. 30 if that is unknown too.

    Returns:
        ``(index, monotonic)`` pairs in recording order, and whether they are
        the recorded times (True) or a nominal rate starting at zero (False).
    """
    times = archive.frame_times()
    if times:
        return times, True
    fps = nominal_fps or 30.0
    return [(index, n / fps) for n, index in enumerate(archive.indices())], False


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
