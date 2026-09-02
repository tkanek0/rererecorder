"""Writing the camera to an archive, and saying what it cost.

The recording is the primary consumer of the camera here, not a side effect of a
preview - which is the opposite of realsense-playground, where a recorder reads
whatever the hub last published. That matters: a hub hands out the newest frame,
so a recorder reading from one can silently miss frames when it is late. Reading
``source.frames()`` directly cannot skip anything, because the iterator yields
every set the SDK delivers.

What can still be lost is a frame the encoder queue has no room for, which means
the disk or the CPU could not keep up. That is counted and reported, never
hidden: a recording with holes is usable, and one that claims to have none is
not.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from video import ArchiveWriter, FrameSource, StreamConfig

logger = logging.getLogger(__name__)


@dataclass
class VideoStats:
    """What the video writer has done so far.

    Attributes:
        frames: Frame sets written.
        dropped: Sets the encoder queue could not accept. Non-zero means the
            disk or the CPU fell behind, and the recording has holes.
        skipped_warmup: Sets discarded before the first one was delivered,
            while the SDK's syncer settled. Every recording has a few; they are
            not a loss and are reported apart from the rest for that reason.
        skipped_duplicate: Sets the source discarded because every frame in
            them had already been delivered.
        skipped_unpaired: Sets discarded because their streams disagreed about
            when they were taken. Not a loss - these were never one instant -
            but the count says how often the camera is re-pairing frames, which
            is worth seeing next to the frame rate.
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

        Computed as intervals over span rather than as the mean of ``1 / dt``,
        which jitter biases high - a trap documented in realsense-playground
        after it made a struggling recorder look healthy.
        """
        span = self.span_s
        if span is None or span <= 0 or self.frames < 2:
            return None
        return (self.frames - 1) / span


class VideoWriter:
    """An open archive, fed from a frame source by a thread of its own."""

    def __init__(
        self,
        source: FrameSource,
        path: str,
        *,
        config: StreamConfig,
        codecs: dict[str, str] | None = None,
    ) -> None:
        """Bind a writer to its source and its output file.

        Args:
            source: Where frames come from. Opened and closed by this writer,
                because a RealSense device admits one owner and this is it.
            path: Archive to write.
            config: Stream configuration to record alongside the frames.
            codecs: Overrides for the archive's default codecs.
        """
        self._source = source
        self._path = path
        self._config = config
        self._codecs = codecs

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = VideoStats()
        self._writer: ArchiveWriter | None = None
        self._ready = threading.Event()

    # -- control -----------------------------------------------------------

    def start(self, timeout: float = 15.0) -> None:
        """Open the camera, start the archive and begin writing.

        Args:
            timeout: Seconds to wait for the camera to deliver its first frame.
                Generous: opening a RealSense pipeline costs about a second, and
                auto-exposure takes longer than that to settle.

        Raises:
            RuntimeError: If this writer is already running, or the camera did
                not start. Raised rather than reported, because the caller is
                deciding whether a session can begin at all - and a session
                that silently records nothing from the camera is worse than one
                that refuses to start.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("this video writer is already running")
            self._stats = VideoStats()
            self._stop.clear()
            self._ready.clear()

        self._source.open()
        self._thread = threading.Thread(
            target=self._run, name="video-writer", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout):
            self.stop()
            raise RuntimeError(
                f"the camera produced no frames within {timeout:.0f}s: "
                f"{self._stats.error or 'no error reported'}"
            )

    def stop(self, timeout: float = 15.0) -> VideoStats:
        """Stop writing, finish the archive and release the camera.

        Args:
            timeout: Seconds to wait for the writer thread and the encoders.

        Returns:
            The final statistics.
        """
        self._stop.set()
        # Wakes the frames() iterator, which otherwise blocks for up to a
        # second per call waiting on the SDK.
        try:
            self._source.close()
        except Exception:  # noqa: BLE001 - closing must not mask the recording
            logger.warning("could not close the frame source", exc_info=True)
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.stats

    @property
    def running(self) -> bool:
        """Whether the writer thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

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
            snapshot.bytes_written = written.bytes_written
        snapshot.skipped_warmup = getattr(self._source, "skipped_warmup", 0)
        snapshot.skipped_duplicate = getattr(self._source, "skipped_duplicate", 0)
        snapshot.skipped_unpaired = getattr(self._source, "skipped_unpaired", 0)
        snapshot.timestamp_domain = getattr(
            self._source, "timestamp_domain", snapshot.timestamp_domain
        )
        return snapshot

    # -- writer thread -----------------------------------------------------

    def _run(self) -> None:
        """Open the archive from the first frame, then feed it everything."""
        try:
            options = self._read_options()
            self._writer = ArchiveWriter(
                self._path,
                calibration=self._source.calibration,
                config=self._config,
                device=getattr(self._source, "device", None),
                options=options,
                codecs=self._codecs,
            )
            try:
                for frames in self._source.frames():
                    if self._stop.is_set():
                        break
                    # Not waiting for room: a full queue means the disk cannot
                    # keep up, and blocking here would stall the camera for
                    # every other consumer. The drop is counted instead.
                    self._writer.append(frames)
                    with self._lock:
                        if self._stats.first_monotonic is None:
                            self._stats.first_monotonic = frames.capture_monotonic
                            self._stats.timestamp_domain = frames.timestamp_domain
                        self._stats.last_monotonic = frames.capture_monotonic
                    self._ready.set()
            finally:
                self._writer.drain(timeout=30.0)
                self._writer.close()
        except Exception as error:  # noqa: BLE001 - reported through stats
            logger.exception("video recording failed")
            with self._lock:
                self._stats.error = str(error)
        finally:
            # Unblocks a caller waiting on the first frame that never came.
            self._ready.set()
            # self.stats, not self._stats: the frame count lives in the
            # archive writer, and the local copy is never updated.
            logger.info(
                "video recording stopped: %s, %d frames", self._path, self.stats.frames
            )

    def _read_options(self) -> dict[str, float]:
        """Read the camera's sensor options, if the source can report them.

        Returns:
            Every option and its value, or an empty mapping. A failure is
            logged and swallowed: the options make a recording interpretable,
            but they are not the measurements.
        """
        reader = getattr(self._source, "options", None)
        if reader is None:
            return {}
        try:
            return reader()
        except Exception:  # noqa: BLE001 - a nicety, not the data
            logger.warning("could not read the sensor options", exc_info=True)
            return {}
