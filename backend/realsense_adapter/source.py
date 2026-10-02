"""Where frames come from.

``FrameSource`` hides from consumers whether frames come from a live camera
(``LiveSource``) or a recording (``rrr.video.ArchiveSource``).
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Iterator
from typing import Protocol

import numpy as np
import pyrealsense2 as rs

from .config import StreamConfig
from .types import (
    Calibration,
    DeviceInfo,
    Extrinsics,
    FrameSet,
    Intrinsics,
    Motion,
    MotionCalibration,
    MotionIntrinsics,
    MotionSample,
)

logger = logging.getLogger(__name__)

#: How long to block for one frame set before checking whether to stop.
FRAME_TIMEOUT_MS = 1000

#: Consecutive seconds without a frame before the stream is declared broken.
STALL_LIMIT_S = 5.0

#: Inertial samples held between drains (both streams share it): about four
#: seconds at ~960 Hz. Oldest are discarded first and counted as overrun.
MOTION_BUFFER = 4096



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


def _option_name(option: rs.option) -> str:
    """The SDK enum member's name, e.g. ``emitter_enabled``."""
    # Not str(option): that is the display name, "Emitter Enabled".
    return option.name


def _motion_intrinsics(profile: rs.stream_profile) -> MotionIntrinsics | None:
    """Convert an SDK motion profile's correction to ours.

    Args:
        profile: An accelerometer or gyroscope stream profile.

    Returns:
        The correction, or None if the device does not carry one - never a
        substituted identity.
    """
    try:
        intr = profile.as_motion_stream_profile().get_motion_intrinsics()
    except RuntimeError:
        return None
    data = np.asarray(intr.data, dtype=np.float64).reshape(-1)
    noise = tuple(float(v) for v in intr.noise_variances)
    bias = tuple(float(v) for v in intr.bias_variances)
    return MotionIntrinsics(
        data=tuple(data.tolist()),
        noise_variances=(noise[0], noise[1], noise[2]),
        bias_variances=(bias[0], bias[1], bias[2]),
    )


#: Every metadata field the SDK defines. A stream supports a subset (22 on a
#: D455's depth), probed once per stream and cached.
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


class LiveSource:
    """Frames from an attached RealSense device.

    One instance owns one ``rs.pipeline``, and therefore the device: a second
    one cannot open it while this is running.
    """

    def __init__(self, config: StreamConfig | None = None, serial: str = "") -> None:
        """Prepare a source without touching the device.

        Args:
            config: Streams to request. Defaults to ``StreamConfig()``.
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
        #: Frame numbers of the last set yielded, per stream, to discard sets
        #: the SDK re-delivers.
        self._last_numbers: dict[str, int] = {}
        self._skipped_duplicate = 0
        #: Sets discarded before the first good one: the syncer settling, not
        #: a loss. See docs/frame-loss.md "What is still discarded, on purpose".
        self._skipped_warmup = 0

        #: The motion sensor, opened separately from the video pipeline with
        #: its own callback. See docs/decisions.md 12.
        self._motion_sensor: rs.sensor | None = None
        self._motion: collections.deque[MotionSample] = collections.deque(
            maxlen=MOTION_BUFFER
        )
        self._motion_lock = threading.Lock()
        self._motion_received = 0
        self._motion_overrun = 0
        self._motion_domain = "unknown"
        self._timestamp_domain = "unknown"

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> LiveSource:
        """Open the device and start streaming."""
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop streaming and release the device."""
        self.close()

    def _require_device(self) -> None:
        """Fail fast when the device ``open`` would ask for is not attached.

        Raises:
            StreamError: If no device, or none with the configured serial, is
                attached.

        Avoids ``pipeline.start()``'s 15 s GIL-holding failure with nothing
        attached (docs/decisions.md 29). Holds the GIL for ~0.1 s itself, so
        call it once per open, never poll it.
        """
        serials = [
            device.get_info(rs.camera_info.serial_number)
            for device in rs.context().query_devices()
        ]
        if not serials:
            raise StreamError("no RealSense device connected")
        if self._serial and self._serial not in serials:
            raise StreamError(
                f"no RealSense device with serial {self._serial!r}; "
                f"attached: {', '.join(serials)}"
            )

    def open(self) -> None:
        """Start the pipeline and read the calibration.

        Raises:
            StreamError: If no device is present or it cannot serve the
                requested profiles. Carries the SDK's own message.
        """
        if self._pipeline is not None:
            return

        self._require_device()
        cfg = self._config
        pipeline = rs.pipeline()
        rs_config = rs.config()
        if self._serial:
            rs_config.enable_device(self._serial)
        if cfg.color is not None:
            width, height, fps = cfg.color
            fmt = rs.format.yuyv if cfg.color_format == "yuyv" else rs.format.rgb8
            rs_config.enable_stream(rs.stream.color, width, height, fmt, fps)
        if cfg.depth is not None:
            width, height, fps = cfg.depth
            rs_config.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
            if cfg.infrared:
                # The same sensor, so the same size and rate; index 1 is left.
                for index in (1, 2):
                    rs_config.enable_stream(
                        rs.stream.infrared, index, width, height, rs.format.y8, fps
                    )
        # Motion is deliberately NOT enabled on the pipeline; see _open_motion
        # and docs/decisions.md 12.
        if cfg.record_path:
            # librealsense 2.56+ records rosbag2 (.db3), not .bag; say so here
            # rather than leave it to the SDK's message.
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
        self._apply_emitter(profile)
        self._calibration = self._read_calibration(profile)
        self._device = self._read_device(profile)
        if cfg.motion:
            self._open_motion(profile)
        if cfg.record_path:
            # Recording starts with the pipeline; pause/resume via
            # set_recording.
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
        # Before the pipeline, or the motion callback runs against a device
        # being torn down.
        self._close_motion()
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

        When depth is aligned to color, the color intrinsics are reported for
        depth, since that is the geometry of the delivered image.
        """
        cfg = self._config
        color_profile = (
            profile.get_stream(rs.stream.color) if cfg.color is not None else None
        )
        depth_profile = (
            profile.get_stream(rs.stream.depth) if cfg.depth is not None else None
        )

        infrared_profiles: tuple[Any, Any] = (None, None)
        if cfg.infrared:
            infrared_profiles = (
                self._stream(profile, rs.stream.infrared, 1),
                self._stream(profile, rs.stream.infrared, 2),
            )

        color = _intrinsics(color_profile) if color_profile else None
        depth = _intrinsics(depth_profile) if depth_profile else None
        depth_to_color = (
            _extrinsics(depth_profile, color_profile)
            if depth_profile and color_profile
            else None
        )
        infrared = tuple(
            _intrinsics(entry) if entry else None for entry in infrared_profiles
        )
        # The first is expected to be the identity on a D400; read, not assumed.
        depth_to_infrared = tuple(
            _extrinsics(depth_profile, entry) if depth_profile and entry else None
            for entry in infrared_profiles
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
            infrared=(infrared[0], infrared[1]),
            depth_to_infrared=(depth_to_infrared[0], depth_to_infrared[1]),
            motion=self._read_motion_calibration(profile, depth_profile),
        )

    def _read_motion_calibration(
        self, profile: rs.pipeline_profile, depth_profile: rs.stream_profile | None
    ) -> MotionCalibration | None:
        """Read where the inertial sensor sits and how to correct it.

        Args:
            profile: The started pipeline's profile, for the device.
            depth_profile: The stream the transforms are expressed against, or
                None when depth is not being recorded.

        Returns:
            The calibration, or None if the device has no inertial sensor or
            depth is not running to express the transforms against.

        Separate from :meth:`_open_motion` so it is recorded even when the
        sensor fails to start; a read failure never stops the recording.
        """
        if depth_profile is None:
            return None
        try:
            sensor = next(
                s
                for s in profile.get_device().query_sensors()
                if "Motion" in s.get_info(rs.camera_info.name)
            )
        except StopIteration:
            return None

        found: dict[str, Any] = {}
        try:
            for stream, name in ((rs.stream.accel, "accel"), (rs.stream.gyro, "gyro")):
                candidates = [
                    p for p in sensor.get_stream_profiles() if p.stream_type() == stream
                ]
                if not candidates:
                    continue
                chosen = max(candidates, key=lambda p: p.fps())
                found[name] = _motion_intrinsics(chosen)
                found[f"depth_to_{name}"] = _extrinsics(depth_profile, chosen)
        except RuntimeError as exc:
            logger.warning("could not read the inertial calibration: %s", exc)
            return None

        if not found:
            return None
        return MotionCalibration(
            accel=found.get("accel"),
            gyro=found.get("gyro"),
            depth_to_accel=found.get("depth_to_accel"),
            depth_to_gyro=found.get("depth_to_gyro"),
        )

    @staticmethod
    def _stream(
        profile: rs.pipeline_profile, stream: rs.stream, index: int = -1
    ) -> rs.stream_profile | None:
        """Find one started stream's profile, or None if it is not running.

        Args:
            profile: The started pipeline's profile.
            stream: Which stream to look for.
            index: Stream index, for the infrared pair.

        Returns:
            The profile, or None where the SDK would raise.
        """
        try:
            return profile.get_stream(stream, index)
        except RuntimeError:
            return None

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

        48 values on a D455, costing 14 ms: read once per recording, not per
        frame.

        Returns:
            ``"Sensor Name/option_name"`` to value. Options that refuse to be
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
                    key = f"{name}/{_option_name(option)}"
                    snapshot[key] = float(sensor.get_option(option))
                except RuntimeError:
                    continue
        return snapshot

    def set_option(self, key: str, value: float) -> None:
        """Set one sensor option, by the same key :meth:`options` reports it under.

        Args:
            key: ``"Sensor Name/option_name"`` - e.g.
                ``"RGB Camera/enable_auto_exposure"``.
            value: The value to set.

        Raises:
            StreamError: If the source is not open, no sensor has that name,
                the option name is not one the SDK knows, or that sensor does
                not support it.

        A diagnostic escape hatch (``scripts/frame_number_gaps.py``);
        ordinary recording configures everything through ``StreamConfig``.
        """
        if self._pipeline is None:
            raise StreamError("open the source before changing its options")
        sensor_name, _, option_name = key.partition("/")
        try:
            option = rs.option.__members__[option_name]
        except KeyError:
            raise StreamError(f"no such option {option_name!r}") from None
        for sensor in self._pipeline.get_active_profile().get_device().sensors:
            try:
                name = str(sensor.get_info(rs.camera_info.name))
            except RuntimeError:
                continue
            if name != sensor_name:
                continue
            if not sensor.supports(option):
                raise StreamError(f"{sensor_name} does not support {option_name}")
            sensor.set_option(option, value)
            return
        raise StreamError(f"no sensor named {sensor_name!r}")

    # -- recording ---------------------------------------------------------

    @property
    def recording(self) -> bool | None:
        """Whether the rosbag recorder is running, or None if there is none."""
        return self._recording if self._recorder is not None else None

    def set_recording(self, active: bool) -> None:
        """Pause or resume writing to the rosbag.

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

    # -- motion ------------------------------------------------------------

    def _open_motion(self, profile: rs.pipeline_profile) -> None:
        """Start the inertial sensor at its highest rate, on its own callback.

        Args:
            profile: The started pipeline's profile, for the device.

        A failure is logged and swallowed, so video still records. Opens each
        stream at its highest offered rate (400 Hz nominal on a D455).
        """
        try:
            sensor = next(
                s
                for s in profile.get_device().query_sensors()
                if "Motion" in s.get_info(rs.camera_info.name)
            )
        except StopIteration:
            logger.warning("this device has no motion sensor")
            return

        try:
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 1.0)
            wanted = []
            for stream in (rs.stream.accel, rs.stream.gyro):
                candidates = [
                    p for p in sensor.get_stream_profiles()
                    if p.stream_type() == stream
                ]
                if candidates:
                    wanted.append(max(candidates, key=lambda p: p.fps()))
            if not wanted:
                logger.warning("the motion sensor offers no streams")
                return
            sensor.open(wanted)
            sensor.start(self._on_motion)
        except RuntimeError as exc:
            logger.warning("could not start the inertial sensor: %s", exc)
            return

        self._motion_sensor = sensor
        logger.info(
            "inertial sensor started: %s",
            ", ".join(f"{p.stream_name()} at {p.fps()} Hz" for p in wanted),
        )

    def _close_motion(self) -> None:
        """Stop and release the inertial sensor, if it was started."""
        sensor, self._motion_sensor = self._motion_sensor, None
        if sensor is None:
            return
        try:
            sensor.stop()
            sensor.close()
        except RuntimeError as exc:  # noqa: BLE001 - closing must not raise
            logger.warning("could not stop the inertial sensor: %s", exc)

    def _on_motion(self, frame: rs.frame) -> None:
        """Store one inertial sample. Runs on librealsense's own thread.

        Args:
            frame: The motion frame.

        Kept to an append: it runs ~960 times a second (docs/decisions.md 12).
        """
        motion_frame = frame.as_motion_frame()
        if not motion_frame:
            return
        data = motion_frame.get_motion_data()
        sample = MotionSample(
            stream=frame.get_profile().stream_name().lower(),
            timestamp_ms=float(frame.get_timestamp()),
            x=float(data.x),
            y=float(data.y),
            z=float(data.z),
        )
        with self._motion_lock:
            if self._motion_domain == "unknown":
                self._motion_domain = str(
                    frame.get_frame_timestamp_domain()
                ).rsplit(".", 1)[-1]
            if len(self._motion) == self._motion.maxlen:
                # A deque with maxlen discards silently; count it instead.
                self._motion_overrun += 1
            self._motion.append(sample)
            self._motion_received += 1

    def drain_motion(self) -> list[MotionSample]:
        """Take every inertial sample buffered since the last call.

        Returns:
            The samples, oldest first. Empty when motion is disabled or nothing
            has arrived.
        """
        with self._motion_lock:
            samples = list(self._motion)
            self._motion.clear()
        return samples

    @property
    def motion_received(self) -> int:
        """Inertial samples the sensor has delivered since it opened."""
        with self._motion_lock:
            return self._motion_received

    @property
    def motion_overrun(self) -> int:
        """Samples discarded because nobody drained the buffer in time."""
        with self._motion_lock:
            return self._motion_overrun

    @property
    def motion_domain(self) -> str:
        """What the inertial timestamps mean, once samples have arrived."""
        with self._motion_lock:
            return self._motion_domain

    # -- timestamps --------------------------------------------------------

    @property
    def timestamp_domain(self) -> str:
        """What the SDK's frame timestamps mean, once frames have arrived.

        ``"unknown"`` until the first frame. See docs/features.md "Timing".
        """
        return self._timestamp_domain

    @property
    def skipped_duplicate(self) -> int:
        """Sets discarded because every frame in them had been seen before."""
        return self._skipped_duplicate

    @property
    def skipped_warmup(self) -> int:
        """Sets discarded before the first good one, while the syncer settled."""
        return self._skipped_warmup

    @property
    def skipped(self) -> int:
        """Sets discarded mid-stream.

        Excludes warm-up sets, which are not a loss.
        """
        return self._skipped_duplicate

    @property
    def frame_numbers(self) -> dict[str, int]:
        """The most recently delivered set's ``frame_number``, per stream.

        For diagnosis only (``scripts/frame_number_gaps.py``); not carried
        on ``FrameSet`` or written to the archive.
        """
        return dict(self._last_numbers)

    @staticmethod
    def _enable_global_time(profile: rs.pipeline_profile) -> None:
        """Ask every sensor to timestamp frames on the host's clock.

        Args:
            profile: The started pipeline's profile.

        Already on by default (librealsense 2.58.3, D455) but set explicitly
        in case something turned it off. A failure is logged, not raised.
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

    def _apply_emitter(self, profile: rs.pipeline_profile) -> None:
        """Put the depth projector into the configured mode.

        Args:
            profile: The started pipeline's profile.

        **The write order matters**: the firmware refuses the obvious orderings
        (docs/decisions.md 18). Failures are logged, not raised, and the
        read-back is checked and warned about.
        """
        if self._config.depth is None:
            return
        try:
            sensor = profile.get_device().first_depth_sensor()
        except RuntimeError as exc:
            logger.warning("no depth sensor to set the emitter on: %s", exc)
            return

        mode = self._config.emitter
        steps = [
            (rs.option.emitter_on_off, 0.0),
            (rs.option.emitter_enabled, 0.0 if mode == "off" else 1.0),
        ]
        if mode == "alternating":
            steps.append((rs.option.emitter_on_off, 1.0))

        for option, value in steps:
            if not sensor.supports(option):
                logger.warning(
                    "this depth sensor does not support %s, so emitter mode %r "
                    "is not what will be recorded",
                    _option_name(option),
                    mode,
                )
                continue
            try:
                sensor.set_option(option, value)
            except RuntimeError as exc:
                logger.warning(
                    "could not set %s to %s: %s", _option_name(option), value, exc
                )

        got = {
            _option_name(option): sensor.get_option(option)
            for option in (rs.option.emitter_enabled, rs.option.emitter_on_off)
            if sensor.supports(option)
        }
        wanted = {
            "emitter_enabled": 0.0 if mode == "off" else 1.0,
            "emitter_on_off": 1.0 if mode == "alternating" else 0.0,
        }
        if any(got.get(name) != value for name, value in wanted.items()):
            logger.warning(
                "asked for emitter mode %r but the device reports %s; the "
                "recording is whatever the device did, not what was asked",
                mode,
                got,
            )
        else:
            logger.info("emitter mode: %s", mode)

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
                # A timeout and a vanished device raise the same way; only
                # STALL_LIMIT_S tells them apart.
                if self._stop:
                    return
                waited = time.monotonic() - last_frame_at
                if waited > STALL_LIMIT_S:
                    raise StreamError(
                        f"no frames for {waited:.1f}s: {exc}"
                    ) from exc
                continue

            # The axis the audio is also on; the SDK's own timestamps are
            # kept beside it, unconverted.
            received = time.monotonic()
            last_frame_at = received
            frame_set = self._assemble(composite, received)
            if frame_set is not None:
                yield frame_set

    def _assemble(
        self, composite: rs.composite_frame, received: float
    ) -> FrameSet | None:
        """Turn one SDK composite frame into a FrameSet.

        Args:
            composite: What ``wait_for_frames`` returned.
            received: ``time.monotonic()`` when it returned.

        Returns:
            The frame set, or None if an enabled stream was missing or every
            frame in it had already been delivered. Colour/depth skew never
            discards a set (docs/decisions.md 21).
        """
        motion = self._latest_motion() if self._config.motion else None

        # Taken before the align. Held, not copied, until the checks below
        # pass; librealsense reference-counts the handles.
        infrared_frames: tuple[rs.frame, rs.frame] | None = None
        if self._config.infrared:
            left = composite.get_infrared_frame(1)
            right = composite.get_infrared_frame(2)
            if not left or not right:
                return None
            infrared_frames = (left, right)

        if self._align is not None:
            composite = self._align.process(composite)

        # Identify the frames before copying any pixels, since the checks
        # below may reject the whole set.
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

        if infrared_frames is not None:
            frames["ir1"], frames["ir2"] = infrared_frames

        if self._timestamp_domain == "unknown":
            self._note_timestamp_domain(next(iter(frames.values())))

        if not self._is_new(frames):
            return None

        metadata: dict[str, dict[str, int]] = {
            name: _read_metadata(frame, self._fields_for(name, frame))
            for name, frame in frames.items()
            # The infrared pair carries the depth sensor's metadata again.
            if not name.startswith("ir")
        }
        # Copied, not viewed: the SDK reuses these buffers once released.
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
        infrared = (
            (
                np.asanyarray(frames["ir1"].get_data()).copy(),
                np.asanyarray(frames["ir2"].get_data()).copy(),
            )
            if infrared_frames is not None
            else None
        )

        self._index += 1
        return FrameSet(
            index=self._index,
            color_timestamp_ms=(
                float(frames["color"].get_timestamp()) if "color" in frames else None
            ),
            depth_timestamp_ms=(
                float(frames["depth"].get_timestamp()) if "depth" in frames else None
            ),
            received_monotonic=received,
            color=color,
            depth=depth,
            calibration=self.calibration,
            motion=motion,
            metadata=metadata or None,
            timestamp_domain=self._timestamp_domain,
            color_format=self._config.color_format,
            infrared=infrared,
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
            self._count_skip("duplicate")
            return False
        self._last_numbers = numbers
        return True

    def _count_skip(self, reason: str) -> None:
        """Record a discarded set, separating startup from the stream proper.

        Args:
            reason: ``"duplicate"``, currently the only reason.

        Before the first delivered set it counts as warm-up, after it as a
        duplicate.
        """
        if self._index == 0:
            self._skipped_warmup += 1
        else:
            self._skipped_duplicate += 1

    def _note_timestamp_domain(self, frame: rs.frame) -> None:
        """Record what the SDK's timestamps mean, once, and say so in the log.

        Args:
            frame: Any frame from the stream.

        Anything other than ``global_time`` is logged as a warning; see
        docs/features.md "Timing".
        """
        self._timestamp_domain = str(frame.get_frame_timestamp_domain()).rsplit(
            ".", 1
        )[-1]
        if self._timestamp_domain == "global_time":
            logger.info(
                "frame timestamps are epoch milliseconds (global_time), one "
                "drift-corrected clock for colour and depth"
            )
        elif self._timestamp_domain == "system_time":
            logger.warning(
                "frame timestamps are system_time: each stream was stamped by the "
                "host on arrival, so colour and depth are not on one clock"
            )
        else:
            logger.warning(
                "frame timestamps are in domain %r, not mapped to the host's "
                "clock",
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

    def _latest_motion(self) -> Motion | None:
        """The newest buffered sample of each inertial stream.

        Returns:
            The pair, or None if nothing has arrived yet. Peeks; never drains.
        """
        with self._motion_lock:
            if not self._motion:
                return None
            accel: tuple[float, float, float] | None = None
            gyro: tuple[float, float, float] | None = None
            for sample in reversed(self._motion):
                if accel is None and sample.stream == "accel":
                    accel = sample.values
                elif gyro is None and sample.stream == "gyro":
                    gyro = sample.values
                if accel is not None and gyro is not None:
                    break
        return Motion(accel=accel, gyro=gyro)


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
