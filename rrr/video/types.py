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
    """

    color: Intrinsics | None
    depth: Intrinsics | None
    depth_scale: float
    depth_to_color: Extrinsics | None
    aligned: bool

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
        timestamp_ms: Frame timestamp in milliseconds, as
            ``frame.get_timestamp()`` reports it. What it means depends on
            ``timestamp_domain``, which is why they travel together.

            Measured on a D455 with librealsense 2.58.3: the domain is
            ``global_time`` by default - ``global_time_enabled`` reads 1.0 on
            all three sensors - and the value is **epoch milliseconds**, fitted
            by the SDK onto the host's realtime clock. So it is directly
            comparable with ``time.time() * 1000``, to within about 10 ms: the
            fit is re-estimated while streaming, and over one minute the offset
            was observed to move from -11 ms to +7 ms.

            The upstream realsense-playground documents this field as being on
            a firmware clock, useful for intervals but not for wall time. That
            is what the SDK reports with global time *disabled*; it is not what
            this camera does out of the box, and the difference is what makes
            synchronising with a second device possible at all.
        received_at: ``time.monotonic()`` when the set was assembled - that is,
            when ``wait_for_frames`` returned, not when the shutter opened.
            Measured 2-11 ms after ``timestamp_ms``.
        clock: Both host clocks, read when this set was assembled. What converts
            ``timestamp_ms`` onto the monotonic axis the audio is on. None for a
            set built without one, in which case ``capture_monotonic`` falls
            back to ``received_at``.
        timestamp_domain: What the SDK said ``timestamp_ms`` means. Recorded
            rather than assumed: if it reads ``hardware_clock``, the value is on
            the device's own clock and cannot be placed against anything else.
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
    timestamp_ms: float
    received_at: float
    color: np.ndarray | None
    depth: np.ndarray | None
    calibration: Calibration
    motion: Motion | None
    metadata: dict[str, dict[str, int]] | None = None
    clock: ClockPair | None = None
    timestamp_domain: str = "unknown"
    color_format: str = "rgb8"
    infrared: tuple[np.ndarray, np.ndarray] | None = None

    @property
    def capture_monotonic(self) -> float:
        """When this frame was captured, on the axis everything else uses.

        Returns:
            The frame's instant as ``time.monotonic()`` would have reported it.

        This is the number to compare with an audio sample's time. It is
        computed from ``timestamp_ms`` - the camera's own estimate of when the
        frame happened - rather than from ``received_at``, which includes
        however long the frame spent in the SDK and the USB stack.

        Falls back to ``received_at`` when the timestamp cannot be placed: no
        clock pair, or a domain other than ``global_time``. That is a real loss
        of accuracy - 2 to 11 ms, measured - so it is worth knowing which one
        happened, and ``timestamp_domain`` says.
        """
        if self.clock is not None and self.timestamp_domain == "global_time":
            return self.clock.epoch_ms_to_monotonic(self.timestamp_ms)
        return self.received_at
