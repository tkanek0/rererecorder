"""A top-down compass for one direction reading."""

from __future__ import annotations

import math

import cv2
import numpy as np

from rrr.playback import Direction


def draw_compass(image: np.ndarray, reading: Direction, *, camera: bool) -> None:
    """Draw a reading as a compass in the top right, and label it at the bottom.

    Args:
        image: BGR image to draw on, in place.
        reading: The direction to show; 0 deg points up.
        camera: Whether the angle is in the color camera's frame rather than
            the array's, which only changes the label.
    """
    radius = max(28, min(image.shape[:2]) // 14)
    centre = (image.shape[1] - radius - 18, radius + 18)
    color = (40, 220, 40) if reading.voice else (160, 160, 160)
    cv2.circle(image, centre, radius, (240, 240, 240), 2, cv2.LINE_AA)
    radians = math.radians(reading.angle)
    tip = (
        round(centre[0] + radius * 0.82 * math.sin(radians)),
        round(centre[1] - radius * 0.82 * math.cos(radians)),
    )
    cv2.arrowedLine(image, centre, tip, color, 3, cv2.LINE_AA, tipLength=0.25)
    label = f"DOA {reading.angle:.0f} deg {'camera' if camera else 'array'}"
    cv2.putText(
        image,
        label,
        (12, image.shape[0] - 16),
        cv2.FONT_HERSHEY_SIMPLEX,
        max(0.45, image.shape[1] / 1800.0),
        color,
        2,
        cv2.LINE_AA,
    )
