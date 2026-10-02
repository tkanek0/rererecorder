"""Value types describing what the camera produced.

Plain dataclasses with no pyrealsense2 import, so consumers work without the
SDK. Depth stays raw ``z16``; scale it with ``Calibration.depth_scale``.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
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
        rotation: 3x3 matrix, row-major (transposed from the SDK's
            column-major).
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

    Recorded even when it is the identity; see docs/features.md "Recording".

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

    Attributes:
        accel: The accelerometer's correction, or None if the device does not
            report one.
        gyro: The gyroscope's correction.
        depth_to_accel: Transform from the depth stream's frame to the
            accelerometer's.
        depth_to_gyro: The same for the gyroscope, recorded separately so
            that it can be checked against ``depth_to_accel``.
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
            frame. The first should be the identity on a D400.
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
            The baseline, or None unless both infrared streams were recorded.
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

    A recording keeps every one; see docs/decisions.md 12.

    Attributes:
        stream: ``"accel"`` or ``"gyro"``.
        timestamp_ms: The sensor's own timestamp in milliseconds; epoch
            milliseconds under ``global_time``, like the video frames.
        x: Acceleration in m/s^2, or angular velocity in rad/s.
        y: The same, second axis.
        z: The same, third axis.
        clock: Host clocks for converting ``timestamp_ms`` onto the monotonic
            axis. Attached on read-back from the archive's anchor, not per
            sample.
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

    The newest sample of each stream when the frame was assembled, for preview
    only; recordings store :class:`MotionSample` (docs/decisions.md 12).

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
        width, all uint8. See docs/decisions.md 4.
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

    The arrays are shared by every consumer: copy before writing to one.

    Attributes:
        index: Monotonically increasing counter assigned by whatever produced
            this set. Used to tell a new frame from one already handled.
        color_timestamp_ms: The colour frame's own ``frame.get_timestamp()``,
            in milliseconds, or None if colour is disabled. What it means
            depends on ``timestamp_domain``.
        depth_timestamp_ms: The depth frame's own ``frame.get_timestamp()``,
            shared by both infrared frames, or None if depth is disabled.
            Never used to discard a set (docs/decisions.md 21).
        received_monotonic: ``time.monotonic()`` when ``wait_for_frames``
            returned; the axis the audio is also on.
        timestamp_domain: What the SDK said the two timestamps mean; see
            docs/features.md "Timing".
        color: The colour image as the sensor produced it, or None if
            disabled. Its shape depends on ``color_format``: ``(height, width)``
            uint16 for ``"yuyv"`` - each element one pixel's two bytes - or
            ``(height, width, 3)`` uint8 for ``"rgb8"``.
        color_format: Which of those two this is.
        depth: ``(height, width)`` uint16 raw z16, or None if disabled. Zero
            means no measurement, not zero distance.
        infrared: The two raw images the depth was computed from, as
            ``(left, right)``, each ``(height, width)`` uint8 - or None if they
            were not recorded.
        calibration: Calibration in force for these images.
        motion: Latest inertial sample, or None if motion is disabled.
        metadata: What the firmware reported about these frames, per stream:
            ``{"depth": {"actual_exposure": 32783, ...}, "color": {...}}``.
            Reading it costs nothing measurable at 30 fps.
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


def color_to_bgr(frames: FrameSet) -> np.ndarray | None:
    """Convert a set's colour image to BGR, whatever format it was recorded in.

    Args:
        frames: The set to read.

    Returns:
        ``(height, width, 3)`` uint8 BGR, or None if colour is disabled.
    """
    if frames.color is None:
        return None
    if frames.color_format == "yuyv":
        return cv2.cvtColor(_yuyv_pairs(frames.color), cv2.COLOR_YUV2BGR_YUY2)
    return cv2.cvtColor(frames.color, cv2.COLOR_RGB2BGR)


def color_to_rgb(frames: FrameSet) -> np.ndarray | None:
    """Convert a set's colour image to RGB, whatever format it was recorded in.

    Args:
        frames: The set to read.

    Returns:
        ``(height, width, 3)`` uint8 RGB - the recorded array itself when it
        already was RGB - or None if colour is disabled.
    """
    if frames.color is None:
        return None
    if frames.color_format == "yuyv":
        return cv2.cvtColor(_yuyv_pairs(frames.color), cv2.COLOR_YUV2RGB_YUY2)
    return frames.color


def _yuyv_pairs(color: np.ndarray) -> np.ndarray:
    """View packed YUYV as the two-channel byte pairs ``cvtColor`` expects.

    Args:
        color: ``(height, width)`` uint16, one element per pixel.

    Returns:
        ``(height, width, 2)`` uint8, sharing memory with ``color``.
    """
    height, width = color.shape
    return color.view(np.uint8).reshape(height, width, 2)
