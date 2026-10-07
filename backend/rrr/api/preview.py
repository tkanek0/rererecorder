"""Turning measurements into pictures for the browser.

Presentation only: nothing recorded depends on this, and it is the one place
depth acquires a range and a color scale. Images stay BGR, as OpenCV encodes
them, and are downscaled before encoding.
"""

from __future__ import annotations

from typing import Literal, get_args

import cv2
import numpy as np
from realsense_adapter import FrameSet, color_to_bgr

#: Which preview a request is asking for.
Kind = Literal["color", "depth", "ir1", "ir2"]
KINDS: tuple[str, ...] = get_args(Kind)

#: Near and far clip for colorization, in metres, sized for indoors.
NEAR_M = 0.3
FAR_M = 6.0

#: multipart boundary for the MJPEG streams.
BOUNDARY = "frame"

#: Content type for ``multipart/x-mixed-replace``, which an ``<img>`` shows as
#: live video.
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"


def colorize_depth(depth: np.ndarray, depth_scale: float) -> np.ndarray:
    """Render a depth image as a color picture, NEAR_M to FAR_M on turbo.

    Turbo, because jet's perceptually uneven bands invent and hide edges.
    Anything further than FAR_M is clipped rather than dropped, so a far wall
    stays visible.

    Args:
        depth: ``(height, width)`` uint16 raw depth.
        depth_scale: Metres per raw depth unit.

    Returns:
        ``(height, width, 3)`` uint8 BGR. Unmeasured pixels are black, which
        is outside every scale here so it cannot pass for a reading.
    """
    metres = depth.astype(np.float32) * depth_scale
    scaled = np.clip((metres - NEAR_M) / (FAR_M - NEAR_M), 0.0, 1.0)
    colored = cv2.applyColorMap((scaled * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    colored[depth == 0] = 0
    return colored


def render(
    frames: FrameSet,
    kind: Kind,
) -> np.ndarray | None:
    """Pick the image a preview request wants, ready to encode.

    Args:
        frames: The frame set to draw from.
        kind: Which stream.

    Returns:
        The image, or None if that stream is not in this recording. Color and
        depth come back as BGR; infrared stays single-channel.
    """
    if kind == "color":
        return color_to_bgr(frames)
    if kind == "depth":
        if frames.depth is None:
            return None
        return colorize_depth(frames.depth, frames.calibration.depth_scale)
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
