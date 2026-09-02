"""Writing the camera to an archive, and saying what it cost.

The writer is a *listener* on the frame hub, not a poller. That distinction is
the whole design: ``FrameHub.latest`` returns the newest set, so anything
reading it that falls a frame behind loses one and cannot tell - fine for a
preview, useless for a recorder. A listener is called for every set, in order,
on the hub's own thread.

The price is that this code runs inside the hub's read loop, so it has to be
quick: ``ArchiveWriter.append`` is a bounded queue put, tens of microseconds,
and the encoding happens on the archive's own pool. If the queue is full the
frame is counted as dropped rather than waited for, because blocking here would
stall the camera for the preview as well.

Reading the source directly - which an earlier version did - would also work
and lose nothing, but then recording from the CLI and recording from the server
would be two different code paths, and only one of them would be exercised.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from video import ArchiveWriter, FrameHub, FrameSet, StreamConfig

logger = logging.getLogger(__name__)


@dataclass
class VideoStats:
    """What the video writer has done so far.

    Attributes:
        frames: Frame sets written.
        dropped: Sets the encoder queue could not accept. Non-zero means the
            disk or the CPU fell behind, and the recording has holes.
        motion: Inertial samples written. About 960 a second on a D455 - both
            streams at 480 Hz - against 30 video frames, which is why they are
            recorded separately rather than one per frame.
        motion_overrun: Samples the source discarded because this writer did
            not drain them in time. Should be zero: draining happens once per
            frame and the buffer holds eight seconds.
        skipped_warmup: Sets the source discarded before delivering its first,
            while the SDK's syncer settled. Measured on a D455: three, every
            time, within the same millisecond as ``pipeline.start``. Not a
            loss, and reported apart from the rest for that reason.
        skipped_duplicate: Sets discarded mid-stream because every frame in
            them had already been delivered.
        skipped_unpaired: Sets discarded mid-stream because their streams
            disagreed about when they were taken.
        bytes_written: Size of the archive at the last commit.
        first_monotonic: Capture time of the first set written, or None.
        last_monotonic: Capture time of the last set written.
        timestamp_domain: What the camera's timestamps mean. Anything other
            than ``global_time`` means the frames cannot be placed against the
            audio, and the session says so rather than pretending.
        error: What went wrong, if anything.
    """

    frames: int = 0
    dropped: int = 0
    motion: int = 0
    motion_overrun: int = 0
    skipped_warmup: int = 0
    skipped_duplicate: int = 0
    skipped_unpaired: int = 0
    bytes_written: int = 0
    first_monotonic: float | None = None
    last_monotonic: float | None = None
    timestamp_domain: str = "unknown"
    error: str | None = None

    @property
    def skipped(self) -> int:
        """Sets discarded mid-stream, for either reason. Excludes startup."""
        return self.skipped_duplicate + self.skipped_unpaired

    @property
    def span_s(self) -> float | None:
        """Seconds between the first and last set written."""
        if self.first_monotonic is None or self.last_monotonic is None:
            return None
        return self.last_monotonic - self.first_monotonic

    @property
    def fps(self) -> float | None:
        """Frames written per second of recording.

        Returns:
            The rate, or None with fewer than two frames.

        Intervals over span, not the mean of ``1 / dt``: jitter biases the
        latter high, a trap documented in realsense-playground after it made a
        struggling recorder look healthy.
        """
        span = self.span_s
        if span is None or span <= 0 or self.frames < 2:
            return None
        return (self.frames - 1) / span


class VideoWriter:
    """An open archive, fed by the frame hub."""

    def __init__(
        self,
        hub: FrameHub,
        path: str,
        *,
        config: StreamConfig,
        codecs: dict[str, str] | None = None,
    ) -> None:
        """Bind a writer to the hub and its output file.

        Args:
            hub: Where frames come from. Held open for as long as the archive
                is - a recording is a consumer of the camera in its own right,
                and putting it on the preview's reference count would stop a
                recording the moment the last browser tab closed.
            path: Archive to write.
            config: Stream configuration to record alongside the frames.
            codecs: Overrides for the archive's default codecs.
        """
        self._hub = hub
        self._path = path
        self._config = config
        self._codecs = codecs

        self._lock = threading.Lock()
        self._stats = VideoStats()
        self._writer: ArchiveWriter | None = None
        self._running = False

    # -- control -----------------------------------------------------------

    def start(self, timeout: float = 15.0) -> None:
        """Open the archive and begin writing every frame the hub delivers.

        Args:
            timeout: Seconds to wait for the camera's first frame. Generous:
                opening a RealSense pipeline costs about a second, and
                auto-exposure takes longer than that to settle.

        Raises:
            RuntimeError: If this writer is already running, or the camera did
                not produce a frame. Raised rather than reported, because the
                caller is deciding whether a session can begin at all - and a
                session that silently records no video is worse than one that
                refuses to start.

        The first frame is waited for rather than assumed: its calibration is
        what the archive stores, and an archive without one is not worth having
        because its depth values would have no scale.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("this video writer is already running")
            self._stats = VideoStats()

        self._hub.acquire()
        try:
            frames = self._hub.latest(timeout=timeout)
            if frames is None:
                raise RuntimeError(
                    self._hub.error or f"no frames within {timeout:.0f}s"
                )
            self._writer = ArchiveWriter(
                self._path,
                calibration=frames.calibration,
                config=self._config,
                device=self._hub.device,
                options=self._read_options(),
                codecs=self._codecs,
            )
        except Exception:
            self._hub.release()
            raise

        self._running = True
        self._hub.add_listener(self._on_frame)
        logger.info("recording video to %s", self._path)

    def stop(self, timeout: float = 30.0) -> VideoStats:
        """Stop writing, finish the archive and let go of the camera.

        Args:
            timeout: Seconds to wait for the encoder queue to drain.

        Returns:
            The final statistics.
        """
        if not self._running:
            return self.stats
        self._running = False
        # Detached first: nothing new arrives while the queue drains.
        self._hub.remove_listener(self._on_frame)
        writer = self._writer
        if writer is not None:
            # Whatever arrived since the last frame. Without this the tail of
            # every recording is missing up to a frame of inertial data.
            self._drain_motion(writer)
            if not writer.drain(timeout=timeout):
                logger.warning("the encoder queue did not drain within %.0fs", timeout)
            writer.close()
            # Copy the counters out before letting go of the writer: they live
            # in the archive, and `stats` reads them from there. Dropping the
            # reference first loses every frame written since the last time
            # anything asked - measured at 30 of 240 on a real recording.
            with self._lock:
                final = writer.stats
                self._stats.frames = final.frames
                self._stats.dropped = final.dropped
                self._stats.motion = final.motion
                self._stats.bytes_written = final.bytes_written
        self._writer = None
        self._hub.release()
        logger.info(
            "video recording stopped: %s, %d frames", self._path, self._stats.frames
        )
        return self.stats

    @property
    def running(self) -> bool:
        """Whether frames are currently being written."""
        return self._running

    @property
    def path(self) -> str:
        """Where the archive is being written."""
        return self._path

    @property
    def stats(self) -> VideoStats:
        """A snapshot of what has been written."""
        with self._lock:
            snapshot = VideoStats(**vars(self._stats))
        writer = self._writer
        if writer is not None:
            written = writer.stats
            snapshot.frames = written.frames
            snapshot.dropped = written.dropped
            snapshot.motion = written.motion
            snapshot.bytes_written = written.bytes_written
        source = self._hub.source
        if source is not None:
            snapshot.motion_overrun = getattr(source, "motion_overrun", 0)
            snapshot.skipped_warmup = getattr(source, "skipped_warmup", 0)
            snapshot.skipped_duplicate = getattr(source, "skipped_duplicate", 0)
            snapshot.skipped_unpaired = getattr(source, "skipped_unpaired", 0)
            snapshot.timestamp_domain = getattr(
                source, "timestamp_domain", snapshot.timestamp_domain
            )
        return snapshot

    # -- the hub's thread --------------------------------------------------

    def _on_frame(self, frames: FrameSet) -> None:
        """Hand one set to the archive. Runs on the hub's reader thread."""
        writer = self._writer
        if writer is None or not self._running:
            return
        # Not waiting for room: a full queue means the disk or the encoders
        # cannot keep up, and blocking here would stall the camera for the
        # preview too. The drop is counted instead.
        writer.append(frames)
        self._drain_motion(writer)
        with self._lock:
            if self._stats.first_monotonic is None:
                self._stats.first_monotonic = frames.capture_monotonic
                self._stats.timestamp_domain = frames.timestamp_domain
            self._stats.last_monotonic = frames.capture_monotonic

    def _drain_motion(self, writer: ArchiveWriter) -> None:
        """Move buffered inertial samples into the archive.

        Args:
            writer: The open archive.

        Drained from the frame listener rather than on a timer of its own: this
        runs 30 times a second, so about 32 samples accumulate against a buffer
        that holds 4096. One drainer only - the preview peeks at the newest
        sample instead, because whoever drains owns every sample it takes.
        """
        source = self._hub.source
        drain = getattr(source, "drain_motion", None) if source is not None else None
        if drain is None:
            return
        samples = drain()
        if samples:
            writer.append_motion(samples)

    def _read_options(self) -> dict[str, float]:
        """Read the camera's sensor options, if the source can report them.

        Returns:
            Every option and its value, or an empty mapping. A failure is
            logged and swallowed: the options make a recording interpretable,
            but they are not the measurements.
        """
        source = self._hub.source
        reader = getattr(source, "options", None) if source is not None else None
        if reader is None:
            return {}
        try:
            return reader()
        except Exception:  # noqa: BLE001 - a nicety, not the data
            logger.warning("could not read the sensor options", exc_info=True)
            return {}
