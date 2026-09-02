"""Turning measurements into pictures for the browser.

Presentation only. Nothing recorded depends on anything here, and nothing here
is used to decide anything - which is why it lives in ``server/`` rather than in
``video/``: a preview is a choice a viewer makes, not a property of the
measurement. Depth is raw z16 everywhere else in this repository; this is the
one place it acquires a range and a colour scale.

Images are carried as BGR because that is what OpenCV encodes, and because the
colour stream arrives as YUYV whose conversion lands in BGR directly. Converting
to RGB in between would cost a pass over 1 MB per frame to arrive back where it
started.

Everything is downscaled before encoding. A 1280x800 JPEG is about 100 KB and
looks no better in a 640-wide panel, so full size would spend CPU and bandwidth
on pixels the page throws away.
"""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np

from video import FrameSet

#: Which preview a request is asking for.
Kind = Literal["color", "depth", "ir1", "ir2"]

#: Colour scales offered by name, so a query parameter can pick one.
#:
#: Turbo rather than the jet that RealSense samples traditionally use: jet's
#: bands are perceptually uneven, which invents edges in a smooth surface and
#: hides real ones elsewhere.
COLORMAPS: dict[str, int] = {
    "turbo": cv2.COLORMAP_TURBO,
    "jet": cv2.COLORMAP_JET,
    "viridis": cv2.COLORMAP_VIRIDIS,
    "magma": cv2.COLORMAP_MAGMA,
}

#: Default near and far clip for colorization, in metres. The D455's usable
#: range starts around 0.4 m; showing 0-20 m instead spends almost the whole
#: scale on an indoor scene.
DEFAULT_NEAR_M = 0.3
DEFAULT_FAR_M = 6.0

#: multipart boundary for the MJPEG streams. Any token works as long as it
#: cannot appear in a JPEG payload.
BOUNDARY = "frame"

#: Content type for ``multipart/x-mixed-replace``. The browser replaces the
#: previous part with each new one, which is the whole trick: an ``<img>``
#: pointed at this shows live video with no JavaScript at all.
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
        ``(height, width, 3)`` uint8 BGR. Pixels the device could not measure
        are black, which is outside every scale here and so cannot be mistaken
        for a real reading - a quarter of a typical indoor depth frame is
        unmeasured, and showing that as "very close" would be a lie.
    """
    metres = depth.astype(np.float32) * depth_scale
    span = max(far_m - near_m, 1e-6)
    scaled = np.clip((metres - near_m) / span, 0.0, 1.0)
    coloured = cv2.applyColorMap(
        (scaled * 255).astype(np.uint8), COLORMAPS.get(colormap, cv2.COLORMAP_TURBO)
    )
    coloured[depth == 0] = 0
    return coloured


def to_bgr(frames: FrameSet) -> np.ndarray | None:
    """Convert a set's colour image to BGR, whatever format it arrived in.

    Args:
        frames: The set to read.

    Returns:
        ``(height, width, 3)`` uint8 BGR, or None if colour is disabled.
    """
    if frames.color is None:
        return None
    if frames.color_format == "yuyv":
        # One call: the packed YUYV buffer straight to BGR. Doing it by hand
        # through the split planes would be three passes and a wrong answer at
        # the chroma edges.
        #
        # Reshaped to two channels first. A uint16 view of the buffer is one
        # channel of double width, and cvtColor wants the pair of bytes to be
        # the channel axis - it rejects the flat view outright rather than
        # guessing, which is the good outcome.
        height, width = frames.color.shape
        return cv2.cvtColor(
            frames.color.view(np.uint8).reshape(height, width, 2),
            cv2.COLOR_YUV2BGR_YUY2,
        )
    return cv2.cvtColor(frames.color, cv2.COLOR_RGB2BGR)


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
        depth come back as BGR; the infrared streams stay single-channel, which
        JPEG encodes as greyscale and is a third of the work.
    """
    if kind == "color":
        return to_bgr(frames)
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

    INTER_AREA rather than the default bilinear: shrinking by more than a factor
    of two with bilinear samples too sparsely and aliases, which on a depth
    colormap looks like structure that is not there.
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

    ``Content-Length`` is included deliberately. Without it the browser has to
    scan for the next boundary before it can decode, which shows up as the
    preview running a frame behind.
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
