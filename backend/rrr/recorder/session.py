"""Recording both devices into one session, and writing down how they relate.

Runs a writer per device, each surviving the other's failure; samples both
host clocks throughout; and rewrites the manifest while recording so a crashed
session still describes itself. It never claims the tracks are aligned - see
docs/design.md "The one idea" and "What it refuses to claim".
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Any

from realsense_adapter import FrameSource, LiveSource, StreamConfig
from respeaker_adapter import CHANNELS, SAMPLE_RATE

from rrr.devices import AudioTap, DoaTap, FrameHub
from rrr.timeline import (
    AudioTimeline,
    AudioTrack,
    ClockTrack,
    Event,
    JsonlWriter,
    SessionManifest,
    SessionPaths,
    VideoTrack,
    write_manifest,
)

from . import config
from .audio_writer import AudioWriter
from .video_writer import VideoWriter

logger = logging.getLogger(__name__)

#: Seconds between clock samples, and between manifest rewrites - so also how
#: stale a crashed session's manifest can be.
MONITOR_INTERVAL_S = 1.0

#: How long to wait for the camera's first frame before giving up on it.
VIDEO_START_TIMEOUT_S = 15.0


class RecorderBusy(RuntimeError):
    """Raised when a recording is asked for while one is already running."""


class SessionRecorder:
    """Records the camera and the array into one session directory."""

    def __init__(
        self,
        root: str,
        *,
        streams: StreamConfig | None = None,
        serial: str = "",
        record_video: bool = True,
        record_audio: bool = True,
        record_doa: bool = True,
        codecs: dict[str, str] | None = None,
        hub: FrameHub | None = None,
    ) -> None:
        """Prepare a recorder. Nothing is opened until :meth:`start`.

        Args:
            root: Where session directories are created.
            streams: What to ask the camera for.
            serial: Camera serial to open, or empty for whichever is found.
            record_video: Whether to record the camera at all.
            record_audio: Whether to record the array at all.
            record_doa: Whether to record the direction beside the audio.
            codecs: Overrides for the archive's default codecs.
            hub: Frame hub to record from, or None to make one. The server
                passes its own so preview and recording share one pipeline
                (docs/design.md "The camera is shared").
        """
        self._root = root
        self._streams = streams or StreamConfig()
        self._serial = serial
        self._record_video = record_video
        self._record_audio = record_audio
        self._codecs = codecs
        self._owns_hub = hub is None
        self._hub = hub or FrameHub(self._open_camera, config.IDLE_SHUTDOWN_S)
        self._tap = _make_audio_tap() if record_audio else None
        self._doa = _make_doa_tap() if record_audio and record_doa else None

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._paths: SessionPaths | None = None
        self._manifest: SessionManifest | None = None
        self._clock_track = ClockTrack(interval_s=MONITOR_INTERVAL_S)
        self._video: VideoWriter | None = None
        self._audio: AudioWriter | None = None
        self._events: JsonlWriter | None = None
        # Counted here so the number survives the event writer being closed.
        self._marks = 0

    def _open_camera(self) -> FrameSource:
        """Open the camera as the default frame source."""
        return LiveSource(self._streams, serial=self._serial)

    # -- control -----------------------------------------------------------

    def start(self, session_id: str | None = None) -> SessionPaths:
        """Create a session and begin recording into it.

        Args:
            session_id: Directory name, or None to build one from the local
                time (with dashes, since colons are not allowed).

        Returns:
            Where the session is being written.

        Raises:
            RecorderBusy: If a recording is already running.
            SessionError: If the id is unusable or the session exists.
            RuntimeError: If neither device could be recorded. The manifest
                is written first so the errors survive.
        """
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                raise RecorderBusy(f"already recording {self._session_id}")

            chosen = session_id or time.strftime("%Y-%m-%d_%H-%M-%S")
            paths = SessionPaths.create(self._root, chosen)
            self._paths = paths
            self._clock_track = ClockTrack(interval_s=MONITOR_INTERVAL_S)
            self._stop.clear()

            started = self._clock_track.sample(force=True)
            self._manifest = SessionManifest(
                session_id=chosen,
                started_at=started,
                clock_samples=self._clock_track.samples,
            )
            # Opened up front so a mark never waits on creating the file.
            self._events = JsonlWriter(paths.events)
            self._marks = 0

        errors: list[str] = []
        self._video = self._start_video(errors)
        self._audio = self._start_audio(errors)

        with self._lock:
            self._manifest.errors = errors
        self._write()

        if self._video is None and self._audio is None:
            raise RuntimeError(
                "neither device could be recorded: " + "; ".join(errors)
            )

        self._monitor = threading.Thread(
            target=self._run_monitor, name="session-monitor", daemon=True
        )
        self._monitor.start()
        logger.info("recording session %s to %s", chosen, paths.directory)
        return paths

    def _start_video(self, errors: list[str]) -> VideoWriter | None:
        """Start the camera writer, reporting a failure rather than raising."""
        if not self._record_video or self._paths is None:
            return None
        writer = VideoWriter(
            self._hub,
            self._paths.video,
            config=self._streams,
            codecs=self._codecs,
        )
        try:
            writer.start(timeout=VIDEO_START_TIMEOUT_S)
        except Exception as error:  # noqa: BLE001 - the array can still record
            logger.warning("the camera could not be recorded: %s", error)
            errors.append(f"video: {error}")
            return None
        return writer

    def _start_audio(self, errors: list[str]) -> AudioWriter | None:
        """Start the array writer, reporting a failure rather than raising."""
        if not self._record_audio or self._paths is None or self._tap is None:
            return None
        writer = AudioWriter(
            self._tap,
            wav_path=self._paths.audio,
            clock_path=self._paths.audio_clock,
            doa=self._doa,
            doa_path=self._paths.doa if self._doa is not None else None,
        )
        try:
            writer.start()
        except Exception as error:  # noqa: BLE001 - the camera can still record
            logger.warning("the array could not be recorded: %s", error)
            errors.append(f"audio: {error}")
            return None
        return writer

    def stop(self, timeout: float = 20.0) -> SessionManifest:
        """Stop recording, close every file and finish the manifest.

        Args:
            timeout: Seconds to allow each writer to finish.

        Returns:
            The completed manifest, as written to disk.

        Raises:
            RuntimeError: If no session was started.
        """
        if self._paths is None or self._manifest is None:
            raise RuntimeError("no session to stop")

        self._stop.set()
        monitor = self._monitor
        if monitor is not None:
            monitor.join(timeout=MONITOR_INTERVAL_S * 3)

        # Audio first: the camera can take seconds to drain its encoder queue.
        if self._audio is not None:
            self._audio.stop(timeout=timeout)
        # Writers are kept, not cleared: _collect_tracks reads their final stats.
        if self._video is not None:
            self._video.stop(timeout=timeout)
        # Detached under the lock first, so a concurrent mark() cannot reach a
        # closed file.
        with self._lock:
            events, self._events = self._events, None
        if events is not None:
            events.close()

        with self._lock:
            self._clock_track.sample(force=True)
            self._manifest.stopped_at = self._clock_track.latest
            self._manifest.clock_samples = self._clock_track.samples
            self._collect_tracks()
        self._write()

        logger.info(
            "session %s finished: %s",
            self._manifest.session_id,
            _summary(self._manifest),
        )
        self._monitor = None
        return self._manifest

    def mark(self, label: str, data: dict[str, Any] | None = None) -> Event:
        """Record a mark against the running session.

        Args:
            label: What the mark means, e.g. the condition being recorded.
            data: Anything else worth keeping with it.

        Returns:
            The event as written, so a caller can show the time it landed on.

        Raises:
            RuntimeError: If nothing is recording.

        Accurate only to a person's reaction time; see docs/features.md "Marks".
        """
        with self._lock:
            if self._events is None:
                raise RuntimeError("nothing is recording, so there is nothing to mark")
            event = Event.now(label, data)
            self._events.append(event.as_dict())
            self._marks += 1
        logger.info("mark: %s", label)
        return event

    def close(self) -> None:
        """Release the devices for good, if this recorder opened them.

        Must be called before exit: the taps run on daemon threads, and a
        capture stream left open makes the next ``InputStream`` open fail
        silently (an empty WAV). A tap release only starts an idle countdown
        the process usually outlives, so this shuts the taps down instead.
        """
        if self._owns_hub:
            self._hub.shutdown()
        if self._tap is not None:
            self._tap.shutdown()
        if self._doa is not None:
            self._doa.shutdown()

    # -- state -------------------------------------------------------------

    @property
    def root(self) -> str:
        """Where session directories are created."""
        return self._root

    @root.setter
    def root(self, value: str) -> None:
        """Move where future sessions are created.

        Args:
            value: The new directory. Applies to the next session; the one
                being written stays where it is.

        Raises:
            RecorderBusy: If a recording is running.
        """
        if self.recording:
            raise RecorderBusy("cannot move the directory while recording")
        self._root = value

    @property
    def streams(self) -> StreamConfig:
        """What the camera is asked for."""
        return self._streams

    @streams.setter
    def streams(self, value: StreamConfig) -> None:
        """Change what the next recording asks the camera for.

        Args:
            value: The new stream configuration.

        Raises:
            RecorderBusy: If a recording is running.

        Takes effect only when the camera next opens; restart a shared hub.
        """
        if self.recording:
            raise RecorderBusy("cannot change streams while recording")
        self._streams = value

    @property
    def codecs(self) -> dict[str, str] | None:
        """Overrides for the archive's default codecs, or None for all of them."""
        return self._codecs

    @codecs.setter
    def codecs(self, value: dict[str, str] | None) -> None:
        """Change how the next recording's archive encodes each stream.

        Args:
            value: Overrides for the default codecs, or None to use them
                unchanged. See ``video.archive.COMPRESSED_CODECS``.

        Raises:
            RecorderBusy: If a recording is running.
        """
        if self.recording:
            raise RecorderBusy("cannot change codecs while recording")
        self._codecs = value

    @property
    def tap(self) -> AudioTap | None:
        """The audio tap this recorder reads, or None if audio is not recorded."""
        return self._tap

    @property
    def doa(self) -> DoaTap | None:
        """The direction tap this recorder reads, or None if it reads none."""
        return self._doa

    @property
    def recording(self) -> bool:
        """Whether a session is currently being written."""
        monitor = self._monitor
        return monitor is not None and monitor.is_alive()

    @property
    def _session_id(self) -> str | None:
        return self._paths.session_id if self._paths else None

    def state(self) -> dict[str, Any]:
        """Describe the recording for an API or a CLI.

        Returns:
            What is being recorded, for how long, and what has gone wrong,
            including the drop and fill counts that reveal holes.
        """
        video = self._video.stats if self._video is not None else None
        audio = self._audio.stats if self._audio is not None else None
        return {
            "recording": self.recording,
            "session_id": self._session_id,
            "seconds": self._elapsed(),
            "size_bytes": self._paths.size_bytes() if self._paths else 0,
            "video": (
                None if video is None else {**asdict(video), "fps": video.fps}
            ),
            "audio": (
                None if audio is None else {**asdict(audio), "seconds": audio.seconds}
            ),
            "marks": self._marks,
            "errors": list(self._manifest.errors) if self._manifest else [],
        }

    def _elapsed(self) -> float:
        """Seconds since the session started."""
        if self._manifest is None or self._manifest.started_at is None:
            return 0.0
        stopped = self._manifest.stopped_at
        end = stopped.monotonic if stopped else time.monotonic()
        return max(0.0, end - self._manifest.started_at.monotonic)

    # -- monitor thread ----------------------------------------------------

    def _run_monitor(self) -> None:
        """Sample the clocks and rewrite the manifest until asked to stop."""
        while not self._stop.wait(MONITOR_INTERVAL_S):
            with self._lock:
                self._clock_track.sample()
                if self._manifest is not None:
                    self._manifest.clock_samples = self._clock_track.samples
                    self._collect_tracks()
            self._write()

    def _collect_tracks(self) -> None:
        """Fold the writers' statistics and errors into the manifest.

        Caller holds the lock. A writer failing mid-session reports only
        through its stats, so this is how that error reaches the manifest.
        """
        if self._manifest is None:
            return
        reported = (
            ("video", self._video.stats.error if self._video else None),
            ("audio", self._audio.stats.error if self._audio else None),
            ("doa", self._doa.error if self._doa and self._audio else None),
        )
        for label, error in reported:
            if error and f"{label}: {error}" not in self._manifest.errors:
                self._manifest.errors.append(f"{label}: {error}")
        if self._video is not None:
            stats = self._video.stats
            self._manifest.video = VideoTrack.from_dict(
                {**asdict(stats), "fps": stats.fps}
            )
        if self._audio is not None and self._tap is not None:
            self._manifest.audio = AudioTrack.from_dict(
                {
                    **asdict(self._audio.stats),
                    "channels": self._tap.channels,
                    "timeline": self._timeline_report(),
                }
            )
            self._manifest.doa = self._doa is not None

    def _timeline_report(self) -> dict[str, object] | None:
        """Read back the clock sidecar and summarise the audio's time axis.

        Returns:
            The report, or None while there is not enough to say anything.

        Read from the file on purpose, so it checks what actually reached disk.
        """
        if self._paths is None or self._tap is None:
            return None
        try:
            timeline = AudioTimeline.read(self._paths.audio_clock, self._tap.rate)
        except (OSError, ValueError):
            return None
        return timeline.report().as_dict()

    def _write(self) -> None:
        """Write the manifest, if there is one."""
        if self._paths is not None and self._manifest is not None:
            write_manifest(self._paths, self._manifest)


def _summary(manifest: SessionManifest) -> str:
    """One line describing what a finished session holds."""
    parts = []
    if manifest.video is not None:
        parts.append(
            f"{manifest.video.frames} frames"
            + (f" at {manifest.video.fps:.1f} fps" if manifest.video.fps else "")
            + (f", {manifest.video.dropped} dropped" if manifest.video.dropped else "")
        )
    if manifest.audio is not None:
        parts.append(
            f"{manifest.audio.seconds:.1f} s audio"
            + (f", {manifest.audio.filled} samples filled" if manifest.audio.filled else "")
        )
    if manifest.duration_s is not None:
        parts.append(f"over {manifest.duration_s:.1f} s")
    return "; ".join(parts) or "nothing recorded"


def _make_audio_tap() -> AudioTap:
    """Open-on-demand audio tap with this application's configured settings."""
    return AudioTap(
        config.IDLE_SHUTDOWN_S,
        device=config.AUDIO_DEVICE,
        rate=SAMPLE_RATE,
        channels=CHANNELS,
        block_size=config.AUDIO_BLOCK_SIZE,
        window_s=config.AUDIO_WINDOW_S,
    )


def _make_doa_tap() -> DoaTap:
    """Open-on-demand direction tap with this application's configured settings."""
    return DoaTap(config.IDLE_SHUTDOWN_S, poll_hz=config.DOA_POLL_HZ)
