"""Everything the server owns for its life: one camera hub and one recorder."""

from __future__ import annotations

import logging
import threading

from realsense_adapter import (
    DeviceInfo,
    LiveSource,
    StreamConfig,
    list_devices,
)

from rrr.devices import FrameHub
from rrr.recorder import SessionRecorder
from rrr.recorder import config as recording_config

logger = logging.getLogger(__name__)


class State:
    """Everything the server owns, for the life of the process.

    One hub, one recorder. The recorder is long-lived because it holds the
    audio taps, which must be closed properly (see ``SessionRecorder.close``).
    """

    def __init__(self) -> None:
        self.hub = FrameHub(self._open_camera, recording_config.IDLE_SHUTDOWN_S)
        self.recorder = SessionRecorder(
            recording_config.SESSIONS_ROOT,
            streams=recording_config.DEFAULT_STREAMS,
            serial=recording_config.SERIAL,
            record_video=recording_config.RECORD_VIDEO,
            record_audio=recording_config.RECORD_AUDIO,
            record_doa=recording_config.RECORD_DOA,
            codecs=dict(recording_config.CODECS),
            hub=self.hub,
        )
        #: Bytes per second the last recording achieved, so the remaining-time
        #: estimate survives the recording ending.
        self.last_write_rate: float | None = None
        #: RealSense devices as last enumerated, or None before the first time.
        self.realsense_found: list[DeviceInfo] | None = None
        #: The hub failure the enumeration above already reflects, by its
        #: error_at, so that each failure costs one enumeration and no more.
        self.realsense_found_after = 0.0
        self.realsense_lock = threading.Lock()

    def enumerate_realsense(self) -> list[DeviceInfo]:
        """Enumerate RealSense devices now, and keep the result."""
        with self.realsense_lock:
            self.realsense_found = list_devices()
            self.realsense_found_after = self.hub.error_at
            return self.realsense_found

    def known_realsense(self) -> list[DeviceInfo]:
        """The kept enumeration, redone only when it may have gone stale."""
        with self.realsense_lock:
            found = self.realsense_found
            stale = found is None or self.hub.error_at > self.realsense_found_after
        return self.enumerate_realsense() if stale else found

    @property
    def streams(self) -> StreamConfig:
        """What the camera is asked for, read through the recorder."""
        return self.recorder.streams

    @property
    def codecs(self) -> dict[str, str]:
        """How each stream's archive is encoded, read through the recorder."""
        return self.recorder.codecs or {}

    def _open_camera(self) -> LiveSource:
        """Open the camera. Called by the hub, and again after a failure."""
        return LiveSource(self.streams, serial=recording_config.SERIAL)

    def close(self) -> None:
        """Stop everything, in the order that leaves the devices usable."""
        if self.recorder.recording:
            logger.warning("shutting down with a recording running; stopping it")
            self.recorder.stop()
        self.recorder.close()
        self.hub.shutdown()


state = State()
