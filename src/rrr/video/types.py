"""Value types describing what the camera produced.

Nothing here imports pyrealsense2: these are plain dataclasses so that a
consumer can be written, and tested, against recorded data without the SDK
being present.

Depth is kept as the raw ``z16`` the device emits, never as meters. The
conversion needs ``Calibration.depth_scale``, and doing it here would force a
float array - four times the memory - onto every consumer, including the ones
that only want to draw a picture.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from rrr.timeline import ClockPair


@dataclass(frozen=True)
class Intrinsics:
    """Pinhole parameters of one video stream.

    Attributes:
        width: Image width in pixels.
        height: Image height in pixels.
        fx: Focal length along x, in pixels.
        fy: Focal length along y, in pixels.
        ppx: Principal point x, in pixels.
        ppy: Principal point y, in pixels.
        model: Distortion model name as the SDK reports it.
        coeffs: Distortion coefficients.
    """

    width: int
    height: int
    fx: float
    fy: float
    ppx: float
    ppy: float
    model: str
    coeffs: tuple[float, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of these intrinsics."""
        return {
            "width": self.width,
            "height": self.height,
            "fx": self.fx,
            "fy": self.fy,
            "ppx": self.ppx,
            "ppy": self.ppy,
            "model": self.model,
            "coeffs": list(self.coeffs),
        }


@dataclass(frozen=True)
class Extrinsics:
    """Rigid transform between two streams.

    Attributes:
        rotation: 3x3 matrix, row-major. The SDK hands out column-major; it is
            transposed on the way in so that this is an ordinary numpy-style
            matrix.
        translation: Offset in meters.
    """

    rotation: tuple[float, ...]
    translation: tuple[float, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this transform."""
        return {"rotation": list(self.rotation), "translation": list(self.translation)}

    @staticmethod
    def identity() -> Extrinsics:
        """Return the transform that changes nothing."""
        return Extrinsics(
            rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            translation=(0.0, 0.0, 0.0),
        )


@dataclass(frozen=True)
class MotionIntrinsics:
    """The inertial sensor's own correction, as the device was calibrated.

    An accelerometer and a gyroscope are not read straight off: each axis has a
    scale, the three axes are not exactly orthogonal, and each carries a bias.
    A device can store the correction for its own unit, and using the raw
    samples without it is a needless source of drift in anything that
    integrates them.

    **This D455 does not have one.** Measured: the correction reads back as the
    identity with zero bias, which is what librealsense reports for a unit that
    was never IMU-calibrated at the factory. That is consistent with the other
    measurement this repository already makes - ``inspect`` puts the stationary
    accelerometer's magnitude at 9.69 m/s^2 against a true 9.81, which is 1.2%
    out and about what an uncalibrated axis scale looks like. Intel's
    ``rs-imu-calibration.py`` writes one if it turns out to matter.

    Recorded either way, because the two cases have to be distinguishable: an
    identity that came from the device is not the same claim as no calibration
    at all, and only the recording can say which one a session had.

    Attributes:
        data: The 3x4 correction, row-major - a 3x3 scale-and-misalignment
            matrix with a bias column. Corrected = ``data[:, :3] @ raw +
            data[:, 3]``.
        noise_variances: Per-axis noise variance, as the device reports it.
        bias_variances: Per-axis bias variance.
    """

    data: tuple[float, ...]
    noise_variances: tuple[float, float, float]
    bias_variances: tuple[float, float, float]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of these intrinsics."""
        return {
            "data": list(self.data),
            "noise_variances": list(self.noise_variances),
            "bias_variances": list(self.bias_variances),
        }


@dataclass(frozen=True)
class MotionCalibration:
    """Where the inertial sensor sits, and how to correct what it reports.

    Both halves are needed by anything that fuses the inertial samples with the
    images, and neither is recoverable afterwards: an archive holding samples
    without the transform to the camera is a set of numbers in an unnamed
    frame.

    Attributes:
        accel: The accelerometer's correction, or None if the device does not
            report one.
        gyro: The gyroscope's correction.
        depth_to_accel: Transform from the depth stream's frame to the
            accelerometer's.
        depth_to_gyro: The same for the gyroscope. Recorded separately because
            the SDK reports them separately; on a D455 they are the same
            frame, which is worth being able to check rather than assume.
    """

    accel: MotionIntrinsics | None = None
    gyro: MotionIntrinsics | None = None
    depth_to_accel: Extrinsics | None = None
    depth_to_gyro: Extrinsics | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this calibration."""
        return {
            "accel": self.accel.as_dict() if self.accel else None,
            "gyro": self.gyro.as_dict() if self.gyro else None,
            "depth_to_accel": (
                self.depth_to_accel.as_dict() if self.depth_to_accel else None
            ),
            "depth_to_gyro": (
                self.depth_to_gyro.as_dict() if self.depth_to_gyro else None
            ),
        }


@dataclass(frozen=True)
class Calibration:
    """Everything needed to turn a depth image into metric 3D points.

    Attributes:
        color: Intrinsics of the color stream, or None if it is disabled.
        depth: Intrinsics that apply to the depth image *as delivered*. When
            depth is aligned to color this is the color stream's intrinsics,
            not the depth sensor's own - unprojecting with the latter would put
            every point in the wrong place.
        depth_scale: Meters per raw depth unit.
        depth_to_color: Transform from the depth frame to the color frame.
            Identity when the two are aligned.
        aligned: Whether depth was resampled into the color viewpoint.
        infrared: Intrinsics of the left and right infrared streams, each None
            if that stream was not recorded.
        depth_to_infrared: Transform from the depth frame to each infrared
            frame. On a D400 the depth image is computed in the left imager's
            frame, so the first of these should be the identity - recorded
            rather than assumed, because it is a claim that can be checked.
        motion: Where the inertial sensor sits and how to correct it, or None
            if it was not recorded.
    """

    color: Intrinsics | None
    depth: Intrinsics | None
    depth_scale: float
    depth_to_color: Extrinsics | None
    aligned: bool
    infrared: tuple[Intrinsics | None, Intrinsics | None] = (None, None)
    depth_to_infrared: tuple[Extrinsics | None, Extrinsics | None] = (None, None)
    motion: MotionCalibration | None = None

    @property
    def infrared_baseline_m(self) -> float | None:
        """Distance between the two infrared imagers, in metres.

        Returns:
            The baseline, or None if both infrared streams were not recorded.
            This is what fixes the scale of anything reconstructed from the
            pair, so it is worth being able to read without composing the two
            transforms by hand.
        """
        left, right = self.depth_to_infrared
        if left is None or right is None:
            return None
        return float(
            sum((a - b) ** 2 for a, b in zip(left.translation, right.translation))
            ** 0.5
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this calibration."""
        return {
            "color": self.color.as_dict() if self.color else None,
            "depth": self.depth.as_dict() if self.depth else None,
            "depth_scale": self.depth_scale,
            "depth_to_color": (
                self.depth_to_color.as_dict() if self.depth_to_color else None
            ),
            "aligned": self.aligned,
            "infrared": [
                entry.as_dict() if entry else None for entry in self.infrared
            ],
            "depth_to_infrared": [
                entry.as_dict() if entry else None for entry in self.depth_to_infrared
            ],
            "motion": self.motion.as_dict() if self.motion else None,
        }


@dataclass(frozen=True)
class DeviceInfo:
    """Identity of the physical camera.

    Attributes:
        name: Product name, e.g. "Intel RealSense D455".
        serial: Serial number.
        firmware: Firmware version string.
        usb_type: USB descriptor the SDK negotiated, e.g. "3.2".
    """

    name: str
    serial: str
    firmware: str
    usb_type: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serialisable view of this device."""
        return {
            "name": self.name,
            "serial": self.serial,
            "firmware": self.firmware,
            "usb_type": self.usb_type,
        }


@dataclass(frozen=True)
class MotionSample:
    """One inertial reading, as the sensor produced it.

    Distinct from :class:`Motion`, which holds whichever samples happened to be
    current when a video frame was assembled. This is the sample itself, with
    its own timestamp, and a recording keeps all of them: measured on a D455,
    the accelerometer runs at 482 Hz and the gyroscope at 478, so taking one of
    each per 30 fps frame discards 93% of what the sensor measured.

    Attributes:
        stream: ``"accel"`` or ``"gyro"``.
        timestamp_ms: The sensor's own timestamp in milliseconds. Epoch
            milliseconds while the motion module's timestamp domain is
            ``global_time``, which is set explicitly when it is opened - the
            same axis the video frames are on.
        x: Acceleration in m/s^2, or angular velocity in rad/s.
        y: The same, second axis.
        z: The same, third axis.
        clock: Host clocks for converting ``timestamp_ms`` onto the monotonic
            axis. Not read per sample - that would mean two syscalls 960 times a
            second - but attached when a recording is read back, from the
            archive's own anchor.
    """

    stream: str
    timestamp_ms: float
    x: float
    y: float
    z: float
    clock: ClockPair | None = None

    @property
    def capture_monotonic(self) -> float | None:
        """When this sample was taken, on the axis everything else uses.

        Returns:
            The instant, or None if no clock pair is attached.
        """
        if self.clock is None:
            return None
        return self.clock.epoch_ms_to_monotonic(self.timestamp_ms)

    @property
    def values(self) -> tuple[float, float, float]:
        """The reading as a tuple."""
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class Motion:
    """One sample from the inertial sensors.

    The D455 runs its IMU far faster than the video streams, so these are
    whichever samples were most recent when the frame was assembled rather than
    a value measured at the frame's own instant. Measured: 482 Hz accelerometer
    against 30 fps video.

    Kept for convenience - a preview or a quick attitude estimate wants one
    number per frame - but a recording stores every sample separately as
    :class:`MotionSample`. Reading this out of an archive and calling it the
    inertial data would be using a fourteenth of it.

    Attributes:
        accel: Acceleration in m/s^2, including gravity, as (x, y, z).
        gyro: Angular velocity in rad/s, as (x, y, z).
    """

    accel: tuple[float, float, float] | None
    gyro: tuple[float, float, float] | None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this sample."""
        return {
            "accel": list(self.accel) if self.accel else None,
            "gyro": list(self.gyro) if self.gyro else None,
        }


def split_yuyv(color: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Separate a YUYV image into its three planes.

    Args:
        color: ``(height, width)`` uint16, one element per pixel, as the SDK
            delivers YUYV.

    Returns:
        ``(y, u, v)``: luma at full width, and the two chroma planes at half
        width, all uint8.

    Stored this way rather than as the interleaved buffer because the
    interleaving defeats compression - Y and U alternate byte by byte, so
    neighbouring values are unrelated and a predictor has nothing to work with.
    Measured on real frames: 761 KB separated against 870 KB interleaved, and
    faster as well. Both are lossless; :func:`join_yuyv` is the proof.
    """
    raw = color.view(np.uint8).reshape(color.shape[0], color.shape[1], 2)
    return (
        raw[:, :, 0].copy(),
        raw[:, :, 1][:, 0::2].copy(),
        raw[:, :, 1][:, 1::2].copy(),
    )


def join_yuyv(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Reassemble a YUYV image from its planes.

    Args:
        y: Luma, ``(height, width)`` uint8.
        u: First chroma plane, ``(height, width // 2)`` uint8.
        v: Second chroma plane, same shape as ``u``.

    Returns:
        ``(height, width)`` uint16, byte-identical to what was split.
    """
    height, width = y.shape
    raw = np.empty((height, width, 2), np.uint8)
    raw[:, :, 0] = y
    raw[:, :, 1][:, 0::2] = u
    raw[:, :, 1][:, 1::2] = v
    return raw.reshape(height, width * 2).view(np.uint16)[:, :width]


@dataclass(frozen=True)
class FrameSet:
    """One synchronised set of frames.

    The arrays are owned by nobody and read by everyone: a source hands out the
    same object to every consumer, so a consumer that modifies an array in
    place corrupts what the others see. Copy before writing.

    Attributes:
        index: Monotonically increasing counter assigned by whatever produced
            this set. Used to tell a new frame from one already handled.
        color_timestamp_ms: The colour frame's own ``frame.get_timestamp()``,
            in milliseconds, or None if colour is disabled. What it means
            depends on ``timestamp_domain``.
        depth_timestamp_ms: The depth frame's own ``frame.get_timestamp()``,
            shared by both infrared frames - depth, IR1 and IR2 come off one
            imager in one exposure, so the three are never usefully compared
            against each other by timestamp, only against colour. None if
            depth is disabled.

            Comparing this against ``color_timestamp_ms`` is what a consumer
            uses to judge how far apart the two sensors' frames were taken -
            including spotting a stale depth frame reused under several
            colour frames, which shows up as the same value repeating across
            consecutive sets. Nothing here discards a set for that; it is
            left in the data to find, not decided for the reader.
        received_monotonic: ``time.monotonic()`` when this set was assembled -
            that is, when ``wait_for_frames`` returned. The one field every
            set is guaranteed to have, and the axis the audio recording is
            also on (see ``rrr.audio.capture``), so this is what a consumer
            uses to place a video frame against an audio sample.
        timestamp_domain: What the SDK said ``color_timestamp_ms`` and
            ``depth_timestamp_ms`` mean. ``global_time`` means the SDK fitted
            the device's own clock onto the host's realtime clock, so the two
            are directly comparable to each other and to wall-clock time.
            Anything else - measured as ``system_time`` on Windows - means
            each was stamped independently when its own frame reached the
            SDK, which is coarser but still usable: see
            ``docs/windows-native.md``.
        color: The colour image as the sensor produced it, or None if
            disabled. Its shape depends on ``color_format``: ``(height, width)``
            uint16 for ``"yuyv"`` - each element one pixel's two bytes - or
            ``(height, width, 3)`` uint8 for ``"rgb8"``.
        color_format: Which of those two this is. Carried with the array
            because nothing about a uint16 array says whether it holds YUYV.
        depth: ``(height, width)`` uint16 raw z16, or None if disabled. Zero
            means no measurement, not zero distance.
        infrared: The two raw images the depth was computed from, as
            ``(left, right)``, each ``(height, width)`` uint8 - or None if they
            were not recorded.

            These are the measurement; the depth is one interpretation of it.
            Keeping them is what makes a recording outlast the stereo matcher
            that produced its depth.
        calibration: Calibration in force for these images.
        motion: Latest inertial sample, or None if motion is disabled.
        metadata: What the firmware reported about these frames, per stream:
            ``{"depth": {"actual_exposure": 32783, ...}, "color": {...}}``.

            Reading it costs nothing measurable - 41 fields across both frames
            came out below the noise floor of a 33 ms frame interval - and it is
            what makes a measurement interpretable afterwards. Exposure, gain,
            laser power and the sensor's own timestamps are all in here.
    """

    index: int
    received_monotonic: float
    color: np.ndarray | None
    depth: np.ndarray | None
    calibration: Calibration
    motion: Motion | None
    color_timestamp_ms: float | None = None
    depth_timestamp_ms: float | None = None
    metadata: dict[str, dict[str, int]] | None = None
    timestamp_domain: str = "unknown"
    color_format: str = "rgb8"
    infrared: tuple[np.ndarray, np.ndarray] | None = None
