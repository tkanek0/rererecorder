"""What to ask the camera for, kept apart from the source that applies it."""

from __future__ import annotations

from dataclasses import dataclass, replace

#: Width, height and frame rate of one video stream.
StreamSpec = tuple[int, int, int]

#: The largest depth the D455 produces: the infrared sensor, cropped. See
#: docs/frame-loss.md, "What the sensors actually are".
DEFAULT_DEPTH: StreamSpec = (1280, 720, 30)

#: The colour sensor's own resolution, not matched to depth: docs/decisions.md 2.
DEFAULT_COLOR: StreamSpec = (1280, 800, 30)

#: What the colour sensor emits. See docs/decisions.md 4.
DEFAULT_COLOR_FORMAT = "yuyv"

#: What the depth projector does while recording. See docs/features.md,
#: "The projector". ``RRR_EMITTER`` is read by rrr.recorder.config, not here.
EMITTER_MODES = ("on", "off", "alternating")
DEFAULT_EMITTER = "on"


@dataclass(frozen=True)
class StreamConfig:
    """A request for a set of streams.

    Attributes:
        color: Color stream as (width, height, fps), or None to disable it.
        depth: Depth stream as (width, height, fps), or None to disable it.
        color_format: Pixel format for the colour stream, ``"yuyv"`` or
            ``"rgb8"``. See DEFAULT_COLOR_FORMAT.
        infrared: Record the two raw infrared images the depth is computed
            from, at the depth stream's own resolution and rate. See
            docs/decisions.md 3.
        align_to_color: Resample depth into the color camera's viewpoint.
            Off by default; see docs/decisions.md 2.
        emitter: What the depth projector does - ``"on"``, ``"off"`` or
            ``"alternating"``. See EMITTER_MODES.
        motion: Enable the accelerometer and gyroscope.
    """

    color: StreamSpec | None = DEFAULT_COLOR
    depth: StreamSpec | None = DEFAULT_DEPTH
    color_format: str = DEFAULT_COLOR_FORMAT
    infrared: bool = False
    emitter: str = DEFAULT_EMITTER
    align_to_color: bool = False
    motion: bool = False

    def __post_init__(self) -> None:
        """Reject a configuration that asks for nothing.

        Raises:
            ValueError: If neither video stream is enabled.
        """
        if self.color is None and self.depth is None:
            raise ValueError("at least one of color or depth must be enabled")
        if self.color_format not in ("yuyv", "rgb8"):
            raise ValueError(f"unsupported colour format {self.color_format!r}")
        if self.emitter not in EMITTER_MODES:
            raise ValueError(
                f"unsupported emitter mode {self.emitter!r}, "
                f"expected one of {', '.join(EMITTER_MODES)}"
            )
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
            "emitter": self.emitter,
            "align_to_color": self.align_to_color,
            "motion": self.motion,
        }

    def with_changes(self, **changes: object) -> StreamConfig:
        """Return a copy with the given fields replaced.

        Args:
            **changes: Field names and their new values.

        Returns:
            A new StreamConfig; validation runs again on the result.
        """
        return replace(self, **changes)  # type: ignore[arg-type]
