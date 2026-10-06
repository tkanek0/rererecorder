"""The ``.rrdb`` recording format: SQLite holding losslessly compressed frames.

Stores pixels, calibration, stream configuration, sensor options, inertial
samples and per-frame metadata. Lossless codecs instead of rosbag2's raw pixels:
220 GB/hour for colour and depth at 848x480/30 there, about 91 GB/hour here.
See docs/features.md "Recording" and docs/decisions.md 4, 5, 8, 22.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Self

import cv2
import numpy as np
from realsense_adapter import (
    Calibration,
    DeviceInfo,
    Extrinsics,
    FrameSet,
    Intrinsics,
    MotionCalibration,
    MotionIntrinsics,
    MotionSample,
    StreamConfig,
    StreamError,
)

from rrr.timeline import ClockPair, read_clocks

logger = logging.getLogger(__name__)

#: Bumped whenever the schema changes; a reader opens only its own version.
FORMAT_VERSION = 4

#: PNG effort. Level 1: 13 ms depth, 19 ms colour; level 6 saves 17% at three
#: times the time, which 30 fps cannot afford.
PNG_LEVEL = 1

#: Each stream's compressed codec; ``"raw"`` is the alternative for each
#: (docs/decisions.md 5, 22).
COMPRESSED_CODECS = {"depth": "zlib", "color": "png", "infrared": "png"}

#: Frames buffered before the encoders: four seconds at 30 fps. Overflow is
#: counted as dropped.
QUEUE_DEPTH = 120

#: Encoder threads, when the caller does not choose one. See
#: docs/decisions.md 19.
DEFAULT_WORKERS = min(8, max(4, os.cpu_count() or 4))

#: Frames per transaction. Committing each one costs more than the encoding.
COMMIT_EVERY = 30

#: Blob columns in the order the INSERT lists them.
_BLOB_COLUMNS = ("depth", "color", "color_y", "color_u", "color_v", "ir1", "ir2")

#: Which blob columns each stream needs, for reading one at a time. Infrared
#: is always a pair, as ``FrameSet.infrared`` holds both or neither.
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
    idx                INTEGER PRIMARY KEY,
    color_timestamp_ms REAL,          -- colour frame's own get_timestamp(), or NULL if disabled
    depth_timestamp_ms REAL,          -- depth's (shared by ir1/ir2 - one imager), or NULL if disabled
    received_monotonic REAL NOT NULL, -- time.monotonic() when the set was assembled
    depth              BLOB,          -- zlib or raw; meta.codecs says which
    color              BLOB,          -- PNG, only when the colour format is rgb8
    color_y            BLOB,          -- PNG, luma, when the format is yuyv
    color_u            BLOB,          -- PNG, chroma at half width
    color_v            BLOB,          -- PNG, chroma at half width
    ir1                BLOB,          -- PNG, left infrared
    ir2                BLOB,          -- PNG, right infrared
    metadata           TEXT           -- JSON, per stream
);
CREATE TABLE IF NOT EXISTS imu(
    id           INTEGER PRIMARY KEY,
    stream       TEXT NOT NULL,   -- 'accel' or 'gyro'
    timestamp_ms REAL NOT NULL,   -- epoch ms while the domain is global_time
    x REAL NOT NULL,
    y REAL NOT NULL,
    z REAL NOT NULL
);
-- Queried by time far more often than by id: "the samples around this frame".
CREATE INDEX IF NOT EXISTS imu_time ON imu(timestamp_ms);
"""


def split_yuyv(color: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Separate a YUYV image into its three planes.

    Args:
        color: ``(height, width)`` uint16, one element per pixel, as the SDK
            delivers YUYV.

    Returns:
        ``(y, u, v)``: luma at full width, and the two chroma planes at half
        width, all uint8. See docs/decisions.md 4.
    """
    raw = color.view(np.uint8).reshape(color.shape[0], color.shape[1], 2)
    return (
        raw[:, :, 0].copy(),
        raw[:, :, 1][:, 0::2].copy(),
        raw[:, :, 1][:, 1::2].copy(),
    )


def join_yuyv(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Reassemble a YUYV image from its planes.

    Args:
        y: Luma, ``(height, width)`` uint8.
        u: First chroma plane, ``(height, width // 2)`` uint8.
        v: Second chroma plane, same shape as ``u``.

    Returns:
        ``(height, width)`` uint16, byte-identical to what was split.
    """
    height, width = y.shape
    raw = np.empty((height, width, 2), np.uint8)
    raw[:, :, 0] = y
    raw[:, :, 1][:, 0::2] = u
    raw[:, :, 1][:, 1::2] = v
    return raw.reshape(height, width * 2).view(np.uint16)[:, :width]


def encode(image: np.ndarray, codec: str) -> bytes:
    """Encode one image losslessly.

    Args:
        image: Depth ``(h, w)`` uint16, an 8-bit plane ``(h, w)``, or RGB
            ``(h, w, 3)`` uint8.
        codec: ``"raw"`` (the values, little-endian), ``"zlib"`` (the same,
            compressed; for depth, where PNG's byte predictors lose,
            docs/decisions.md 5) or ``"png"`` (for 8-bit images).

    Returns:
        The blob. Only a PNG carries its own shape; the others take theirs from
        the archive's calibration.

    Raises:
        RuntimeError: If OpenCV refused to encode a PNG.
    """
    if codec == "png":
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)  # what OpenCV writes
        ok, buffer = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, PNG_LEVEL])
        if not ok:
            raise RuntimeError("PNG encoding failed")
        return buffer.tobytes()
    raw = image.astype(image.dtype.newbyteorder("<"), copy=False).tobytes()
    return zlib.compress(raw, 1) if codec == "zlib" else raw


def decode(
    blob: bytes, codec: str, shape: tuple[int, ...] | None, dtype: type
) -> np.ndarray:
    """Decode what :func:`encode` produced, back to the exact array.

    Args:
        blob: The stored bytes.
        codec: The codec it was written with.
        shape: The array's shape, from the calibration; None for a PNG.
        dtype: The array's element type.

    Raises:
        StreamError: If the blob is not an image of that type and shape.
    """
    if codec == "png":
        image = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None or image.dtype != dtype:
            raise StreamError(f"PNG blob did not decode to a {np.dtype(dtype)} image")
        return cv2.cvtColor(image, cv2.COLOR_BGR2RGB) if image.ndim == 3 else image
    if codec == "zlib":
        blob = zlib.decompress(blob)
    values = np.frombuffer(blob, dtype=np.dtype(dtype).newbyteorder("<"))
    if shape is None or values.size != math.prod(shape):
        raise StreamError(f"{codec} blob holds {values.size} values, not shape {shape}")
    return values.reshape(shape)


@dataclass
class WriterStats:
    """What the writer has done so far.

    Attributes:
        frames: Frames written.
        dropped: Frames the queue could not accept: the disk or the encoders
            fell behind.
        motion: Inertial samples written.
    """

    frames: int = 0
    dropped: int = 0
    motion: int = 0


class ArchiveWriter:
    """Writes frame sets to a ``.rrdb`` archive.

    Encoding runs on a thread pool and SQLite writes on one thread of their
    own, so the caller - the camera's reader thread - only does a queue put.
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
        workers: int = DEFAULT_WORKERS,
        clock_anchor: ClockPair | None = None,
    ) -> None:
        """Open an archive for writing.

        Args:
            path: File to create. Overwritten if it exists.
            calibration: Calibration in force, stored once.
            config: Stream configuration, stored once.
            device: Identity of the camera, stored once.
            options: Sensor options at the start of the recording.
            codecs: Overrides for COMPRESSED_CODECS. ``depth`` is ``"zlib"`` or
                ``"raw"``; ``color`` and ``infrared`` are ``"png"`` or ``"raw"``.
            workers: Encoder threads.
            clock_anchor: Host clock pair naming the monotonic axis in
                wall-clock terms, for the inertial timestamps. None reads the
                host's clocks at the first frame; tests pass one explicitly.
        """
        self._path = path
        self._clock_anchor = clock_anchor
        self._codecs = {**COMPRESSED_CODECS, **(codecs or {})}
        self._motion_written = 0
        if self._codecs["depth"] not in ("zlib", "raw"):
            raise ValueError(f"unknown depth codec {self._codecs['depth']!r}")
        if self._codecs["color"] not in ("png", "raw"):
            raise ValueError(f"unknown color codec {self._codecs['color']!r}")
        if self._codecs["infrared"] not in ("png", "raw"):
            raise ValueError(f"unknown infrared codec {self._codecs['infrared']!r}")
        # Frames and inertial samples share one queue, so one writer thread
        # owns the SQLite connection.
        self._queue: queue.Queue[FrameSet | list[MotionSample] | None] = queue.Queue(
            QUEUE_DEPTH
        )
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="archive-encode")
        self._stats = WriterStats()
        self._lock = threading.Lock()
        self._closed = False

        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.executescript(SCHEMA)
        # Crash durability without a flush per frame; folded back on close.
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
                # Filled in from the first frame.
                "timestamp_domain": None,
                "clock_anchor": None,
            }
        )

        self._thread = threading.Thread(
            target=self._run, name="archive-writer", daemon=True
        )
        self._thread.start()

    def __enter__(self) -> Self:
        """Return the open writer."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Flush and close."""
        self.close()

    @property
    def stats(self) -> WriterStats:
        """A snapshot of what has been written."""
        with self._lock:
            return WriterStats(
                frames=self._stats.frames,
                dropped=self._stats.dropped,
                motion=self._motion_written,
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
            timeout: How long to wait for room. None does not wait, for a
                live recording that must not stall the camera. An offline pass
                should pass a generous value, or ``float("inf")`` to insist.

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

    def append_motion(self, samples: list[MotionSample]) -> bool:
        """Queue inertial samples to be written.

        Args:
            samples: What ``LiveSource.drain_motion`` returned. An empty list is
                accepted and does nothing.

        Returns:
            Whether they were accepted. False means the queue was full and they
            were dropped, which is counted like a dropped frame. Never waits.
        """
        if self._closed or not samples:
            return not samples
        try:
            self._queue.put_nowait(samples)
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
            item = self._queue.get()
            if item is None:
                break
            if isinstance(item, list):
                try:
                    self._insert_motion(item)
                except Exception:  # noqa: BLE001 - inertial data is not the video
                    logger.exception("could not write %d inertial samples", len(item))
                continue
            try:
                self._insert(item)
            except Exception:  # noqa: BLE001 - one bad frame is not the session
                logger.exception("could not write frame %d", item.index)
                continue
            pending += 1
            if pending >= COMMIT_EVERY:
                self._commit()
                pending = 0
        self._commit()

    def _insert(self, frames: FrameSet) -> None:
        """Encode one frame set and add it to the open transaction."""
        if self._stats.frames == 0:
            # The domain is only known once a frame arrives; the anchor lets
            # `imu`'s epoch-ms timestamps be placed on the monotonic axis.
            self._write_meta(
                {
                    "timestamp_domain": frames.timestamp_domain,
                    "clock_anchor": (self._clock_anchor or read_clocks()).as_dict(),
                }
            )
        futures = self._submit(frames)
        self._connection.execute(
            "INSERT OR REPLACE INTO frames"
            "(idx, color_timestamp_ms, depth_timestamp_ms, received_monotonic,"
            " depth, color, color_y, color_u, color_v, ir1, ir2, metadata) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frames.index,
                frames.color_timestamp_ms,
                frames.depth_timestamp_ms,
                frames.received_monotonic,
                *(
                    futures[name].result() if futures[name] else None
                    for name in _BLOB_COLUMNS
                ),
                json.dumps(frames.metadata) if frames.metadata else None,
            ),
        )
        with self._lock:
            self._stats.frames += 1

    def _insert_motion(self, samples: list[MotionSample]) -> None:
        """Add inertial samples to the open transaction.

        Args:
            samples: Samples to write, in any order.
        """
        self._connection.executemany(
            "INSERT INTO imu(stream, timestamp_ms, x, y, z) VALUES(?, ?, ?, ?, ?)",
            [
                (sample.stream, sample.timestamp_ms, sample.x, sample.y, sample.z)
                for sample in samples
            ],
        )
        with self._lock:
            self._motion_written += len(samples)

    def _submit(self, frames: FrameSet) -> dict[str, Any]:
        """Start encoding every image in a set, returning one future per column.

        Args:
            frames: The set to encode.

        Returns:
            Column name to future, with None where the set has nothing.
        """
        images: dict[str, tuple[np.ndarray, str]] = {}
        if frames.depth is not None:
            images["depth"] = (frames.depth, self._codecs["depth"])
        if frames.color is not None:
            color_codec = self._codecs["color"]
            if frames.color_format == "yuyv":
                planes = split_yuyv(frames.color)
                for column, plane in zip(("color_y", "color_u", "color_v"), planes):
                    images[column] = (plane, color_codec)
            else:
                images["color"] = (frames.color, color_codec)
        if frames.infrared is not None:
            for column, plane in zip(("ir1", "ir2"), frames.infrared):
                images[column] = (plane, self._codecs["infrared"])
        futures: dict[str, Any] = dict.fromkeys(_BLOB_COLUMNS)
        for column, (image, codec) in images.items():
            futures[column] = self._pool.submit(encode, image, codec)
        return futures

    def _commit(self) -> None:
        """Commit the open transaction."""
        self._connection.commit()


def _extrinsics(raw: dict[str, Any] | None) -> Extrinsics | None:
    """Rebuild a transform from its stored form."""
    if not raw:
        return None
    return Extrinsics(
        rotation=tuple(raw["rotation"]),
        translation=tuple(raw["translation"]),
    )


def _motion_intrinsics(raw: dict[str, Any] | None) -> MotionIntrinsics | None:
    """Rebuild an inertial correction from its stored form."""
    if not raw:
        return None
    noise = tuple(raw["noise_variances"])
    bias = tuple(raw["bias_variances"])
    return MotionIntrinsics(
        data=tuple(raw["data"]),
        noise_variances=(noise[0], noise[1], noise[2]),
        bias_variances=(bias[0], bias[1], bias[2]),
    )


def _motion_calibration(raw: dict[str, Any] | None) -> MotionCalibration | None:
    """Rebuild the inertial calibration from its stored form.

    Args:
        raw: The stored mapping, or None for an archive written before the
            inertial calibration was recorded.

    Returns:
        The calibration, or None if the archive has none.
    """
    if not raw:
        return None
    return MotionCalibration(
        accel=_motion_intrinsics(raw.get("accel")),
        gyro=_motion_intrinsics(raw.get("gyro")),
        depth_to_accel=_extrinsics(raw.get("depth_to_accel")),
        depth_to_gyro=_extrinsics(raw.get("depth_to_gyro")),
    )


def _pair(raw: Any, build) -> tuple[Any, Any]:
    """Rebuild a left/right pair that older archives do not carry."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return (None, None)
    return (build(raw[0]), build(raw[1]))


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


#: What every frame query selects before the blobs.
_FRAME_COLUMNS = (
    "idx, color_timestamp_ms, depth_timestamp_ms, received_monotonic, metadata"
)


class ArchiveSource:
    """Replays a ``.rrdb`` archive as a ``FrameSource``."""

    def __init__(self, path: str) -> None:
        """Prepare to read an archive; :meth:`open` reads it.

        Args:
            path: Archive to read.
        """
        self._path = path
        self._stop = False
        self._connection: sqlite3.Connection | None = None
        self._meta: dict[str, Any] = {}
        self._calibration: Calibration | None = None
        self._codecs: dict[str, str] = {}

    def __enter__(self) -> Self:
        """Open the archive."""
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        """Close it."""
        self.close()

    def open(self) -> None:
        """Read the archive's descriptions and make it ready to iterate.

        Raises:
            StreamError: If the file is not an archive of this format version.
        """
        if self._connection is not None:
            return
        try:
            connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            rows = connection.execute("SELECT key, value FROM meta").fetchall()
        except sqlite3.Error as exc:
            raise StreamError(f"{self._path} is not a readable archive: {exc}") from exc

        self._meta = {key: json.loads(value) for key, value in rows}
        version = self._meta.get("format_version")
        if version != FORMAT_VERSION:
            connection.close()
            raise StreamError(
                f"{self._path} is format version {version}; this reads "
                f"{FORMAT_VERSION}"
            )
        self._codecs = dict(self._meta["codecs"])
        raw = self._meta["calibration"]
        self._calibration = Calibration(
            color=_intrinsics(raw.get("color")),
            depth=_intrinsics(raw.get("depth")),
            depth_scale=raw["depth_scale"],
            depth_to_color=_extrinsics(raw.get("depth_to_color")),
            aligned=raw["aligned"],
            infrared=_pair(raw.get("infrared"), _intrinsics),
            depth_to_infrared=_pair(raw.get("depth_to_infrared"), _extrinsics),
            motion=_motion_calibration(raw.get("motion")),
        )
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
    def timestamp_domain(self) -> str | None:
        """What this recording's frame timestamps mean, or None if unrecorded."""
        return self._meta.get("timestamp_domain")

    @property
    def clock_anchor(self) -> ClockPair | None:
        """One pair of host clocks from the start of the recording.

        None for an archive that holds no frame.
        """
        raw = self._meta.get("clock_anchor")
        return ClockPair.from_dict(raw) if raw else None

    def __len__(self) -> int:
        """How many frames the archive holds."""
        if self._connection is None:
            return 0
        return int(self._connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0])

    def _require_open(self) -> sqlite3.Connection:
        """Return the connection.

        Raises:
            StreamError: If the archive is not open.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading it")
        return self._connection

    def frame_at(self, index: int, *, only: str | None = None) -> FrameSet | None:
        """Return one frame set by its index, without reading the others.

        Args:
            index: The archive's own ``idx``, as :meth:`bounds` reports the
                range of.
            only: Decode just one stream - ``"depth"``, ``"color"`` or
                ``"infrared"`` - leaving the rest None (11 ms instead of 30).

        Returns:
            The set, or None if there is no frame with that index.

        Raises:
            StreamError: If the archive is not open.
            ValueError: If ``only`` names no stream this format has.
        """
        connection = self._require_open()
        if only is not None and only not in _STREAM_COLUMNS:
            raise ValueError(f"no stream called {only!r}")
        wanted = set(_BLOB_COLUMNS) if only is None else set(_STREAM_COLUMNS[only])
        blobs = ", ".join(name if name in wanted else "NULL" for name in _BLOB_COLUMNS)
        row = connection.execute(
            f"SELECT {_FRAME_COLUMNS}, {blobs} FROM frames WHERE idx = ?",
            (index,),
        ).fetchone()
        return None if row is None else self._to_frame_set(row)

    def bounds(self) -> tuple[int, int, float, float] | None:
        """Describe the range of frames the archive holds.

        Returns:
            ``(first_index, last_index, first_monotonic, last_monotonic)``, or
            None if it is empty. Indices may have gaps.

        Raises:
            StreamError: If the archive is not open.
        """
        row = self._require_open().execute(
            "SELECT MIN(idx), MAX(idx), MIN(received_monotonic), "
            "MAX(received_monotonic) FROM frames"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        first, last, start, end = row
        return int(first), int(last), float(start), float(end)

    def motion_samples(self) -> Iterator[MotionSample]:
        """Yield every inertial sample, oldest first.

        Yields:
            The samples, each with ``capture_monotonic`` placed through the
            archive's clock anchor, or None if it has none.

        Raises:
            StreamError: If the archive is not open.
        """
        rows = self._require_open().execute(
            "SELECT stream, timestamp_ms, x, y, z FROM imu ORDER BY timestamp_ms"
        )
        anchor = self.clock_anchor
        for stream, timestamp_ms, x, y, z in rows:
            yield MotionSample(
                stream=stream,
                timestamp_ms=timestamp_ms,
                x=x,
                y=y,
                z=z,
                capture_monotonic=(
                    anchor.epoch_ms_to_monotonic(timestamp_ms) if anchor else None
                ),
            )

    def motion_rate(self) -> dict[str, float]:
        """Measured sample rate of each inertial stream, in Hz.

        Returns:
            Stream name to rate, measured from the timestamps; empty if there
            are no samples.

        Raises:
            StreamError: If the archive is not open.
        """
        rows = self._require_open().execute(
            "SELECT stream, COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) "
            "FROM imu GROUP BY stream"
        ).fetchall()
        rates: dict[str, float] = {}
        for stream, count, first, last in rows:
            span = (last - first) / 1000.0
            if span > 0 and count > 1:
                rates[stream] = (count - 1) / span
        return rates

    def frame_times(self) -> list[tuple[int, float]]:
        """Every frame's index and capture time, without decoding anything.

        Returns:
            ``(index, received_monotonic)`` pairs in order.

        Raises:
            StreamError: If the archive is not open.
        """
        return [
            (int(idx), float(t))
            for idx, t in self._require_open().execute(
                "SELECT idx, received_monotonic FROM frames ORDER BY idx"
            )
        ]

    def frames(self) -> Iterator[FrameSet]:
        """Yield the recorded frame sets in order, decoded.

        Raises:
            StreamError: If the archive is not open.
        """
        rows = self._require_open().execute(
            f"SELECT {_FRAME_COLUMNS}, {', '.join(_BLOB_COLUMNS)} "
            "FROM frames ORDER BY idx"
        )
        for row in rows:
            if self._stop:
                return
            yield self._to_frame_set(row)

    # -- decoding ----------------------------------------------------------

    def _to_frame_set(self, row: tuple) -> FrameSet:
        """Turn one row selected as ``_FRAME_COLUMNS`` then the blobs into a FrameSet."""
        idx, color_timestamp_ms, depth_timestamp_ms, received_monotonic, metadata = row[:5]
        depth_blob, color_blob, y_blob, u_blob, v_blob, ir1, ir2 = row[5:12]
        color, color_format = self._decode_color(color_blob, y_blob, u_blob, v_blob)
        return FrameSet(
            index=idx,
            color_timestamp_ms=color_timestamp_ms,
            depth_timestamp_ms=depth_timestamp_ms,
            received_monotonic=received_monotonic,
            color=color,
            color_format=color_format,
            depth=self._decode_depth(depth_blob),
            infrared=self._decode_infrared(ir1, ir2),
            calibration=self.calibration,
            metadata=json.loads(metadata) if metadata else None,
            timestamp_domain=self._meta.get("timestamp_domain") or "unknown",
        )

    def _image(
        self,
        blob: bytes | None,
        stream: str,
        intrinsics: Intrinsics | None,
        dtype: type,
        *,
        width_divisor: int = 1,
        channels: int = 1,
    ) -> np.ndarray | None:
        """Decode one stored image as the recording's codec for ``stream`` says.

        Raises:
            StreamError: If a codec that needs the calibration's shape finds none.
        """
        if blob is None:
            return None
        codec = self._codecs[stream]
        shape: tuple[int, ...] | None = None
        if codec != "png":
            if intrinsics is None:
                raise StreamError(
                    f"{self._path} stores {codec} {stream} but no calibration "
                    "to give it a shape"
                )
            shape = (intrinsics.height, intrinsics.width // width_divisor)
            if channels > 1:
                shape += (channels,)
        return decode(blob, codec, shape, dtype)

    def _decode_depth(self, blob: bytes | None) -> np.ndarray | None:
        return self._image(blob, "depth", self.calibration.depth, np.uint16)

    def _decode_infrared(
        self, ir1: bytes | None, ir2: bytes | None
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """The infrared pair, which shares depth's resolution, or None."""
        if ir1 is None or ir2 is None:
            return None
        depth = self.calibration.depth
        return (
            self._image(ir1, "infrared", depth, np.uint8),
            self._image(ir2, "infrared", depth, np.uint8),
        )

    def _decode_color(
        self,
        color: bytes | None,
        y: bytes | None,
        u: bytes | None,
        v: bytes | None,
    ) -> tuple[np.ndarray | None, str]:
        """Rebuild the colour image: one blob for rgb8, three planes for yuyv.

        Returns:
            ``(image, format)``.
        """
        intrinsics = self.calibration.color
        if y is not None and u is not None and v is not None:
            planes = (
                self._image(y, "color", intrinsics, np.uint8),
                self._image(u, "color", intrinsics, np.uint8, width_divisor=2),
                self._image(v, "color", intrinsics, np.uint8, width_divisor=2),
            )
            return join_yuyv(*planes), "yuyv"
        return self._image(color, "color", intrinsics, np.uint8, channels=3), "rgb8"
