"""The direction the array currently hears, polled.

The XVF-3000 holds a current value rather than streaming it, so it is read on a
schedule. Times are ``time.monotonic()``, the axis audio is on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from respeaker_adapter import find_tuning

from .worker import SharedWorker


@dataclass(frozen=True)
class Reading:
    """One sample of what the chip believes about the sound field.

    Attributes:
        angle: Direction of arrival in degrees, 0-359.
        voice_activity: Whether the chip's VAD is currently firing.
        captured_at: ``time.monotonic()`` when the value was read.
        index: Assigned on publication, to tell a new reading from an old one.
    """

    angle: int
    voice_activity: bool
    captured_at: float
    index: int = 0


class DoaTap(SharedWorker[Reading]):
    """Poll the array's direction estimate."""

    name = "doa-tap"

    def __init__(self, idle_shutdown_s: float, *, poll_hz: float) -> None:
        """Prepare the tap without opening the device.

        Args:
            idle_shutdown_s: Seconds polling continues with no consumers.
            poll_hz: How often to ask the chip for its current angle.
        """
        super().__init__(idle_shutdown_s)
        self._interval = 1.0 / max(1e-3, poll_hz)

    def _stamp(self, item: Reading, index: int) -> Reading:
        return replace(item, index=index)

    def _serve(self) -> None:
        device = find_tuning()
        try:
            next_at = time.monotonic()
            while not self._should_stop():
                # Read together so both describe the same instant.
                angle, voice = device.direction, device.voice_activity
                self._publish(Reading(angle, voice, time.monotonic()))
                # To a schedule, not a fixed sleep, or the transfer time would
                # roughly halve the rate; after falling behind, resync.
                next_at = max(next_at + self._interval, time.monotonic())
                time.sleep(max(0.0, next_at - time.monotonic()))
        finally:
            device.close()
