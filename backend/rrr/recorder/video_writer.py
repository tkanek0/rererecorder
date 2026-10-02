"""Writing the camera to an archive, and saying what it cost.

The writer is a listener on the frame hub, so it runs on the hub's read loop
and must never block - see docs/design.md "The camera is shared".
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from realsense_adapter import FrameSet, StreamConfig

from rrr.video import ArchiveWriter, FrameHub

logger = logging.getLogger(__name__)


@dataclass
class VideoStats:
    """What the video writer has done so far.

    Attributes:
        frames: Frame sets written.
        dropped: Sets the encoder queue could not accept; non-zero means the
            recording has holes.
        motion: Inertial samples written.
        motion_overrun: Samples the source discarded because this writer did
            not drain them in time. Should be zero.
        skipped_warmup: Sets the source discarded while the SDK's syncer
            settled, before the first. Not a loss (docs/frame-loss.md).
        skipped_duplicate: Sets discarded mid-stream because every frame in
            them had already been delivered.
        bytes_written: Size of the archive at the last commit.
        first_monotonic: Capture time of the first set written, or None.
        last_monotonic: Capture time of the last set written.
        timestamp_domain: What the camera's timestamps mean. Anything but
            ``global_time`` cannot be placed against the audio.
        error: What went wrong, if anything.
    """

    frames: int = 0
    dropped: int = 0
    motion: int = 0
    motion_overrun: int = 0
    skipped_warmup: int = 0
    skipped_duplicate: int = 0
    bytes_written: int = 0
    first_monotonic: float | None = None
    last_monotonic: float | None = None
    timestamp_domain: str = "unknown"
    error: str | None = None

    @property
    def skipped(self) -> int:
        """Sets discarded mid-stream. Excludes startup."""
        return self.skipped_duplicate

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

        Intervals over span, not the mean of ``1 / dt``, which jitter biases high.
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
            hub: Where frames come from. Acquired for as long as the archive
                is open, independently of the preview's viewers.
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
            timeout: Seconds to wait for the camera's first frame.

        Raises:
            RuntimeError: If this writer is already running, or the camera did
                not produce a frame.

        The first frame is waited for because its calibration is what the
        archive stores; without it depth has no scale.
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
        except Exception:  # noqa: BLE001 - a nicety, not the data
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
            # Inertial samples since the last frame, or the tail is lost.
            self._drain_motion(writer)
            if not writer.drain(timeout=timeout):
                logger.warning("the encoder queue did not drain within %.0fs", timeout)
            writer.close()
            # Copy the counters out before dropping the writer: `stats` reads
            # them from it, and frames written since the last read would be lost.
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
        # Never waits for room: blocking would stall the hub and the preview.
        # A full queue is counted as a drop instead.
        writer.append(frames)
        self._drain_motion(writer)
        with self._lock:
            if self._stats.first_monotonic is None:
                self._stats.first_monotonic = frames.received_monotonic
                self._stats.timestamp_domain = frames.timestamp_domain
            self._stats.last_monotonic = frames.received_monotonic

    def _drain_motion(self, writer: ArchiveWriter) -> None:
        """Move buffered inertial samples into the archive.

        Args:
            writer: The open archive.

        This must be the only drainer: draining takes ownership of the samples,
        so the preview only peeks at the newest one.
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
            Every option and its value, or an empty mapping on failure.
        """
        source = self._hub.source
        reader = getattr(source, "options", None) if source is not None else None
        if reader is None:
            return {}
        try:
            return reader()
        except Exception:
            logger.warning("could not read the sensor options", exc_info=True)
            return {}
