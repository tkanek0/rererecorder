"""A recording format that keeps every measurement and a tenth of the disk.

The SDK's own rosbag2 (`.db3`) writes raw pixels: 61 MB/s with colour and depth
at 848x480/30, which is 220 GB an hour. Almost all of that is compressible
without touching a single value - depth especially, being smooth and a seventh
zeros - so this writes the same frames through lossless codecs instead, at
about 91 GB an hour.

Nothing is thrown away. Alongside the pixels go the calibration, the stream
configuration, every sensor option the device exposes, the inertial samples, and
the per-frame metadata the firmware reports - exposure, gain, laser power and the
sensor's own timestamps. The intent is that a session recorded here can answer
the same questions as one recorded by the SDK.

The container is SQLite, like rosbag2 and for the same reasons: one file, random
access by frame, and readable by anything that speaks SQL whether or not
librealsense is installed. See `docs/recording.md`.

The one thing given up is `realsense-viewer`, which reads rosbag2 and not this.
`tools/record.py --format db3` still writes that when it is what you want.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from timeline import ClockPair

from .config import StreamConfig
from .source import StreamError
from .types import (
    Calibration,
    DeviceInfo,
    Extrinsics,
    FrameSet,
    Intrinsics,
    Motion,
    join_yuyv,
    split_yuyv,
)

logger = logging.getLogger(__name__)

#: Bumped when the schema changes in a way a reader must know about.
#:
#: 2 adds the infrared pair, splits YUYV colour across three columns, and lets
#: depth be zlib rather than PNG. Unlike ``capture_monotonic`` - an added column
#: an older reader can simply ignore - these change what the existing columns
#: mean, so a v1 reader must not open a v2 file. It cannot: realsense-playground
#: refuses any version but its own.
#:
#: The reader here still opens v1, because there are real recordings in that
#: format and nothing about them became wrong.
FORMAT_VERSION = 2

#: The oldest format this reader accepts.
MIN_READABLE_VERSION = 1

#: Meta key listing what this file carries beyond the base schema, so a reader
#: can say what it is missing rather than silently doing without.
EXTENSIONS_KEY = "extensions"

#: Suffix these files carry.
#:
#: Not ``.db3``, which means rosbag2 and would invite someone to open this with
#: a tool that cannot read it - and no longer ``.rsdb`` either, which means
#: realsense-playground's format. A v2 file is not one of those, and giving it
#: their name would only produce a confusing error somewhere else.
SUFFIX = ".rrdb"

#: What realsense-playground writes. Readable here; never written here.
LEGACY_SUFFIX = ".rsdb"

#: PNG effort. Level 1 costs 13 ms for a depth frame and 19 ms for colour, and
#: level 6 buys 17% at three times the time - which a 30 fps recorder does not
#: have. Two encoder threads clear 60 fps at level 1.
PNG_LEVEL = 1

#: How each stream is encoded, and what the meta records.
#:
#: Measured on real 1280x720 / 1280x800 frames, with four encoder threads: a
#: whole set - depth, three colour planes and two infrared images - takes 18.3
#: ms, against the 33.3 ms a 30 fps recorder has. Every codec here was verified
#: lossless by decoding and comparing, not assumed to be.
DEFAULT_CODECS = {"depth": "zlib", "color": "png", "infrared": "png"}

#: Frames buffered between the camera and the encoder threads. Four seconds at
#: 30 fps: long enough to ride out a stalled disk, short enough that the memory
#: is bounded. Frames arriving when it is full are counted, not silently lost.
QUEUE_DEPTH = 120

#: Frames per transaction. Committing each one costs more than the encoding.
COMMIT_EVERY = 30

#: Blob columns in the order the INSERT lists them.
_BLOB_COLUMNS = ("depth", "color", "color_y", "color_u", "color_v", "ir1", "ir2")

#: Which blob columns each stream needs, for reading one at a time.
#:
#: Infrared is a pair even when only one side is wanted: ``FrameSet.infrared``
#: holds both or neither, and the second image costs 5 ms.
_STREAM_COLUMNS: dict[str, tuple[str, ...]] = {
    "depth": ("depth",),
    "color": ("color", "color_y", "color_u", "color_v"),
    "infrared": ("ir1", "ir2"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL          -- JSON
);
CREATE TABLE IF NOT EXISTS frames(
    idx               INTEGER PRIMARY KEY,
    timestamp_ms      REAL NOT NULL, -- epoch ms while the domain is global_time
    received_at       REAL NOT NULL, -- time.monotonic() when assembled
    capture_monotonic REAL,          -- timestamp_ms on the monotonic axis
    depth             BLOB,          -- zlib or PNG16; meta.codecs says which
    color             BLOB,          -- PNG, only when the colour format is rgb8
    color_y           BLOB,          -- PNG, luma, when the format is yuyv
    color_u           BLOB,          -- PNG, chroma at half width
    color_v           BLOB,          -- PNG, chroma at half width
    ir1               BLOB,          -- PNG, left infrared
    ir2               BLOB,          -- PNG, right infrared
    metadata          TEXT           -- JSON, per stream
);
CREATE TABLE IF NOT EXISTS motion(
    idx          INTEGER PRIMARY KEY,
    timestamp_ms REAL NOT NULL,
    ax REAL, ay REAL, az REAL,
    gx REAL, gy REAL, gz REAL
);
"""


def encode_depth_zlib(depth: np.ndarray) -> bytes:
    """Compress a raw depth image with zlib.

    Args:
        depth: ``(height, width)`` uint16.

    Returns:
        A zlib stream of the raw values, little-endian.

    Faster and smaller than PNG16 on this data - measured on 1280x720 depth
    from a D455: 12.2 ms and 580 KB against 24.1 ms and 646 KB. PNG's row
    predictors work on bytes, and a 16-bit depth image interleaves high and low
    bytes, so they have little to predict from.

    What it gives up is self-description: the blob is values and nothing else,
    so the shape has to come from the archive's calibration. Written
    little-endian explicitly rather than in native order, so a recording made on
    one machine reads on another.
    """
    return zlib.compress(depth.astype("<u2", copy=False).tobytes(), 1)


def decode_depth_zlib(blob: bytes, shape: tuple[int, int]) -> np.ndarray:
    """Decompress a zlib depth blob.

    Args:
        blob: What :func:`encode_depth_zlib` produced.
        shape: ``(height, width)``, from the recording's calibration.

    Returns:
        The uint16 array that was written.

    Raises:
        StreamError: If the blob does not hold exactly that many values.
    """
    values = np.frombuffer(zlib.decompress(blob), dtype="<u2")
    if values.size != shape[0] * shape[1]:
        raise StreamError(
            f"depth blob holds {values.size} values, not {shape[0] * shape[1]}"
        )
    return values.reshape(shape)


def encode_plane(plane: np.ndarray) -> bytes:
    """Encode one 8-bit plane - infrared, or a YUYV component - as a PNG.

    Args:
        plane: ``(height, width)`` uint8.

    Returns:
        A PNG. Measured 10.8 ms and 231 KB for a 1280x720 infrared frame, and
        beating zlib on size here because the predictors do work on 8-bit data.

    Raises:
        RuntimeError: If OpenCV refused to encode it.
    """
    ok, buffer = cv2.imencode(".png", plane, [cv2.IMWRITE_PNG_COMPRESSION, PNG_LEVEL])
    if not ok:
        raise RuntimeError("plane PNG encoding failed")
    return buffer.tobytes()


def decode_plane(blob: bytes) -> np.ndarray:
    """Decode an 8-bit plane."""
    image = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype != np.uint8 or image.ndim != 2:
        raise StreamError("plane blob did not decode to an 8-bit image")
    return image


def encode_depth(depth: np.ndarray) -> bytes:
    """Encode a raw depth image losslessly.

    Args:
        depth: ``(height, width)`` uint16.

    Returns:
        A 16-bit PNG. Self-describing, so the file stays readable with ordinary
        image tools rather than only with this module.

    Raises:
        RuntimeError: If OpenCV refused to encode it.
    """
    ok, buffer = cv2.imencode(".png", depth, [cv2.IMWRITE_PNG_COMPRESSION, PNG_LEVEL])
    if not ok:
        raise RuntimeError("depth PNG encoding failed")
    return buffer.tobytes()


def encode_color(color: np.ndarray) -> bytes:
    """Encode a colour image losslessly.

    Args:
        color: ``(height, width, 3)`` uint8 RGB.

    Returns:
        A PNG. Written BGR-first because that is what OpenCV encodes; the
        decoder puts it back.

    Raises:
        RuntimeError: If OpenCV refused to encode it.
    """
    ok, buffer = cv2.imencode(
        ".png",
        cv2.cvtColor(color, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_PNG_COMPRESSION, PNG_LEVEL],
    )
    if not ok:
        raise RuntimeError("colour PNG encoding failed")
    return buffer.tobytes()


def decode_depth(blob: bytes) -> np.ndarray:
    """Decode a depth blob back to the exact array that was written."""
    image = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype != np.uint16:
        raise StreamError("depth blob did not decode to a 16-bit image")
    return image


def decode_color(blob: bytes) -> np.ndarray:
    """Decode a colour blob back to RGB."""
    image = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise StreamError("colour blob did not decode")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


@dataclass
class WriterStats:
    """What the writer has done so far.

    Attributes:
        frames: Frames written.
        dropped: Frames the queue could not accept. Non-zero means the disk or
            the encoders could not keep up, and is reported rather than hidden.
        bytes_written: Size of the file on disk at the last commit.
    """

    frames: int = 0
    dropped: int = 0
    bytes_written: int = 0


class ArchiveWriter:
    """Writes frame sets to a `.rsdb` archive.

    Encoding runs on a small thread pool - OpenCV releases the GIL, so two
    workers clear 60 fps for a lossless colour and depth pair - and the SQLite
    writes happen on one thread of their own, batched into transactions. The
    caller's thread does no work beyond a queue put, which matters because the
    caller is whatever is reading the camera.
    """

    def __init__(
        self,
        path: str,
        *,
        calibration: Calibration,
        config: StreamConfig,
        device: DeviceInfo | None = None,
        options: dict[str, float] | None = None,
        codecs: dict[str, str] | None = None,
        workers: int = 4,
    ) -> None:
        """Open an archive for writing.

        Args:
            path: File to create. Overwritten if it exists.
            calibration: Calibration in force, stored once.
            config: Stream configuration, stored once.
            device: Identity of the camera, stored once.
            options: Sensor options at the start of the recording.
            codecs: Overrides for DEFAULT_CODECS. Only ``depth`` has a choice:
                ``"zlib"`` (faster and smaller) or ``"png16"`` (openable by any
                image tool).
            workers: Encoder threads. Four, because a full set is six images
                and measurement puts the knee there: 25.9 ms per set with two
                threads, 18.3 with four, no better with six.
        """
        self._path = path
        self._codecs = {**DEFAULT_CODECS, **(codecs or {})}
        if self._codecs["depth"] not in ("zlib", "png16"):
            raise ValueError(f"unknown depth codec {self._codecs['depth']!r}")
        self._queue: queue.Queue[FrameSet | None] = queue.Queue(QUEUE_DEPTH)
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="archive-encode")
        self._stats = WriterStats()
        self._lock = threading.Lock()
        self._closed = False

        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.executescript(SCHEMA)
        # Durability against a crash, without paying a flush per frame. The
        # sidecar files are folded back in on close, so an archive is one file
        # once it is finished.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._write_meta(
            {
                "format_version": FORMAT_VERSION,
                "created_at": time.time(),
                "calibration": calibration.as_dict(),
                "config": config.as_dict(),
                "device": device.as_dict() if device else None,
                "options": options or {},
                "codecs": self._codecs,
                "color_format": config.color_format,
                EXTENSIONS_KEY: ["capture_monotonic"],
                # Filled in from the first frame: neither is known until one
                # arrives, and both describe the whole recording.
                "timestamp_domain": None,
                "clock_anchor": None,
            }
        )

        self._thread = threading.Thread(
            target=self._run, name="archive-writer", daemon=True
        )
        self._thread.start()

    def __enter__(self) -> ArchiveWriter:
        """Return the open writer."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Flush and close."""
        self.close()

    @property
    def path(self) -> str:
        """Where the archive is being written."""
        return self._path

    @property
    def stats(self) -> WriterStats:
        """A snapshot of what has been written."""
        with self._lock:
            return WriterStats(
                frames=self._stats.frames,
                dropped=self._stats.dropped,
                bytes_written=self._stats.bytes_written,
            )

    def _write_meta(self, values: dict[str, Any]) -> None:
        """Store the one-off descriptions of this recording."""
        self._connection.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            [(key, json.dumps(value)) for key, value in values.items()],
        )
        self._connection.commit()

    def append(self, frames: FrameSet, timeout: float | None = None) -> bool:
        """Queue a frame set to be written.

        Args:
            frames: The frame set to store.
            timeout: How long to wait for room.

                None - the default - does not wait at all, and is what a live
                recording wants: a camera nobody can stall is worth more than a
                frame, and the drop is counted rather than hidden.

                A number waits that long, which is what an offline pass wants.
                Converting a file or replaying one runs far faster than the
                encoders, so without waiting most of it would be dropped. Pass
                a generous value, or ``float("inf")`` to insist.

        Returns:
            Whether it was accepted. False means the frame was dropped.
        """
        if self._closed:
            return False
        try:
            if timeout is None:
                self._queue.put_nowait(frames)
            else:
                self._queue.put(frames, timeout=None if timeout == float("inf") else timeout)
            return True
        except queue.Full:
            with self._lock:
                self._stats.dropped += 1
            return False

    def drain(self, timeout: float = 60.0) -> bool:
        """Wait until everything queued has been written.

        Args:
            timeout: Seconds to wait.

        Returns:
            Whether the queue emptied within the timeout.
        """
        deadline = time.monotonic() + timeout
        while not self._queue.empty():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self) -> None:
        """Finish writing and leave the archive as a single file."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=30.0)
        self._pool.shutdown(wait=True)
        try:
            self._connection.commit()
            # Fold the write-ahead log back in, so what is left is one file.
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.commit()
        finally:
            self._connection.close()
        logger.info(
            "archive closed: %s, %d frames, %d dropped",
            self._path,
            self._stats.frames,
            self._stats.dropped,
        )

    def _run(self) -> None:
        """Encode and insert, until told to stop."""
        pending = 0
        while True:
            frames = self._queue.get()
            if frames is None:
                break
            try:
                self._insert(frames)
            except Exception:  # noqa: BLE001 - one bad frame is not the session
                logger.exception("could not write frame %d", frames.index)
                continue
            pending += 1
            if pending >= COMMIT_EVERY:
                self._commit()
                pending = 0
        self._commit()

    def _insert(self, frames: FrameSet) -> None:
        """Encode one frame set and add it to the open transaction."""
        if self._stats.frames == 0:
            # What the timestamps mean, and one pair of host clocks to name the
            # monotonic axis in wall-clock terms. Written from the first frame
            # because the domain is not known until one has arrived.
            self._write_meta(
                {
                    "timestamp_domain": frames.timestamp_domain,
                    "clock_anchor": (
                        frames.clock.as_dict() if frames.clock is not None else None
                    ),
                }
            )
        # Every plane at once: the pool is what makes this keep up with 30 fps.
        futures = self._submit(frames)
        self._connection.execute(
            "INSERT OR REPLACE INTO frames"
            "(idx, timestamp_ms, received_at, capture_monotonic, depth, color,"
            " color_y, color_u, color_v, ir1, ir2, metadata) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frames.index,
                frames.timestamp_ms,
                frames.received_at,
                # Stored rather than recomputed on read: it depends on the clock
                # offset that was in force for this frame, and that offset is
                # gone once the recording ends.
                frames.capture_monotonic,
                *(
                    futures[name].result() if futures[name] else None
                    for name in _BLOB_COLUMNS
                ),
                json.dumps(frames.metadata) if frames.metadata else None,
            ),
        )
        if frames.motion is not None:
            accel = frames.motion.accel or (None, None, None)
            gyro = frames.motion.gyro or (None, None, None)
            self._connection.execute(
                "INSERT OR REPLACE INTO motion"
                "(idx, timestamp_ms, ax, ay, az, gx, gy, gz) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (frames.index, frames.timestamp_ms, *accel, *gyro),
            )
        with self._lock:
            self._stats.frames += 1

    def _submit(self, frames: FrameSet) -> dict[str, Any]:
        """Start encoding every image in a set, returning one future per column.

        Args:
            frames: The set to encode.

        Returns:
            Column name to future, with None where the set has nothing.

        The YUYV split happens on this thread rather than in the pool: it is a
        pair of strided copies, tens of microseconds, and submitting it would
        cost more in scheduling than it saves.
        """
        futures: dict[str, Any] = dict.fromkeys(_BLOB_COLUMNS)
        if frames.depth is not None:
            encoder = (
                encode_depth_zlib if self._codecs["depth"] == "zlib" else encode_depth
            )
            futures["depth"] = self._pool.submit(encoder, frames.depth)
        if frames.color is not None:
            if frames.color_format == "yuyv":
                planes = split_yuyv(frames.color)
                for column, plane in zip(("color_y", "color_u", "color_v"), planes):
                    futures[column] = self._pool.submit(encode_plane, plane)
            else:
                futures["color"] = self._pool.submit(encode_color, frames.color)
        if frames.infrared is not None:
            futures["ir1"] = self._pool.submit(encode_plane, frames.infrared[0])
            futures["ir2"] = self._pool.submit(encode_plane, frames.infrared[1])
        return futures

    def _commit(self) -> None:
        """Commit, and note how large the file has become."""
        self._connection.commit()
        try:
            page_size = self._connection.execute("PRAGMA page_size").fetchone()[0]
            page_count = self._connection.execute("PRAGMA page_count").fetchone()[0]
            with self._lock:
                self._stats.bytes_written = page_size * page_count
        except sqlite3.Error:
            pass


def _intrinsics(raw: dict[str, Any] | None) -> Intrinsics | None:
    """Rebuild intrinsics from their stored form."""
    if raw is None:
        return None
    return Intrinsics(
        width=raw["width"],
        height=raw["height"],
        fx=raw["fx"],
        fy=raw["fy"],
        ppx=raw["ppx"],
        ppy=raw["ppy"],
        model=raw["model"],
        coeffs=tuple(raw["coeffs"]),
    )


class ArchiveSource:
    """Replays a `.rsdb` archive as a ``FrameSource``.

    The point of the seam: analysis written against the live camera runs against
    a recording with no change, and - unlike the camera, which admits one process
    at a time - any number of readers can work on the same file at once.
    """

    def __init__(self, path: str, *, realtime: bool = False, loop: bool = False) -> None:
        """Open an archive for reading.

        Args:
            path: Archive to read.
            realtime: Pace playback at the rate the frames were recorded.
                False replays as fast as the reader can consume, which is what
                an analysis pass wants.
            loop: Start again from the beginning when the archive runs out.

        Raises:
            StreamError: If the file is not an archive this version can read.
        """
        self._path = path
        self._realtime = realtime
        self._loop = loop
        self._stop = False
        self._connection: sqlite3.Connection | None = None
        self._meta: dict[str, Any] = {}
        self._calibration: Calibration | None = None
        self._has_monotonic = False
        self._columns: set[str] = set()
        self._codecs: dict[str, str] = {}

    def __enter__(self) -> ArchiveSource:
        """Open the archive."""
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        """Close it."""
        self.close()

    def open(self) -> None:
        """Read the archive's descriptions and make it ready to iterate."""
        if self._connection is not None:
            return
        try:
            connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            rows = connection.execute("SELECT key, value FROM meta").fetchall()
        except sqlite3.Error as exc:
            raise StreamError(f"{self._path} is not a readable archive: {exc}") from exc

        self._meta = {key: json.loads(value) for key, value in rows}
        version = self._meta.get("format_version")
        if not isinstance(version, int) or not (
            MIN_READABLE_VERSION <= version <= FORMAT_VERSION
        ):
            raise StreamError(
                f"{self._path} is format version {version}; this reads "
                f"{MIN_READABLE_VERSION} to {FORMAT_VERSION}"
            )
        # v1 predates the codec choice and wrote PNG16 depth unconditionally.
        self._codecs = {
            "depth": "png16",
            "color": "png",
            **(self._meta.get("codecs") or {}),
        }
        raw = self._meta["calibration"]
        extrinsics = raw.get("depth_to_color")
        self._calibration = Calibration(
            color=_intrinsics(raw.get("color")),
            depth=_intrinsics(raw.get("depth")),
            depth_scale=raw["depth_scale"],
            depth_to_color=(
                Extrinsics(
                    rotation=tuple(extrinsics["rotation"]),
                    translation=tuple(extrinsics["translation"]),
                )
                if extrinsics
                else None
            ),
            aligned=raw["aligned"],
        )
        # Detected, not inferred from the version: a file written by
        # realsense-playground has no such column, and one written here does.
        self._columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(frames)").fetchall()
        }
        self._has_monotonic = "capture_monotonic" in self._columns
        self._connection = connection
        self._stop = False

    def close(self) -> None:
        """Stop iterating and release the file."""
        self._stop = True
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    # -- description -------------------------------------------------------

    @property
    def calibration(self) -> Calibration:
        """Calibration recorded with this archive.

        Raises:
            StreamError: If the archive has not been opened.
        """
        if self._calibration is None:
            raise StreamError("open the archive before reading its calibration")
        return self._calibration

    @property
    def device(self) -> DeviceInfo | None:
        """The camera this was recorded from, if it was recorded."""
        raw = self._meta.get("device")
        if not raw:
            return None
        return DeviceInfo(**raw)

    @property
    def options(self) -> dict[str, float]:
        """Sensor options as they were when the recording started."""
        return dict(self._meta.get("options") or {})

    @property
    def meta(self) -> dict[str, Any]:
        """Everything stored about the recording, verbatim."""
        return dict(self._meta)

    @property
    def has_monotonic(self) -> bool:
        """Whether frames carry a capture time on the monotonic axis.

        False for an archive written by realsense-playground, whose frames can
        still be read - the pixels and the calibration are identical - but
        cannot be placed against an audio recording.
        """
        return self._has_monotonic

    @property
    def timestamp_domain(self) -> str | None:
        """What this recording's frame timestamps mean, or None if unrecorded."""
        return self._meta.get("timestamp_domain")

    @property
    def clock_anchor(self) -> ClockPair | None:
        """One pair of host clocks from the start of the recording.

        What names the monotonic axis in wall-clock terms. None for an archive
        written without one.
        """
        raw = self._meta.get("clock_anchor")
        return ClockPair.from_dict(raw) if raw else None

    def __len__(self) -> int:
        """How many frames the archive holds."""
        if self._connection is None:
            return 0
        return int(self._connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0])

    def frame_at(self, index: int, *, only: str | None = None) -> FrameSet | None:
        """Return one frame set by its index, without reading the others.

        Args:
            index: The archive's own ``idx``, as :meth:`bounds` reports the
                range of.
            only: Decode just one stream - ``"depth"``, ``"color"`` or
                ``"infrared"`` - leaving the rest None. What playback wants: a
                player shows one image at a time, and decoding all five to
                produce one costs 30 ms against 11.

        Returns:
            The set, or None if there is no frame with that index.

        Raises:
            StreamError: If the archive is not open.
            ValueError: If ``only`` names no stream this format has.

        What playback needs. ``frames()`` is a forward iterator, so seeking
        through it would mean decoding everything up to the point of interest -
        seconds of work to answer a question about one frame. ``idx`` is the
        primary key, so this is a single row lookup.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading frames")
        if only is not None and only not in _STREAM_COLUMNS:
            raise ValueError(f"no stream called {only!r}")

        wanted = (
            set(_BLOB_COLUMNS) if only is None else set(_STREAM_COLUMNS[only])
        )
        monotonic = "f.capture_monotonic" if self._has_monotonic else "NULL"
        blobs = ", ".join(
            f"f.{name}" if name in self._columns and name in wanted else "NULL"
            for name in _BLOB_COLUMNS
        )
        row = self._connection.execute(
            "SELECT f.idx, f.timestamp_ms, f.received_at, f.metadata,"
            f"       {monotonic}, {blobs},"
            "       m.ax, m.ay, m.az, m.gx, m.gy, m.gz "
            "FROM frames f LEFT JOIN motion m ON m.idx = f.idx WHERE f.idx = ?",
            (index,),
        ).fetchone()
        return None if row is None else self._to_frame_set(row)

    def bounds(self) -> tuple[int, int, float, float] | None:
        """Describe the range of frames the archive holds.

        Returns:
            ``(first_index, last_index, first_monotonic, last_monotonic)``, or
            None if it is empty. Indices are not assumed contiguous: a set the
            source discarded leaves a gap, so a player has to know both the
            range and that it may have holes in it.

        Raises:
            StreamError: If the archive is not open.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading frames")
        monotonic = "capture_monotonic" if self._has_monotonic else "NULL"
        row = self._connection.execute(
            f"SELECT MIN(idx), MAX(idx), MIN({monotonic}), MAX({monotonic}) FROM frames"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        first, last, start, end = row
        return int(first), int(last), float(start or 0.0), float(end or 0.0)

    def indices(self) -> list[int]:
        """Every frame index the archive holds, in order.

        Returns:
            The indices. About 8 KB of JSON for a 30 second recording and 860 KB
            for an hour, which is why a player seeks by index and asks the
            server for the time of the frame it landed on rather than fetching
            this.

        Raises:
            StreamError: If the archive is not open.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading frames")
        return [
            int(row[0])
            for row in self._connection.execute("SELECT idx FROM frames ORDER BY idx")
        ]

    def _decode_depth(self, blob: bytes | None) -> np.ndarray | None:
        """Decode a depth blob according to what the recording says it is.

        Args:
            blob: The stored bytes, or None.

        Returns:
            The uint16 array, or None.

        Raises:
            StreamError: If the file claims a zlib depth but records no shape
                to give it. A zlib blob is values and nothing else, so without
                the calibration's depth size it cannot be read at all - and
                guessing would silently produce a differently-shaped image.
        """
        if blob is None:
            return None
        if self._codecs.get("depth") != "zlib":
            return decode_depth(blob)
        intrinsics = self.calibration.depth
        if intrinsics is None:
            raise StreamError(
                f"{self._path} stores zlib depth but no depth calibration, so "
                "the image shape is unknown"
            )
        return decode_depth_zlib(blob, (intrinsics.height, intrinsics.width))

    def _to_frame_set(self, row: tuple, *, realtime: bool = False) -> FrameSet:
        """Turn one selected row into a FrameSet.

        Args:
            row: The columns in the order both queries select them.
            realtime: Stamp ``received_at`` with now rather than with what was
                recorded, which is what a paced replay wants.

        Returns:
            The frame set, with every stream decoded.
        """
        idx, timestamp_ms, received_at, metadata = row[:4]
        capture_monotonic = row[4]
        depth_blob, color_blob, y_blob, u_blob, v_blob, ir1, ir2 = row[5:12]
        accel = row[12:15]
        gyro = row[15:18]
        motion = (
            Motion(
                accel=tuple(accel) if accel[0] is not None else None,
                gyro=tuple(gyro) if gyro[0] is not None else None,
            )
            if any(value is not None for value in row[12:18])
            else None
        )
        color, color_format = self._decode_color(color_blob, y_blob, u_blob, v_blob)
        return FrameSet(
            index=idx,
            timestamp_ms=timestamp_ms,
            received_at=time.monotonic() if realtime else received_at,
            color=color,
            color_format=color_format,
            depth=self._decode_depth(depth_blob),
            infrared=(
                (decode_plane(ir1), decode_plane(ir2))
                if ir1 is not None and ir2 is not None
                else None
            ),
            calibration=self.calibration,
            motion=motion,
            metadata=json.loads(metadata) if metadata else None,
            # Rebuilt so that FrameSet.capture_monotonic returns the value that
            # was stored rather than recomputing it from an offset that no
            # longer applies. The pair holds one instant expressed on both axes,
            # which is all the conversion needs.
            clock=(
                ClockPair(
                    monotonic=capture_monotonic, realtime=timestamp_ms / 1000.0
                )
                if capture_monotonic is not None
                else None
            ),
            timestamp_domain=self._meta.get("timestamp_domain") or "unknown",
        )

    @staticmethod
    def _decode_color(
        color: bytes | None,
        y: bytes | None,
        u: bytes | None,
        v: bytes | None,
    ) -> tuple[np.ndarray | None, str]:
        """Rebuild the colour image from whichever columns hold it.

        Args:
            color: Single PNG, written when the format was rgb8.
            y: Luma plane, written when the format was yuyv.
            u: First chroma plane.
            v: Second chroma plane.

        Returns:
            ``(image, format)``. The format travels with the array because
            nothing about a uint16 array says it holds YUYV.
        """
        if y is not None and u is not None and v is not None:
            return join_yuyv(decode_plane(y), decode_plane(u), decode_plane(v)), "yuyv"
        if color is not None:
            return decode_color(color), "rgb8"
        return None, "rgb8"

    # -- frames ------------------------------------------------------------

    def frames(self) -> Iterator[FrameSet]:
        """Yield the recorded frame sets in order.

        Yields:
            One FrameSet per recorded frame, decoded back to the arrays that
            were written.

        Raises:
            StreamError: If the archive is not open.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading frames")

        while not self._stop:
            previous: float | None = None
            monotonic_column = (
                "f.capture_monotonic" if self._has_monotonic else "NULL"
            )
            # A v1 file has neither the colour planes nor the infrared columns;
            # selecting NULL in their place keeps one code path for both.
            blobs = ", ".join(
                f"f.{name}" if name in self._columns else "NULL"
                for name in _BLOB_COLUMNS
            )
            rows = self._connection.execute(
                "SELECT f.idx, f.timestamp_ms, f.received_at, f.metadata,"
                f"       {monotonic_column}, {blobs},"
                "       m.ax, m.ay, m.az, m.gx, m.gy, m.gz "
                "FROM frames f LEFT JOIN motion m ON m.idx = f.idx ORDER BY f.idx"
            )
            empty = True
            for row in rows:
                if self._stop:
                    return
                empty = False
                idx, timestamp_ms, received_at, metadata = row[:4]
                if self._realtime and previous is not None:
                    delay = (timestamp_ms - previous) / 1000.0
                    if 0 < delay < 5:
                        time.sleep(delay)
                previous = timestamp_ms

                yield self._to_frame_set(row, realtime=self._realtime)
            if empty or not self._loop:
                return
