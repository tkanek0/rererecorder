"""Recording both devices into one session, and writing down how they relate.

The two devices are independent: separate USB endpoints, separate clocks,
separate failure modes. Nothing here tries to start them at the same instant,
because that would be both impossible and pointless - a RealSense pipeline takes
about a second to open, and neither device's data is timestamped by when
recording began. What matters is that every frame and every sample carries a
time on one axis, and that the axis is written down. Then the overlap can be
found afterwards, exactly, from the files.

So this class does three things:

* runs a writer per device, each on its own thread, each surviving the other's
  failure - a session with one track and an explanation beats no session,
* samples both host clocks throughout, so the monotonic axis can be named in
  wall-clock terms and an NTP step is visible rather than smeared,
* rewrites the manifest while recording, so a session interrupted by a crash
  still describes itself.

What it deliberately does not do is claim the two tracks are aligned. The
residual offset between a microphone and a shutter is not derivable from either
device, and until ``rrr/tools/calibrate.py`` measures it the manifest's calibration
stays null.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from rrr.audio import AudioTap, DoaTap
from rrr.timeline import (
    EVENTS_NAME,
    AudioTimeline,
    AudioTrack,
    ClockTrack,
    Event,
    EventWriter,
    SessionManifest,
    SessionPaths,
    VideoTrack,
    read_clocks,
    write_manifest,
)
from rrr.video import FrameHub, FrameSource, LiveSource, StreamConfig

from .audio_writer import AudioWriter
from .video_writer import VideoWriter

logger = logging.getLogger(__name__)

#: Seconds between clock samples, and between manifest rewrites.
#:
#: One second is 3.6k clock samples an hour - nothing beside the frames - and
#: fine enough to catch an NTP step. It is also how stale a crashed session's
#: manifest can be, which is the more demanding of the two requirements.
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
        tap: AudioTap | None = None,
        doa: DoaTap | None = None,
        record_video: bool = True,
        record_audio: bool = True,
        record_doa: bool = True,
        codecs: dict[str, str] | None = None,
        hub: FrameHub | None = None,
        source_factory: Any = None,
    ) -> None:
        """Prepare a recorder. Nothing is opened until :meth:`start`.

        Args:
            root: Where session directories are created.
            streams: What to ask the camera for.
            serial: Camera serial to open, or empty for whichever is found.
            tap: Audio tap to read, or None to make one.
            doa: Direction tap to read, or None to make one.
            record_video: Whether to record the camera at all.
            record_audio: Whether to record the array at all.
            record_doa: Whether to record the direction beside the audio.
            codecs: Overrides for the archive's default codecs.
            hub: Frame hub to record from, or None to make one. The server
                passes its own so that the preview and the recording share one
                pipeline - opening the camera again for a recording would take
                a second, drop the preview and leave auto-exposure settling in
                the middle of what was being recorded.
            source_factory: Callable returning a :class:`FrameSource`, for
                tests and for a future replay source. Ignored when a hub is
                given.
        """
        self._root = root
        self._streams = streams or StreamConfig()
        self._serial = serial
        self._record_video = record_video
        self._record_audio = record_audio
        self._record_doa = record_doa
        self._codecs = codecs
        self._source_factory = source_factory or self._open_camera
        self._owns_hub = hub is None
        self._hub = hub or FrameHub(self._source_factory)

        # Whether this recorder made the taps, and so has to close them. A tap
        # passed in belongs to whoever passed it - the server holds its own
        # across many sessions.
        self._owns_taps = tap is None and doa is None
        self._tap = tap or (AudioTap() if record_audio else None)
        self._doa = doa or (DoaTap() if record_audio and record_doa else None)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._paths: SessionPaths | None = None
        self._manifest: SessionManifest | None = None
        self._clock_track = ClockTrack(interval_s=MONITOR_INTERVAL_S)
        self._video: VideoWriter | None = None
        self._audio: AudioWriter | None = None
        self._events: EventWriter | None = None
        # Counted here rather than read off the writer, so that the number
        # survives the writer being closed at the end of a session.
        self._marks = 0

    def _open_camera(self) -> FrameSource:
        """Open the camera as the default frame source."""
        return LiveSource(self._streams, serial=self._serial)

    # -- control -----------------------------------------------------------

    def start(self, session_id: str | None = None) -> SessionPaths:
        """Create a session and begin recording into it.

        Args:
            session_id: Directory name, or None to build one from the local
                time. Colons are not usable in a session id, so the timestamp
                uses dashes.

        Returns:
            Where the session is being written.

        Raises:
            RecorderBusy: If a recording is already running.
            SessionError: If the id is unusable or the session exists.
            RuntimeError: If neither device could be recorded. Both failing is
                not a recording, and reporting it as one would be a lie; the
                manifest is written first so the errors survive.
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
            # Opened for every session rather than on the first mark: a button
            # press is worth nothing if it has to wait for a file to be
            # created, and an empty sidecar costs a directory entry.
            self._events = EventWriter(paths.events)
            self._marks = 0
            self._manifest.events_file = EVENTS_NAME

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
            doa=self._doa if self._record_doa else None,
            doa_path=self._paths.doa if self._record_doa else None,
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

        # Audio first: it is cheap to close, and stopping the camera can take
        # seconds while the encoder queue drains. Closing the camera first would
        # keep recording audio through all of it, for no reason.
        if self._audio is not None:
            self._audio.stop(timeout=timeout)
        # Kept, not cleared: _collect_tracks below reads the final statistics
        # off it, and start() replaces it anyway.
        if self._video is not None:
            self._video.stop(timeout=timeout)
        # Detached under the lock before it is closed: a mark arriving from the
        # page while the session is being stopped would otherwise reach a file
        # that has just been closed.
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
            RuntimeError: If nothing is recording. A mark with no session has
                nowhere to go, and inventing one would put it in the next
                recording instead.

        The stamp is taken inside the lock, as close to the call as possible.
        It is still a person's reaction time late - see
        :mod:`rrr.timeline.events` - so this is for saying what a stretch of a
        recording was, not for aligning against a frame.
        """
        with self._lock:
            if self._events is None:
                raise RuntimeError("nothing is recording, so there is nothing to mark")
            event = Event.now(label, data)
            self._events.append(event)
            self._marks += 1
        logger.info("mark: %s", label)
        return event

    def close(self) -> None:
        """Release the devices for good, if this recorder opened them.

        The array needs this most. Its taps run on daemon threads, so a process
        that exits while a capture stream is still open never closes it - and the array is then in a state where the next
        ``InputStream`` open fails, silently producing a session with an empty
        WAV and no error to explain it. Observed exactly that, twice in a row,
        before this existed.

        Releasing is not enough: a release only starts an idle countdown, and
        the process is usually gone before it expires.
        """
        if self._owns_hub:
            self._hub.stop()
        if not self._owns_taps:
            return
        if self._tap is not None:
            self._tap.shutdown()
        if self._doa is not None:
            self._doa.shutdown()

    def __enter__(self) -> SessionRecorder:
        """Return the recorder."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop any recording and release the devices."""
        if self.recording:
            self.stop()
        self.close()

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
            RecorderBusy: If a recording is running. Moving the directory
                mid-session would leave half a session on one disk and half on
                another, and the manifest would describe neither.

        Settable because the choice of disk is a per-session decision here: a
        recording costs 195 GB an hour, so which volume it lands on is not
        something to fix at deployment time.
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
            RecorderBusy: If a recording is running. The SDK settles
                resolution and frame rate at pipeline start, so this takes
                effect only the next time the camera opens - restart the hub
                after setting this if one is shared with a preview.
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
                unchanged. See ``video.archive.DEFAULT_CODECS``.

        Raises:
            RecorderBusy: If a recording is running - an archive's codecs are
                fixed for its whole life, so this can only affect one that has
                not started yet.
        """
        if self.recording:
            raise RecorderBusy("cannot change codecs while recording")
        self._codecs = value

    @property
    def recording(self) -> bool:
        """Whether a session is currently being written."""
        monitor = self._monitor
        return monitor is not None and monitor.is_alive()

    @property
    def paths(self) -> SessionPaths | None:
        """Where the current or most recent session lives."""
        return self._paths

    @property
    def manifest(self) -> SessionManifest | None:
        """The current or most recent manifest."""
        return self._manifest

    @property
    def _session_id(self) -> str | None:
        return self._paths.session_id if self._paths else None

    def state(self) -> dict[str, Any]:
        """Describe the recording for an API or a CLI.

        Returns:
            What is being recorded, for how long, and what has gone wrong. The
            drop and fill counts are included deliberately: they are how a
            caller learns that a recording has holes, and a UI that does not
            show them lets a bad session look fine.
        """
        video = self._video.stats if self._video is not None else None
        audio = self._audio.stats if self._audio is not None else None
        return {
            "recording": self.recording,
            "session_id": self._session_id,
            "directory": self._paths.directory if self._paths else None,
            "seconds": self._elapsed(),
            "size_bytes": self._paths.size_bytes() if self._paths else 0,
            "video": (
                None
                if video is None
                else {
                    "frames": video.frames,
                    "dropped": video.dropped,
                    "skipped": video.skipped,
                    "skipped_duplicate": video.skipped_duplicate,
                    "skipped_warmup": video.skipped_warmup,
                    "motion": video.motion,
                    "motion_overrun": video.motion_overrun,
                    "fps": round(video.fps, 2) if video.fps else None,
                    "timestamp_domain": video.timestamp_domain,
                    "error": video.error,
                }
            ),
            "audio": (
                None
                if audio is None
                else {
                    "seconds": round(audio.seconds, 2),
                    "filled": audio.filled,
                    "gaps": audio.gaps,
                    "dropped_by_reader": audio.dropped_by_reader,
                    "overruns": audio.overruns,
                    "clock_points": audio.clock_points,
                    "error": audio.error,
                }
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
        """Fold the writers' statistics into the manifest. Caller holds the lock.

        Including their errors. A writer that fails after the session started
        reports through its stats rather than raising - the other device keeps
        recording - and without this the failure would never reach the file that
        is supposed to describe the session.
        """
        if self._manifest is None:
            return
        for label, writer in (("video", self._video), ("audio", self._audio)):
            error = writer.stats.error if writer is not None else None
            if error and f"{label}: {error}" not in self._manifest.errors:
                self._manifest.errors.append(f"{label}: {error}")
        if self._video is not None:
            stats = self._video.stats
            self._manifest.video = VideoTrack(
                frames=stats.frames,
                dropped=stats.dropped,
                motion=stats.motion,
                motion_overrun=stats.motion_overrun,
                skipped_warmup=stats.skipped_warmup,
                skipped_duplicate=stats.skipped_duplicate,
                first_monotonic=stats.first_monotonic,
                last_monotonic=stats.last_monotonic,
                timestamp_domain=stats.timestamp_domain,
                fps=stats.fps,
            )
        if self._audio is not None and self._tap is not None:
            stats = self._audio.stats
            self._manifest.audio = AudioTrack(
                rate=self._tap.rate,
                channels=self._tap.channels,
                samples=stats.samples,
                filled=stats.filled,
                overruns=stats.overruns,
                first_monotonic=stats.first_monotonic,
                timeline=self._timeline_report(),
            )
            self._manifest.doa_file = (
                self._manifest.doa_file
                if self._doa is None or not self._record_doa
                else "doa.jsonl"
            )

    def _timeline_report(self) -> dict[str, object] | None:
        """Read back the clock sidecar and summarise the audio's time axis.

        Returns:
            The report, or None while there is not enough to say anything.

        Read from the file rather than kept in memory on purpose: this is the
        one check that the sidecar a consumer will actually read says what the
        recording believes. A report built from the writer's own variables would
        agree with itself no matter what reached the disk.
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
