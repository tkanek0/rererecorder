"""Turning measurements into pictures for the browser.

Presentation only: nothing recorded depends on this, and it is the one place
depth acquires a range and a colour scale. Images stay BGR, as OpenCV encodes
them, and are downscaled before encoding.
"""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np

from rrr.video import FrameSet, color_to_bgr

#: Which preview a request is asking for.
Kind = Literal["color", "depth", "ir1", "ir2"]

#: Colour scales offered by name, so a query parameter can pick one. Turbo is
#: the default: jet's perceptually uneven bands invent and hide edges.
COLORMAPS: dict[str, int] = {
    "turbo": cv2.COLORMAP_TURBO,
    "jet": cv2.COLORMAP_JET,
    "viridis": cv2.COLORMAP_VIRIDIS,
    "magma": cv2.COLORMAP_MAGMA,
}

#: Default near and far clip for colorization, in metres, sized for indoors.
DEFAULT_NEAR_M = 0.3
DEFAULT_FAR_M = 6.0

#: multipart boundary for the MJPEG streams.
BOUNDARY = "frame"

#: Content type for ``multipart/x-mixed-replace``, which an ``<img>`` shows as
#: live video.
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"


def colorize_depth(
    depth: np.ndarray,
    depth_scale: float,
    near_m: float = DEFAULT_NEAR_M,
    far_m: float = DEFAULT_FAR_M,
    colormap: str = "turbo",
) -> np.ndarray:
    """Render a depth image as a colour picture.

    Args:
        depth: ``(height, width)`` uint16 raw depth.
        depth_scale: Metres per raw depth unit.
        near_m: Distance mapped to the low end of the scale.
        far_m: Distance mapped to the high end. Anything further is clipped
            rather than dropped, so a far wall stays visible.
        colormap: Key from :data:`COLORMAPS`. An unknown name falls back to
            turbo.

    Returns:
        ``(height, width, 3)`` uint8 BGR. Unmeasured pixels are black, which
        is outside every scale here so it cannot pass for a reading.
    """
    metres = depth.astype(np.float32) * depth_scale
    span = max(far_m - near_m, 1e-6)
    scaled = np.clip((metres - near_m) / span, 0.0, 1.0)
    coloured = cv2.applyColorMap(
        (scaled * 255).astype(np.uint8), COLORMAPS.get(colormap, cv2.COLORMAP_TURBO)
    )
    coloured[depth == 0] = 0
    return coloured


def to_bgr_from_planes(
    y: np.ndarray, u: np.ndarray, v: np.ndarray
) -> np.ndarray:
    """Convert stored YUYV planes to BGR without rebuilding the packed buffer.

    Args:
        y: Luma, ``(height, width)`` uint8.
        u: First chroma plane, half width.
        v: Second chroma plane, half width.

    Returns:
        ``(height, width, 3)`` uint8 BGR.
    """
    height, width = y.shape
    yuv = np.empty((height, width, 3), np.uint8)
    yuv[:, :, 0] = y
    # 4:2:2: repeat each chroma sample, not interpolate, to match color_to_bgr.
    yuv[:, :, 1] = np.repeat(u, 2, axis=1)
    yuv[:, :, 2] = np.repeat(v, 2, axis=1)
    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)


def render(
    frames: FrameSet,
    kind: Kind,
    *,
    near_m: float = DEFAULT_NEAR_M,
    far_m: float = DEFAULT_FAR_M,
    colormap: str = "turbo",
) -> np.ndarray | None:
    """Pick the image a preview request wants, ready to encode.

    Args:
        frames: The frame set to draw from.
        kind: Which stream.
        near_m: Near clip for depth colorization.
        far_m: Far clip for depth colorization.
        colormap: Colour scale name for depth.

    Returns:
        The image, or None if that stream is not in this recording. Colour and
        depth come back as BGR; infrared stays single-channel.
    """
    if kind == "color":
        return color_to_bgr(frames)
    if kind == "depth":
        if frames.depth is None:
            return None
        return colorize_depth(
            frames.depth,
            frames.calibration.depth_scale,
            near_m=near_m,
            far_m=far_m,
            colormap=colormap,
        )
    if frames.infrared is None:
        return None
    return frames.infrared[0] if kind == "ir1" else frames.infrared[1]


def downscale(image: np.ndarray, width: int) -> np.ndarray:
    """Shrink an image to a target width, keeping its aspect ratio.

    Args:
        image: The image to shrink.
        width: Target width in pixels. A width at or above the image's own
            returns it untouched rather than upscaling.

    Returns:
        The resized image.

    INTER_AREA, because bilinear aliases when shrinking by more than two.
    """
    if width <= 0 or width >= image.shape[1]:
        return image
    height = max(1, round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def encode_jpeg(image: np.ndarray, quality: int = 80) -> bytes:
    """Encode an image as JPEG.

    Args:
        image: BGR or single-channel uint8.
        quality: JPEG quality, 1-100.

    Returns:
        The encoded bytes.

    Raises:
        RuntimeError: If OpenCV refused to encode it.
    """
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buffer.tobytes()


def mjpeg_part(jpeg: bytes) -> bytes:
    """Wrap one JPEG as a multipart part.

    Args:
        jpeg: The encoded frame.

    Returns:
        The part, ready to write to the response.

    ``Content-Length`` lets the browser decode without waiting for the next
    boundary, which would otherwise leave the preview a frame behind.
    """
    return b"".join(
        (
            f"--{BOUNDARY}\r\n".encode(),
            b"Content-Type: image/jpeg\r\n",
            f"Content-Length: {len(jpeg)}\r\n\r\n".encode(),
            jpeg,
            b"\r\n",
        )
    )
