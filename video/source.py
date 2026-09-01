"""Where frames come from.

``FrameSource`` is the seam that keeps everything downstream - analysis, the
web server, the CLI tools - from knowing whether a real camera is attached.
Only ``LiveSource`` exists today; a rosbag reader or a client that pulls frames
from a running server can be added without touching a consumer.

The seam earns its keep immediately: a RealSense device can be opened by one
process at a time, so anything that wants frames while the server holds the
camera has to get them from somewhere else.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Protocol

import numpy as np
import pyrealsense2 as rs

from timeline import ClockPair, read_clocks

from .config import StreamConfig
from .types import (
    Calibration,
    DeviceInfo,
    Extrinsics,
    FrameSet,
    Intrinsics,
    Motion,
)

logger = logging.getLogger(__name__)

#: How long to block for one frame set before looking up to check whether we
#: have been asked to stop. Short enough that closing a source is responsive,
#: long enough not to spin.
FRAME_TIMEOUT_MS = 1000

#: Consecutive seconds without a frame before the stream is declared broken.
#: The USB link recovers from the odd missed frame on its own; five seconds of
#: silence is a device that has gone away or wedged.
STALL_LIMIT_S = 5.0

#: How far apart the depth and colour timestamps of one set may be before the
#: set is discarded as mispaired.
#:
#: Measured on a D455 at 848x480/30, aligned: a properly paired set has the two
#: within **0.03 ms** - they come off one ASIC. The first five sets after
#: ``pipeline.start`` do not: the syncer pairs one stale depth frame with five
#: successive colour frames, and the gap runs 294, 328, 363, 398, 432 ms. A
#: dropped depth frame mid-stream does the same thing, once, at 32 ms.
#:
#: 5 ms therefore separates the two cases by two orders of magnitude in both
#: directions, which is why the threshold is not delicate. Discarding these
#: matters more here than in a viewer: a set whose depth is 400 ms older than
#: its colour is not a moment in time, and writing it into a recording that
#: claims to be synchronised would be worse than dropping it.
MAX_PAIR_SKEW_MS = 5.0


class StreamError(RuntimeError):
    """The stream stopped delivering frames and will not recover on its own."""


class FrameSource(Protocol):
    """A supply of frame sets.

    Implementations are used as context managers and iterated once:

        with LiveSource(config) as source:
            for frames in source.frames():
                ...

    Attributes:
        calibration: Calibration in force, valid once the source is open.
    """

    calibration: Calibration

    def __enter__(self) -> FrameSource:
        """Open the source."""
        ...

    def __exit__(self, *exc: object) -> None:
        """Close the source."""
        ...

    def frames(self) -> Iterator[FrameSet]:
        """Yield frame sets until the source is closed or fails.

        Raises:
            StreamError: If the stream breaks in a way retrying will not fix.
        """
        ...


def _intrinsics(profile: rs.stream_profile) -> Intrinsics:
    """Convert an SDK video stream profile to our own intrinsics type."""
    intr = profile.as_video_stream_profile().get_intrinsics()
    return Intrinsics(
        width=intr.width,
        height=intr.height,
        fx=intr.fx,
        fy=intr.fy,
        ppx=intr.ppx,
        ppy=intr.ppy,
        model=str(intr.model),
        coeffs=tuple(float(c) for c in intr.coeffs),
    )


def _extrinsics(source: rs.stream_profile, target: rs.stream_profile) -> Extrinsics:
    """Convert an SDK extrinsic to ours, transposing to row-major."""
    extr = source.get_extrinsics_to(target)
    column_major = np.asarray(extr.rotation, dtype=np.float64).reshape(3, 3)
    return Extrinsics(
        rotation=tuple(column_major.T.reshape(-1).tolist()),
        translation=tuple(float(t) for t in extr.translation),
    )


#: Every metadata field the SDK defines. A given frame supports a subset - 22 of
#: 131 on this D455's depth stream - so the supported set is worked out once per
#: stream and cached rather than probed per frame.
_METADATA_FIELDS = tuple(rs.frame_metadata_value.__members__.values())


def _read_metadata(frame: rs.frame, fields: tuple[rs.frame_metadata_value, ...]) -> dict[str, int]:
    """Read the given metadata fields off a frame.

    Args:
        frame: The frame to read.
        fields: Fields known to be supported by this stream.

    Returns:
        Field name to value. A field that fails is skipped rather than fatal:
        firmware occasionally stops reporting one mid-stream.
    """
    values: dict[str, int] = {}
    for field in fields:
        try:
            values[str(field).split(".")[-1]] = int(frame.get_frame_metadata(field))
        except RuntimeError:
            continue
    return values


def _vector(frame: rs.frame) -> tuple[float, float, float]:
    """Read an SDK motion frame as a plain tuple."""
    data = frame.as_motion_frame().get_motion_data()
    return (float(data.x), float(data.y), float(data.z))


class LiveSource:
    """Frames from an attached RealSense device.

    One instance owns one ``rs.pipeline``, and therefore the device. Opening a
    second one while this is running fails inside the SDK, which is the reason
    the server and the CLI tools cannot both stream at once.
    """

    def __init__(self, config: StreamConfig | None = None, serial: str = "") -> None:
        """Prepare a source without touching the device.

        Args:
            config: Streams to request. Defaults to aligned color and depth at
                848x480/30.
            serial: Serial number of the device to use. Empty means whichever
                device the SDK finds first.
        """
        self._config = config or StreamConfig()
        self._serial = serial
        self._pipeline: rs.pipeline | None = None
        self._align: rs.align | None = None
        self._recorder: rs.recorder | None = None
        self._recording = False
        self._calibration: Calibration | None = None
        self._device: DeviceInfo | None = None
        self._index = 0
        self._stop = False
        self._metadata_fields: dict[str, tuple[rs.frame_metadata_value, ...]] = {}
        #: Frame numbers of the last set that was yielded, per stream. The SDK
        #: re-delivers a frame it has already given out - see MAX_PAIR_SKEW_MS -
        #: and processing one twice would put two entries in a recording for one
        #: moment.
        self._last_numbers: dict[str, int] = {}
        self._skipped_duplicate = 0
        self._skipped_unpaired = 0
        self._timestamp_domain = "unknown"

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> LiveSource:
        """Open the device and start streaming."""
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop streaming and release the device."""
        self.close()

    def open(self) -> None:
        """Start the pipeline and read the calibration.

        Raises:
            StreamError: If no device is present or it cannot serve the
                requested profiles. The SDK's own message is preserved, since
                it names the profile that was refused.
        """
        if self._pipeline is not None:
            return

        cfg = self._config
        pipeline = rs.pipeline()
        rs_config = rs.config()
        if self._serial:
            rs_config.enable_device(self._serial)
        if cfg.color is not None:
            width, height, fps = cfg.color
            rs_config.enable_stream(rs.stream.color, width, height, rs.format.rgb8, fps)
        if cfg.depth is not None:
            width, height, fps = cfg.depth
            rs_config.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
        if cfg.motion:
            # No resolution to choose; the SDK picks the sensor's default rate.
            rs_config.enable_stream(rs.stream.accel)
            rs_config.enable_stream(rs.stream.gyro)
        if cfg.record_path:
            # Checked here rather than left to the SDK so that the message names
            # the constraint. librealsense 2.56 moved recording from rosbag1
            # (.bag) to rosbag2 (.db3, SQLite), and every tutorial written before
            # that says .bag.
            if not cfg.record_path.endswith(".db3"):
                raise StreamError(
                    f"recordings must be named *.db3, got {cfg.record_path!r}"
                )
            rs_config.enable_record_to_file(cfg.record_path)

        try:
            profile = pipeline.start(rs_config)
        except RuntimeError as exc:
            raise StreamError(f"could not start the pipeline: {exc}") from exc

        self._pipeline = pipeline
        self._stop = False
        self._align = rs.align(rs.stream.color) if cfg.aligns else None
        self._enable_global_time(profile)
        self._calibration = self._read_calibration(profile)
        self._device = self._read_device(profile)
        if cfg.record_path:
            # Recording starts the moment the pipeline does; a caller who wants
            # to arm it later pauses it here and resumes on demand.
            self._recorder = profile.get_device().as_recorder()
            self._recording = True
        logger.info(
            "live source open: %s, aligned=%s, recording=%s",
            self._device.serial if self._device else "?",
            cfg.aligns,
            bool(cfg.record_path),
        )

    def close(self) -> None:
        """Stop the pipeline.

        Safe to call from another thread while ``frames`` is blocked: the
        generator notices within ``FRAME_TIMEOUT_MS`` and returns.
        """
        self._stop = True
        pipeline, self._pipeline = self._pipeline, None
        self._recorder = None
        if pipeline is None:
            return
        try:
            pipeline.stop()
        except RuntimeError as exc:  # already stopped, or the device vanished
            logger.debug("pipeline stop complained: %s", exc)
        logger.info("live source closed")

    # -- description -------------------------------------------------------

    @property
    def config(self) -> StreamConfig:
        """The configuration this source was opened with."""
        return self._config

    @property
    def calibration(self) -> Calibration:
        """Calibration in force.

        Raises:
            StreamError: If the source has not been opened yet.
        """
        if self._calibration is None:
            raise StreamError("calibration is only known once the source is open")
        return self._calibration

    @property
    def device(self) -> DeviceInfo | None:
        """Identity of the camera, or None before the source is opened."""
        return self._device

    def _read_calibration(self, profile: rs.pipeline_profile) -> Calibration:
        """Read intrinsics, extrinsics and depth scale from a started pipeline.

        When depth is aligned to color the delivered depth image lives in the
        color camera's geometry, so that is what gets reported: handing back the
        depth sensor's own intrinsics here would silently misplace every
        unprojected point.
        """
        cfg = self._config
        color_profile = (
            profile.get_stream(rs.stream.color) if cfg.color is not None else None
        )
        depth_profile = (
            profile.get_stream(rs.stream.depth) if cfg.depth is not None else None
        )

        color = _intrinsics(color_profile) if color_profile else None
        depth = _intrinsics(depth_profile) if depth_profile else None
        depth_to_color = (
            _extrinsics(depth_profile, color_profile)
            if depth_profile and color_profile
            else None
        )
        if cfg.aligns:
            depth = color
            depth_to_color = Extrinsics.identity()

        scale = 0.0
        if depth_profile:
            sensor = profile.get_device().first_depth_sensor()
            scale = float(sensor.get_depth_scale())

        return Calibration(
            color=color,
            depth=depth,
            depth_scale=scale,
            depth_to_color=depth_to_color,
            aligned=cfg.aligns,
        )

    @staticmethod
    def _read_device(profile: rs.pipeline_profile) -> DeviceInfo:
        """Read the device's identity, tolerating fields it does not expose."""
        device = profile.get_device()

        def info(key: rs.camera_info) -> str:
            try:
                return str(device.get_info(key))
            except RuntimeError:
                return ""

        return DeviceInfo(
            name=info(rs.camera_info.name),
            serial=info(rs.camera_info.serial_number),
            firmware=info(rs.camera_info.firmware_version),
            usb_type=info(rs.camera_info.usb_type_descriptor),
        )

    def options(self) -> dict[str, float]:
        """Read every sensor option the device exposes, with its current value.

        The state the data was taken in: exposure, gain, laser power, the visual
        preset, the emitter mode, and the temperatures. 48 values on this D455,
        costing 14 ms - worth taking once at the start of a recording, not per
        frame.

        Returns:
            ``"Sensor Name/Option_Name"`` to value. Options that refuse to be
            read are omitted rather than reported as zero.

        Raises:
            StreamError: If the source has not been opened yet.
        """
        if self._pipeline is None:
            raise StreamError("open the source before reading its options")
        snapshot: dict[str, float] = {}
        for sensor in self._pipeline.get_active_profile().get_device().sensors:
            try:
                name = sensor.get_info(rs.camera_info.name)
            except RuntimeError:
                continue
            for option in sensor.get_supported_options():
                try:
                    key = f"{name}/{str(option).split('.')[-1]}"
                    snapshot[key] = float(sensor.get_option(option))
                except RuntimeError:
                    continue
        return snapshot

    # -- recording ---------------------------------------------------------

    @property
    def recording(self) -> bool | None:
        """Whether the rosbag recorder is running, or None if there is none."""
        return self._recording if self._recorder is not None else None

    def set_recording(self, active: bool) -> None:
        """Pause or resume writing to the rosbag.

        The file itself was fixed when the pipeline started, so this switches an
        existing recording on and off rather than choosing where it goes.

        Args:
            active: True to write frames, False to stop writing them.

        Raises:
            StreamError: If the source was opened without ``record_path``.
        """
        if self._recorder is None:
            raise StreamError("this source was not opened with a record_path")
        if active:
            self._recorder.resume()
        else:
            self._recorder.pause()
        self._recording = active

    # -- timestamps --------------------------------------------------------

    @property
    def timestamp_domain(self) -> str:
        """What the SDK's frame timestamps mean, once frames have arrived.

        ``"global_time"`` - the default, and what makes this recorder work - is
        epoch milliseconds fitted to the host clock. ``"unknown"`` until the
        first frame.
        """
        return self._timestamp_domain

    @property
    def skipped_duplicate(self) -> int:
        """Sets discarded because every frame in them had been seen before."""
        return self._skipped_duplicate

    @property
    def skipped_unpaired(self) -> int:
        """Sets discarded because their streams disagreed about the moment."""
        return self._skipped_unpaired

    @property
    def skipped(self) -> int:
        """Sets discarded for either reason."""
        return self._skipped_duplicate + self._skipped_unpaired

    @staticmethod
    def _enable_global_time(profile: rs.pipeline_profile) -> None:
        """Ask every sensor to timestamp frames on the host's clock.

        Args:
            profile: The started pipeline's profile.

        Measured on a D455 with librealsense 2.58.3, this is already on: all
        three sensors report ``global_time_enabled = 1.0`` without being asked.
        It is set explicitly anyway, because the whole point of this repository
        is that frame times can be compared with audio times, and that stops
        being true if a future SDK, a different device or somebody else's
        leftover configuration turns it off. A failure is logged rather than
        raised: a recording with frames on the device clock is still worth
        having, and the session says which it got.
        """
        for sensor in profile.get_device().query_sensors():
            name = sensor.get_info(rs.camera_info.name)
            if not sensor.supports(rs.option.global_time_enabled):
                logger.warning(
                    "%s does not support global time; its frames cannot be "
                    "placed on the host clock",
                    name,
                )
                continue
            if sensor.get_option(rs.option.global_time_enabled) == 1.0:
                continue
            try:
                sensor.set_option(rs.option.global_time_enabled, 1.0)
                logger.info("enabled global time on %s", name)
            except RuntimeError as exc:
                logger.warning("could not enable global time on %s: %s", name, exc)

    # -- frames ------------------------------------------------------------

    def frames(self) -> Iterator[FrameSet]:
        """Yield frame sets until the source is closed.

        Yields:
            One FrameSet per synchronised set of frames.

        Raises:
            StreamError: If the source is not open, or the device stops
                delivering frames for ``STALL_LIMIT_S``.
        """
        if self._pipeline is None:
            raise StreamError("open the source before reading frames")

        last_frame_at = time.monotonic()
        while not self._stop:
            pipeline = self._pipeline
            if pipeline is None:
                return
            try:
                composite = pipeline.wait_for_frames(timeout_ms=FRAME_TIMEOUT_MS)
            except RuntimeError as exc:
                # The SDK raises for a timeout and for a device that went away,
                # with no way to tell them apart other than waiting to see
                # whether anything arrives.
                if self._stop:
                    return
                waited = time.monotonic() - last_frame_at
                if waited > STALL_LIMIT_S:
                    raise StreamError(
                        f"no frames for {waited:.1f}s: {exc}"
                    ) from exc
                continue

            # Both host clocks, per set. A single pair taken at the start would
            # be enough only if the offset held still, and NTP slews it - so the
            # conversion from the camera's epoch milliseconds to the monotonic
            # axis uses the offset that was in force for this frame.
            clock = read_clocks()
            last_frame_at = clock.monotonic
            frame_set = self._assemble(composite, clock)
            if frame_set is not None:
                yield frame_set

    def _assemble(
        self, composite: rs.composite_frame, clock: ClockPair
    ) -> FrameSet | None:
        """Turn one SDK composite frame into a FrameSet.

        Args:
            composite: What ``wait_for_frames`` returned.
            clock: Both host clocks, read when it returned.

        Returns:
            The frame set, or None if this composite is not one moment worth
            keeping. Three things cause that, and all three were observed on a
            D455 within the first two seconds of streaming:

            * an enabled stream was missing from the composite,
            * every frame in it had already been delivered,
            * its streams disagreed about when they were taken by more than
              ``MAX_PAIR_SKEW_MS``.
        """
        # Motion is read before alignment: rs.align rebuilds the composite from
        # the video streams and the inertial frames do not survive the trip.
        motion = self._read_motion(composite) if self._config.motion else None

        if self._align is not None:
            composite = self._align.process(composite)

        # Identify the frames before copying any pixels. Both checks below
        # reject the whole set, and a copy is 814 KB for depth and 1.2 MB for
        # colour - not work to do before finding out the set is being thrown
        # away, at 30 sets a second.
        frames: dict[str, rs.frame] = {}
        if self._config.color is not None:
            frame = composite.get_color_frame()
            if not frame:
                return None
            frames["color"] = frame
        if self._config.depth is not None:
            frame = composite.get_depth_frame()
            if not frame:
                return None
            frames["depth"] = frame

        if self._timestamp_domain == "unknown":
            self._note_timestamp_domain(next(iter(frames.values())))

        if not self._is_new(frames) or not self._is_paired(frames):
            return None

        metadata: dict[str, dict[str, int]] = {
            name: _read_metadata(frame, self._fields_for(name, frame))
            for name, frame in frames.items()
        }
        # Copied, not viewed: the SDK reuses these buffers as soon as the
        # composite is released, and consumers hold frames past that point.
        color = (
            np.asanyarray(frames["color"].get_data()).copy()
            if "color" in frames
            else None
        )
        depth = (
            np.asanyarray(frames["depth"].get_data()).copy()
            if "depth" in frames
            else None
        )

        self._index += 1
        return FrameSet(
            index=self._index,
            timestamp_ms=float(composite.get_timestamp()),
            received_at=clock.monotonic,
            color=color,
            depth=depth,
            calibration=self.calibration,
            motion=motion,
            metadata=metadata or None,
            clock=clock,
            timestamp_domain=self._timestamp_domain,
        )

    def _is_new(self, frames: dict[str, rs.frame]) -> bool:
        """Whether this set holds anything not already delivered.

        Args:
            frames: The set's frames by stream name.

        Returns:
            False if every frame in it carries a number already yielded, in
            which case the set is a repeat and is counted as skipped.
        """
        numbers = {name: frame.get_frame_number() for name, frame in frames.items()}
        if numbers == self._last_numbers:
            self._skipped_duplicate += 1
            return False
        self._last_numbers = numbers
        return True

    def _is_paired(self, frames: dict[str, rs.frame]) -> bool:
        """Whether the frames in this set describe the same moment.

        Args:
            frames: The set's frames by stream name.

        Returns:
            False if the streams' timestamps differ by more than
            ``MAX_PAIR_SKEW_MS``, in which case the set is counted as skipped.
            Always True when only one stream is enabled - there is nothing to
            disagree with.
        """
        if len(frames) < 2:
            return True
        stamps = [frame.get_timestamp() for frame in frames.values()]
        skew = max(stamps) - min(stamps)
        if skew > MAX_PAIR_SKEW_MS:
            self._skipped_unpaired += 1
            logger.debug(
                "discarding a set whose streams are %.1f ms apart: %s",
                skew,
                {name: frame.get_frame_number() for name, frame in frames.items()},
            )
            return False
        return True

    def _note_timestamp_domain(self, frame: rs.frame) -> None:
        """Record what the SDK's timestamps mean, once, and say so in the log.

        Args:
            frame: Any frame from the stream.

        Anything other than ``global_time`` means frame times are on the
        device's own clock, and cannot be compared with the audio's. The
        recording stays usable - the frames are still frames - but it is not a
        synchronised one, so this is written into the session rather than left
        for someone to discover.
        """
        self._timestamp_domain = str(frame.get_frame_timestamp_domain()).rsplit(
            ".", 1
        )[-1]
        if self._timestamp_domain == "global_time":
            logger.info(
                "frame timestamps are epoch milliseconds (global_time); "
                "they can be placed against the audio clock"
            )
        else:
            logger.warning(
                "frame timestamps are in domain %r, not global_time: they are on "
                "the device's own clock and cannot be compared with audio times. "
                "Frame arrival times will be used instead, which costs a few "
                "milliseconds of accuracy",
                self._timestamp_domain,
            )

    def _fields_for(
        self, stream: str, frame: rs.frame
    ) -> tuple[rs.frame_metadata_value, ...]:
        """Return the metadata fields this stream supports, probing once."""
        known = self._metadata_fields.get(stream)
        if known is None:
            known = tuple(
                field
                for field in _METADATA_FIELDS
                if frame.supports_frame_metadata(field)
            )
            self._metadata_fields[stream] = known
            logger.debug("%s frames carry %d metadata fields", stream, len(known))
        return known

    @staticmethod
    def _read_motion(composite: rs.composite_frame) -> Motion:
        """Read the newest inertial samples out of a composite frame."""
        accel = composite.first_or_default(rs.stream.accel)
        gyro = composite.first_or_default(rs.stream.gyro)
        return Motion(
            accel=_vector(accel) if accel else None,
            gyro=_vector(gyro) if gyro else None,
        )


def list_devices() -> list[DeviceInfo]:
    """Enumerate attached RealSense devices without starting a stream.

    Returns:
        One entry per connected device, empty if none are attached.
    """
    context = rs.context()
    found: list[DeviceInfo] = []
    for device in context.query_devices():

        def info(key: rs.camera_info, dev: rs.device = device) -> str:
            try:
                return str(dev.get_info(key))
            except RuntimeError:
                return ""

        found.append(
            DeviceInfo(
                name=info(rs.camera_info.name),
                serial=info(rs.camera_info.serial_number),
                firmware=info(rs.camera_info.firmware_version),
                usb_type=info(rs.camera_info.usb_type_descriptor),
            )
        )
    return found
