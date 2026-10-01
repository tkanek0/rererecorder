"""The ``.rrdb`` recording format: SQLite holding losslessly compressed frames.

Stores pixels, calibration, stream configuration, sensor options, inertial
samples and per-frame metadata. Lossless codecs instead of rosbag2's raw pixels:
220 GB/hour for colour and depth at 848x480/30 there, about 91 GB/hour here.
See docs/features.md "Recording" and docs/decisions.md 4, 5, 8, 22.
"""

from __future__ import annotations

import json
import logging
import os
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

from rrr.timeline import ClockPair, read_clocks

from .config import StreamConfig
from .source import StreamError
from .types import (
    Calibration,
    DeviceInfo,
    Extrinsics,
    FrameSet,
    Intrinsics,
    Motion,
    MotionCalibration,
    MotionIntrinsics,
    MotionSample,
    join_yuyv,
    split_yuyv,
)

logger = logging.getLogger(__name__)

#: Bumped when the schema changes in a way a reader must know about: 2 in
#: docs/decisions.md 8, 3 in 12, 4 in 21. The reader still opens v1 to v3.
FORMAT_VERSION = 4

#: The oldest format this reader accepts.
MIN_READABLE_VERSION = 1

#: Meta key listing what this file carries beyond the base schema, so a reader
#: can say what it is missing rather than silently doing without.
EXTENSIONS_KEY = "extensions"

#: Suffix these files carry; see docs/decisions.md 8.
SUFFIX = ".rrdb"

#: What realsense-playground writes. Readable here; never written here.
LEGACY_SUFFIX = ".rsdb"

#: PNG effort. Level 1: 13 ms depth, 19 ms colour; level 6 saves 17% at three
#: times the time, which 30 fps cannot afford.
PNG_LEVEL = 1

#: How each stream is encoded by default; ``"raw"`` is the uncompressed
#: alternative for each (docs/decisions.md 22).
DEFAULT_CODECS = {"depth": "zlib", "color": "png", "infrared": "png"}

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
    depth              BLOB,          -- zlib or PNG16; meta.codecs says which
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


def encode_depth_zlib(depth: np.ndarray) -> bytes:
    """Compress a raw depth image with zlib.

    Args:
        depth: ``(height, width)`` uint16.

    Returns:
        A zlib stream of the raw values, little-endian regardless of host. The
        shape comes from the archive's calibration (docs/decisions.md 5).
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


def encode_depth_raw(depth: np.ndarray) -> bytes:
    """Store a raw depth image's bytes directly, no compression at all.

    Args:
        depth: ``(height, width)`` uint16.

    Returns:
        The values as little-endian bytes, row-major. The shape comes from the
        archive's calibration. See docs/decisions.md 22.
    """
    return depth.astype("<u2", copy=False).tobytes()


def decode_depth_raw(blob: bytes, shape: tuple[int, int]) -> np.ndarray:
    """Decode a raw depth blob.

    Args:
        blob: What :func:`encode_depth_raw` produced.
        shape: ``(height, width)``, from the recording's calibration.

    Returns:
        The uint16 array that was written.

    Raises:
        StreamError: If the blob does not hold exactly that many values.
    """
    values = np.frombuffer(blob, dtype="<u2")
    if values.size != shape[0] * shape[1]:
        raise StreamError(
            f"depth blob holds {values.size} values, not {shape[0] * shape[1]}"
        )
    return values.reshape(shape)


def encode_plane_raw(plane: np.ndarray) -> bytes:
    """Store an 8-bit plane's bytes directly - infrared, or a YUYV component.

    Args:
        plane: ``(height, width)`` uint8.

    Returns:
        The raw bytes, row-major. See docs/decisions.md 22.
    """
    return plane.tobytes()


def decode_plane_raw(blob: bytes, shape: tuple[int, int]) -> np.ndarray:
    """Decode a raw 8-bit plane blob.

    Args:
        blob: What :func:`encode_plane_raw` produced.
        shape: ``(height, width)``, from the recording's calibration.

    Raises:
        StreamError: If the blob does not hold exactly that many bytes.
    """
    values = np.frombuffer(blob, dtype=np.uint8)
    if values.size != shape[0] * shape[1]:
        raise StreamError(
            f"plane blob holds {values.size} bytes, not {shape[0] * shape[1]}"
        )
    return values.reshape(shape)


def encode_plane(plane: np.ndarray) -> bytes:
    """Encode one 8-bit plane - infrared, or a YUYV component - as a PNG.

    Args:
        plane: ``(height, width)`` uint8.

    Returns:
        A PNG; on 8-bit data it beats zlib on size (10.8 ms, 231 KB for
        1280x720 infrared).

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
        A 16-bit PNG, readable by ordinary image tools.

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


def encode_color_raw(color: np.ndarray) -> bytes:
    """Store an rgb8 colour image's bytes directly, no compression.

    Args:
        color: ``(height, width, 3)`` uint8 RGB.

    Returns:
        The raw bytes, row-major, RGB order.
    """
    return color.tobytes()


def decode_color_raw(blob: bytes, shape: tuple[int, int]) -> np.ndarray:
    """Decode a raw rgb8 colour blob.

    Args:
        blob: What :func:`encode_color_raw` produced.
        shape: ``(height, width)``, from the recording's calibration.
    """
    values = np.frombuffer(blob, dtype=np.uint8)
    if values.size != shape[0] * shape[1] * 3:
        raise StreamError(
            f"colour blob holds {values.size} bytes, not {shape[0] * shape[1] * 3}"
        )
    return values.reshape((shape[0], shape[1], 3))


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
        dropped: Frames the queue could not accept: the disk or the encoders
            fell behind.
        motion: Inertial samples written.
        bytes_written: Size of the file on disk at the last commit.
    """

    frames: int = 0
    dropped: int = 0
    motion: int = 0
    bytes_written: int = 0


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
            codecs: Overrides for DEFAULT_CODECS. ``depth`` is ``"zlib"``,
                ``"png16"`` or ``"raw"``; ``color`` and ``infrared`` are
                ``"png"`` or ``"raw"``.
            workers: Encoder threads.
            clock_anchor: Host clock pair naming the monotonic axis in
                wall-clock terms, for the inertial timestamps. None reads the
                host's clocks at the first frame; tests pass one explicitly.
        """
        self._path = path
        self._clock_anchor = clock_anchor
        self._codecs = {**DEFAULT_CODECS, **(codecs or {})}
        self._motion_written = 0
        if self._codecs["depth"] not in ("zlib", "png16", "raw"):
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
                EXTENSIONS_KEY: [],
                # Filled in from the first frame.
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
                motion=self._motion_written,
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
        # FrameSet.motion is not written; samples go to `imu` via
        # append_motion (docs/decisions.md 12).
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
        futures: dict[str, Any] = dict.fromkeys(_BLOB_COLUMNS)
        if frames.depth is not None:
            if self._codecs["depth"] == "zlib":
                encoder = encode_depth_zlib
            elif self._codecs["depth"] == "raw":
                encoder = encode_depth_raw
            else:
                encoder = encode_depth
            futures["depth"] = self._pool.submit(encoder, frames.depth)
        plane_encoder = (
            encode_plane_raw if self._codecs["infrared"] == "raw" else encode_plane
        )
        if frames.color is not None:
            if frames.color_format == "yuyv":
                planes = split_yuyv(frames.color)
                color_plane_encoder = (
                    encode_plane_raw if self._codecs["color"] == "raw" else encode_plane
                )
                for column, plane in zip(("color_y", "color_u", "color_v"), planes):
                    futures[column] = self._pool.submit(color_plane_encoder, plane)
            elif self._codecs["color"] == "raw":
                futures["color"] = self._pool.submit(encode_color_raw, frames.color)
            else:
                futures["color"] = self._pool.submit(encode_color, frames.color)
        if frames.infrared is not None:
            futures["ir1"] = self._pool.submit(plane_encoder, frames.infrared[0])
            futures["ir2"] = self._pool.submit(plane_encoder, frames.infrared[1])
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


class ArchiveSource:
    """Replays a ``.rrdb`` (or legacy ``.rsdb``) archive as a ``FrameSource``."""

    def __init__(self, path: str, *, realtime: bool = False, loop: bool = False) -> None:
        """Open an archive for reading.

        Args:
            path: Archive to read.
            realtime: Pace playback at the rate the frames were recorded.
                False replays as fast as the reader can consume.
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
        self._tables: set[str] = set()
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
        self._calibration = Calibration(
            color=_intrinsics(raw.get("color")),
            depth=_intrinsics(raw.get("depth")),
            depth_scale=raw["depth_scale"],
            depth_to_color=_extrinsics(raw.get("depth_to_color")),
            aligned=raw["aligned"],
            # Absent from older archives; reads back as unknown.
            infrared=_pair(raw.get("infrared"), _intrinsics),
            depth_to_infrared=_pair(raw.get("depth_to_infrared"), _extrinsics),
            motion=_motion_calibration(raw.get("motion")),
        )
        # Detected, not inferred from the version: realsense-playground's
        # files lack columns ours have.
        self._columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(frames)").fetchall()
        }
        self._has_monotonic = bool(
            self._columns & {"received_monotonic", "capture_monotonic", "received_at"}
        )
        # Which inertial table this file has: `imu` from v3, `motion` before it.
        self._tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
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

        True if it has ``received_monotonic`` (v4+), ``capture_monotonic``
        (v2-v3) or ``received_at`` (every version).
        """
        return self._has_monotonic

    @property
    def timestamp_domain(self) -> str | None:
        """What this recording's frame timestamps mean, or None if unrecorded."""
        return self._meta.get("timestamp_domain")

    @property
    def clock_anchor(self) -> ClockPair | None:
        """One pair of host clocks from the start of the recording.

        None for an archive written without one.
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
                ``"infrared"`` - leaving the rest None (11 ms instead of 30).

        Returns:
            The set, or None if there is no frame with that index.

        Raises:
            StreamError: If the archive is not open.
            ValueError: If ``only`` names no stream this format has.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading frames")
        if only is not None and only not in _STREAM_COLUMNS:
            raise ValueError(f"no stream called {only!r}")

        wanted = (
            set(_BLOB_COLUMNS) if only is None else set(_STREAM_COLUMNS[only])
        )
        color_ts, depth_ts, monotonic = self._timestamp_columns()
        blobs = ", ".join(
            f"f.{name}" if name in self._columns and name in wanted else "NULL"
            for name in _BLOB_COLUMNS
        )
        row = self._connection.execute(
            f"SELECT f.idx, {color_ts}, {depth_ts}, {monotonic}, f.metadata,"
            f"       {blobs},"
            f"       {self._motion_columns()} "
            f"FROM frames f {self._motion_join()} WHERE f.idx = ?",
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
        if self._connection is None:
            raise StreamError("open the archive before reading frames")
        _, _, monotonic = self._timestamp_columns(prefix="")
        row = self._connection.execute(
            f"SELECT MIN(idx), MAX(idx), MIN({monotonic}), MAX({monotonic}) FROM frames"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        first, last, start, end = row
        return int(first), int(last), float(start or 0.0), float(end or 0.0)

    def motion_samples(self) -> Iterator[MotionSample]:
        """Yield every inertial sample, oldest first.

        Yields:
            The samples, each carrying the archive's clock anchor so that
            ``capture_monotonic`` works.

        Raises:
            StreamError: If the archive is not open.

        Reads ``imu`` (v3+) or the per-frame ``motion`` table (v1-v2);
        :meth:`motion_rate` tells which a consumer got.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading samples")
        anchor = self.clock_anchor

        if "imu" in self._tables:
            rows = self._connection.execute(
                "SELECT stream, timestamp_ms, x, y, z FROM imu ORDER BY timestamp_ms"
            )
            for stream, timestamp_ms, x, y, z in rows:
                yield MotionSample(
                    stream=stream,
                    timestamp_ms=timestamp_ms,
                    x=x,
                    y=y,
                    z=z,
                    clock=anchor,
                )
            return

        if "motion" not in self._tables:
            return
        # One row held both readings; split it into two samples.
        rows = self._connection.execute(
            "SELECT timestamp_ms, ax, ay, az, gx, gy, gz FROM motion ORDER BY idx"
        )
        for timestamp_ms, ax, ay, az, gx, gy, gz in rows:
            if ax is not None:
                yield MotionSample("accel", timestamp_ms, ax, ay, az, clock=anchor)
            if gx is not None:
                yield MotionSample("gyro", timestamp_ms, gx, gy, gz, clock=anchor)

    def motion_rate(self) -> dict[str, float]:
        """Measured sample rate of each inertial stream, in Hz.

        Returns:
            Stream name to rate, measured from the timestamps; empty if there
            are no samples. A per-frame (v1-v2) recording reports ~30 Hz.

        Raises:
            StreamError: If the archive is not open.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading samples")
        table, column = (
            ("imu", "stream") if "imu" in self._tables else ("motion", "'both'")
        )
        if table not in self._tables:
            return {}
        rows = self._connection.execute(
            f"SELECT {column}, COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) "
            f"FROM {table} GROUP BY {column}"
        ).fetchall()
        rates: dict[str, float] = {}
        for stream, count, first, last in rows:
            span = (last - first) / 1000.0 if last and first else 0.0
            if span > 0 and count > 1:
                rates[stream] = (count - 1) / span
        return rates

    def frame_times(self) -> list[tuple[int, float]]:
        """Every frame's index and capture time, without decoding anything.

        Returns:
            ``(index, received_monotonic)`` pairs in order. Empty if the
            archive stores no capture times.

        Raises:
            StreamError: If the archive is not open.
        """
        if self._connection is None:
            raise StreamError("open the archive before reading frames")
        if not self._has_monotonic:
            return []
        _, _, monotonic = self._timestamp_columns(prefix="")
        return [
            (int(idx), float(t))
            for idx, t in self._connection.execute(
                f"SELECT idx, {monotonic} FROM frames "
                f"WHERE {monotonic} IS NOT NULL ORDER BY idx"
            )
        ]

    def indices(self) -> list[int]:
        """Every frame index the archive holds, in order.

        Returns:
            The indices. About 860 KB of JSON for an hour, too much for a
            player to fetch.

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
            StreamError: If the file claims a zlib or raw depth but records no
                depth calibration to give it a shape.
        """
        if blob is None:
            return None
        codec = self._codecs.get("depth")
        if codec not in ("zlib", "raw"):
            return decode_depth(blob)
        intrinsics = self.calibration.depth
        if intrinsics is None:
            raise StreamError(
                f"{self._path} stores {codec} depth but no depth calibration, "
                "so the image shape is unknown"
            )
        shape = (intrinsics.height, intrinsics.width)
        return (
            decode_depth_raw(blob, shape)
            if codec == "raw"
            else decode_depth_zlib(blob, shape)
        )

    def _motion_columns(self) -> str:
        """The six inertial columns to select, or nulls in their place.

        Returns:
            SQL. Nulls from v3 on, where ``FrameSet.motion`` is None and
            :meth:`motion_samples` holds the data.
        """
        if "motion" in self._tables:
            return "m.ax, m.ay, m.az, m.gx, m.gy, m.gz"
        return "NULL, NULL, NULL, NULL, NULL, NULL"

    def _motion_join(self) -> str:
        """The join onto the old per-frame inertial table, if there is one."""
        if "motion" in self._tables:
            return "LEFT JOIN motion m ON m.idx = f.idx"
        return ""

    def _timestamp_columns(self, prefix: str = "f.") -> tuple[str, str, str]:
        """The three timestamp expressions to select, whichever version this is.

        Args:
            prefix: Table alias to qualify column names with, or ``""`` for a
                query with no join.

        Returns:
            ``(color_timestamp_ms, depth_timestamp_ms, received_monotonic)``
            SQL expressions. For v1-v3 the per-stream timestamps are NULL and
            ``received_monotonic`` maps to the closest old column.
        """
        color_ts = f"{prefix}color_timestamp_ms" if "color_timestamp_ms" in self._columns else "NULL"
        depth_ts = f"{prefix}depth_timestamp_ms" if "depth_timestamp_ms" in self._columns else "NULL"
        if "received_monotonic" in self._columns:
            monotonic = f"{prefix}received_monotonic"
        elif "capture_monotonic" in self._columns and "received_at" in self._columns:
            # capture_monotonic is NULL on rows not in global_time.
            monotonic = f"COALESCE({prefix}capture_monotonic, {prefix}received_at)"
        elif "received_at" in self._columns:
            monotonic = f"{prefix}received_at"
        else:
            monotonic = "NULL"
        return color_ts, depth_ts, monotonic

    def _to_frame_set(self, row: tuple, *, realtime: bool = False) -> FrameSet:
        """Turn one selected row into a FrameSet.

        Args:
            row: The columns in the order both queries select them:
                ``idx, color_timestamp_ms, depth_timestamp_ms,
                received_monotonic, metadata, <blobs>, <motion>``.
            realtime: Stamp ``received_monotonic`` with now rather than with
                what was recorded.

        Returns:
            The frame set, with every stream decoded.
        """
        idx, color_timestamp_ms, depth_timestamp_ms, received_monotonic, metadata = row[:5]
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
            color_timestamp_ms=color_timestamp_ms,
            depth_timestamp_ms=depth_timestamp_ms,
            received_monotonic=time.monotonic() if realtime else received_monotonic,
            color=color,
            color_format=color_format,
            depth=self._decode_depth(depth_blob),
            infrared=self._decode_infrared(ir1, ir2),
            calibration=self.calibration,
            motion=motion,
            metadata=json.loads(metadata) if metadata else None,
            timestamp_domain=self._meta.get("timestamp_domain") or "unknown",
        )

    def _decode_infrared(
        self, ir1: bytes | None, ir2: bytes | None
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Decode the infrared pair according to what the recording says it is.

        Args:
            ir1: Left plane's blob, or None.
            ir2: Right plane's blob, or None.

        Returns:
            ``(left, right)``, or None if either is missing.

        Raises:
            StreamError: If the codec is raw but the recording has no depth
                calibration, whose resolution infrared shares.
        """
        if ir1 is None or ir2 is None:
            return None
        if self._codecs.get("infrared") != "raw":
            return decode_plane(ir1), decode_plane(ir2)
        intrinsics = self.calibration.depth
        if intrinsics is None:
            raise StreamError(
                f"{self._path} stores raw infrared but no depth calibration, "
                "so the plane shape is unknown"
            )
        shape = (intrinsics.height, intrinsics.width)
        return decode_plane_raw(ir1, shape), decode_plane_raw(ir2, shape)

    def _decode_color(
        self,
        color: bytes | None,
        y: bytes | None,
        u: bytes | None,
        v: bytes | None,
    ) -> tuple[np.ndarray | None, str]:
        """Rebuild the colour image from whichever columns hold it.

        Args:
            color: Single blob (PNG, or raw if the codec says so), written
                when the format was rgb8.
            y: Luma plane, written when the format was yuyv.
            u: First chroma plane.
            v: Second chroma plane.

        Returns:
            ``(image, format)``.

        Raises:
            StreamError: If the codec is raw but the recording has no colour
                calibration to give the planes their shape.
        """
        raw = self._codecs.get("color") == "raw"
        if y is not None and u is not None and v is not None:
            if not raw:
                return join_yuyv(decode_plane(y), decode_plane(u), decode_plane(v)), "yuyv"
            intrinsics = self.calibration.color
            if intrinsics is None:
                raise StreamError(
                    f"{self._path} stores raw colour but no colour "
                    "calibration, so the plane shapes are unknown"
                )
            width = intrinsics.width
            half = (intrinsics.height, width // 2)
            return (
                join_yuyv(
                    decode_plane_raw(y, (intrinsics.height, width)),
                    decode_plane_raw(u, half),
                    decode_plane_raw(v, half),
                ),
                "yuyv",
            )
        if color is not None:
            if not raw:
                return decode_color(color), "rgb8"
            intrinsics = self.calibration.color
            if intrinsics is None:
                raise StreamError(
                    f"{self._path} stores raw colour but no colour "
                    "calibration, so the image shape is unknown"
                )
            return (
                decode_color_raw(color, (intrinsics.height, intrinsics.width)),
                "rgb8",
            )
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
            color_ts, depth_ts, monotonic = self._timestamp_columns()
            # NULL for columns a v1 file lacks.
            blobs = ", ".join(
                f"f.{name}" if name in self._columns else "NULL"
                for name in _BLOB_COLUMNS
            )
            rows = self._connection.execute(
                f"SELECT f.idx, {color_ts}, {depth_ts}, {monotonic}, f.metadata,"
                f"       {blobs},"
                f"       {self._motion_columns()} "
                f"FROM frames f {self._motion_join()} ORDER BY f.idx"
            )
            empty = True
            for row in rows:
                if self._stop:
                    return
                empty = False
                received_monotonic = row[3]
                if self._realtime and previous is not None:
                    delay = received_monotonic - previous
                    if 0 < delay < 5:
                        time.sleep(delay)
                previous = received_monotonic

                yield self._to_frame_set(row, realtime=self._realtime)
            if empty or not self._loop:
                return
