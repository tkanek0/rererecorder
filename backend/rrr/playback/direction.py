"""The array's own direction readings, on the video clock and in the camera."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

import numpy as np
from realsense_adapter import Extrinsics

from rrr.timeline import Rig, read_jsonl


@dataclass(frozen=True)
class Direction:
    """One direction reading on the video clock.

    Attributes:
        time: Video-clock time of the reading, in seconds.
        angle: Degrees, in whichever frame the producer says.
        voice: Whether the firmware flagged voice activity.
    """

    time: float
    angle: float
    voice: bool


def read_directions(path: str, offset: float) -> list[Direction] | None:
    """Read the session's DOA track onto the video clock, in array coordinates.

    Args:
        path: The session's ``doa.jsonl``.
        offset: Seconds to add to an audio time to reach the video time of the
            same instant.

    Returns:
        The readings in time order, or None if the session has no track.
        Angles are the firmware's: 0 deg is the array's +Y, increasing
        clockwise toward +X.
    """
    try:
        readings = read_jsonl(
            path,
            lambda raw: Direction(
                time=float(raw["t"]) + offset,
                angle=float(raw["angle"]) % 360.0,
                voice=bool(raw["voice"]),
            ),
        )
    except FileNotFoundError:
        return None
    readings.sort(key=lambda reading: reading.time)
    return readings


def in_colour_camera(
    readings: list[Direction], rig: Rig, depth_to_color: Extrinsics | None
) -> list[Direction] | None:
    """Rotate array-frame readings into bearings in the colour camera.

    Args:
        readings: Array-frame readings, as :func:`read_directions` returns.
        rig: The mounting from the array to the depth frame.
        depth_to_color: The camera's own depth-to-colour extrinsics.

    Returns:
        Bearings about the colour camera's vertical axis, 0 deg straight ahead
        and increasing toward +X - or None if either transform is unknown.
        An unmeasured mounting is never replaced by an identity.
    """
    if not rig.known or depth_to_color is None:
        return None
    rotation = np.asarray(depth_to_color.rotation).reshape(3, 3) @ np.asarray(
        rig.rotation
    ).reshape(3, 3)
    corrected = []
    for reading in readings:
        radians = math.radians(reading.angle)
        ray = rotation @ np.array([math.sin(radians), math.cos(radians), 0.0])
        bearing = math.degrees(math.atan2(float(ray[0]), float(ray[2]))) % 360.0
        corrected.append(Direction(reading.time, bearing, reading.voice))
    return corrected


def direction_at(
    readings: list[Direction], stamp: float, stale_s: float
) -> Direction | None:
    """Return the latest reading at or before an instant, unless it is stale.

    Args:
        readings: Readings in time order.
        stamp: Video-clock time to look at.
        stale_s: How old a reading may be and still count.

    Returns:
        The reading, or None if there is none recent enough.
    """
    position = bisect.bisect_right(readings, stamp, key=lambda r: r.time) - 1
    if position < 0:
        return None
    reading = readings[position]
    return reading if stamp - reading.time <= stale_s else None
