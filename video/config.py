"""What to ask the camera for.

Kept separate from the source that applies it so that the same description can
be written by a CLI flag, an HTTP request or a test, and compared for equality
to decide whether a running pipeline has to be restarted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: Width, height and frame rate of one video stream.
StreamSpec = tuple[int, int, int]

#: The largest depth the D455 produces. Its stereo sensors are 1280x800 each,
#: but the depth processor tops out at 720 lines - measured: 1280x720 depth has
#: the same fx (653.36) as the raw 1280x800 infrared, with ppy 40 lower, so the
#: depth output is the sensor with 80 lines cropped rather than anything scaled.
#:
#: Every smaller size is a scale of this one. 848x480 is *not* a native mode,
#: whatever realsense-playground's notes say: its fx is 432.85 = 653.36 x 0.6625,
#: which is exactly 848/1280.
DEFAULT_DEPTH: StreamSpec = (1280, 720, 30)

#: The colour sensor's own resolution, and its maximum frame rate.
#:
#: Not matched to depth on purpose. Matching mattered when recordings were
#: aligned, because alignment would otherwise resample; recordings here are not
#: aligned, so each stream keeps its own geometry and alignment is applied later
#: from the extrinsics.
DEFAULT_COLOR: StreamSpec = (1280, 800, 30)

#: Pixel format to ask the colour sensor for.
#:
#: YUYV is what the sensor emits. Asking for rgb8 makes the SDK convert, which
#: costs CPU and 50% more bytes without adding anything - the chroma has already
#: been subsampled by then. Recording what the sensor produced means the
#: conversion stays a decision for whoever reads the file.
DEFAULT_COLOR_FORMAT = "yuyv"


@dataclass(frozen=True)
class StreamConfig:
    """A request for a set of streams.

    Attributes:
        color: Color stream as (width, height, fps), or None to disable it.
        depth: Depth stream as (width, height, fps), or None to disable it.
        color_format: Pixel format for the colour stream, ``"yuyv"`` or
            ``"rgb8"``. See DEFAULT_COLOR_FORMAT.
        infrared: Record the two raw infrared images the depth is computed
            from. They can only be opened at the depth stream's own resolution
            and rate, so there is nothing to configure beyond on or off.

            Worth the bytes when the point is to keep everything: the depth in
            a recording is one particular stereo match made by the camera's
            ASIC, and the infrared pair is what it was made from. Costs 55 MB/s
            raw on top of depth and colour, 27 MB/s compressed.
        align_to_color: Resample depth into the color camera's viewpoint, so
            that ``depth[y, x]`` describes ``color[y, x]``.

            **Off by default here**, unlike in realsense-playground. Alignment
            resamples, and resampling cannot be undone: it would put the depth
            on the colour camera's 1280x800 grid, destroy its correspondence
            with the infrared pair, and bake one particular choice into a file
            meant to outlast it. Every consumer can align on the way out using
            ``calibration.depth_to_color``; none of them can un-align.
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
    color_format: str = DEFAULT_COLOR_FORMAT
    infrared: bool = False
    align_to_color: bool = False
    motion: bool = False
    record_path: str | None = None

    def __post_init__(self) -> None:
        """Reject a configuration that asks for nothing.

        Raises:
            ValueError: If neither video stream is enabled.
        """
        if self.color is None and self.depth is None:
            raise ValueError("at least one of color or depth must be enabled")
        if self.color_format not in ("yuyv", "rgb8"):
            raise ValueError(f"unsupported colour format {self.color_format!r}")
        if self.infrared and self.depth is None:
            # The infrared streams are the depth sensor's own; without depth
            # enabled there is no resolution to give them.
            raise ValueError("infrared needs the depth stream enabled")

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
            "color_format": self.color_format,
            "infrared": self.infrared,
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
