"""The camera, shared by the preview and the recording.

Pollers get the newest set via ``latest``; a recording gets every set via a
listener (docs/decisions.md 7, docs/design.md "The camera is shared").
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable

from realsense_adapter import Calibration, DeviceInfo, FrameSet, FrameSource

from .worker import SharedWorker


class FrameHub(SharedWorker[FrameSet]):
    """Keeps the most recent frame set available to any number of readers."""

    name = "frame-hub"

    def __init__(
        self, open_source: Callable[[], FrameSource], idle_shutdown_s: float
    ) -> None:
        """Prepare the hub without opening anything.

        Args:
            open_source: Called on the hub's thread to create and open a source,
                and again after a reconnect or a restart.
            idle_shutdown_s: Seconds the source stays open with no consumers.
        """
        super().__init__(idle_shutdown_s)
        self._open_source = open_source
        self._generation = 0
        self._wanted_generation = 0
        self._source: FrameSource | None = None
        self._device: DeviceInfo | None = None
        self._source_lock = threading.Lock()

    def restart(self) -> None:
        """Close the current source and open a fresh one.

        For a changed stream configuration, which the SDK only accepts at
        pipeline start. Also clears a failure. With no consumer it only records
        the intent; the next consumer opens with the new configuration.
        """
        with self._lock:
            self._wanted_generation += 1
            self._floor = self._index
        self.reconnect()

    @property
    def device(self) -> DeviceInfo | None:
        """Identity of the camera, once it has been opened at least once."""
        with self._source_lock:
            return self._device

    @property
    def source(self) -> FrameSource | None:
        """The source currently open, for device-level controls.

        The hub may replace it at any moment, so a call on it may raise; treat
        that as "the stream restarted".
        """
        with self._source_lock:
            return self._source

    @property
    def calibration(self) -> Calibration | None:
        """Calibration of the newest frame set, or None if there is none."""
        with self._lock:
            return self._latest.calibration if self._latest else None

    def _stamp(self, item: FrameSet, index: int) -> FrameSet:
        return dataclasses.replace(item, index=index)

    def _superseded(self) -> bool:
        with self._lock:
            return self._generation != self._wanted_generation

    def _serve(self) -> None:
        # Loops only to follow a restart.
        while not self._should_stop():
            with self._lock:
                self._generation = self._wanted_generation
            with self._open_source() as source:
                with self._source_lock:
                    self._source = source
                    self._device = getattr(source, "device", None)
                for frame_set in source.frames():
                    if self._should_stop() or self._superseded():
                        break
                    self._publish(frame_set)
            self._closed()

    def _closed(self) -> None:
        with self._source_lock:
            self._source = None
