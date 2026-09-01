"""What to ask the camera for.

Kept separate from the source that applies it so that the same description can
be written by a CLI flag, an HTTP request or a test, and compared for equality
to decide whether a running pipeline has to be restarted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: Width, height and frame rate of one video stream.
StreamSpec = tuple[int, int, int]

#: The D455's native depth resolution. Asking for anything else makes the
#: firmware scale internally, which costs sharpness for no bandwidth saving on
#: a local link.
DEFAULT_DEPTH: StreamSpec = (848, 480, 30)

#: Matched to the depth stream so that alignment neither up- nor downsamples.
DEFAULT_COLOR: StreamSpec = (848, 480, 30)


@dataclass(frozen=True)
class StreamConfig:
    """A request for a set of streams.

    Attributes:
        color: Color stream as (width, height, fps), or None to disable it.
        depth: Depth stream as (width, height, fps), or None to disable it.
        align_to_color: Resample depth into the color camera's viewpoint, so
            that ``depth[y, x]`` describes ``color[y, x]``. Costs a few
            milliseconds per frame and is what almost every consumer wants;
            turn it off to measure with the depth sensor's own geometry.
        motion: Enable the accelerometer and gyroscope.
        record_path: rosbag file to write every frame to, or None. Must end in
            ``.db3``: librealsense 2.56 moved from rosbag1 to rosbag2 and
            rejects the ``.bag`` that older examples use.

            The SDK fixes this at pipeline start, so switching to a different
            file means restarting the pipeline. Pausing and resuming an open
            recording does not - see ``LiveSource.set_recording``.
    """

    color: StreamSpec | None = DEFAULT_COLOR
    depth: StreamSpec | None = DEFAULT_DEPTH
    align_to_color: bool = True
    motion: bool = False
    record_path: str | None = None

    def __post_init__(self) -> None:
        """Reject a configuration that asks for nothing.

        Raises:
            ValueError: If neither video stream is enabled.
        """
        if self.color is None and self.depth is None:
            raise ValueError("at least one of color or depth must be enabled")

    @property
    def aligns(self) -> bool:
        """Whether alignment will actually happen.

        Alignment needs both streams; asking for it with one of them disabled
        is a no-op rather than an error, so that toggling a stream in a UI does
        not have to also toggle this.
        """
        return self.align_to_color and self.color is not None and self.depth is not None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this configuration."""
        return {
            "color": list(self.color) if self.color else None,
            "depth": list(self.depth) if self.depth else None,
            "align_to_color": self.align_to_color,
            "motion": self.motion,
            "record_path": self.record_path,
        }

    def with_changes(self, **changes: object) -> StreamConfig:
        """Return a copy with the given fields replaced.

        Args:
            **changes: Field names and their new values.

        Returns:
            A new StreamConfig; validation runs again on the result.
        """
        return replace(self, **changes)  # type: ignore[arg-type]
