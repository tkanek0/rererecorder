"""Reproduce decision 22's SQLite/WAL write-throughput finding.

Isolates the archive's own insert cost from encoding by writing pre-sized
byte blobs directly into the exact schema and pragmas
`rrr.video.archive.ArchiveWriter` uses - no device, no encoder, nothing but
disk and SQLite. What decision 22 measured with this: compressed-size blobs
(~600 KB, a whole six-image set) insert at 10.2 ms each - `ArchiveWriter` was
never disk-bound at that size - but raw-size blobs (~1.8 MB, no compression)
at 55.5 ms each, past the 33.3 ms/frame budget on its own, independent of any
encoding cost at all. That is why decision 22's raw codec fixes colour alone
(about 61 MB/s, comfortably inside this) but not the full six-image set.

    uv run python tests/perf/sqlite_write_benchmark.py
    uv run python tests/perf/sqlite_write_benchmark.py --dir data/ --frames 3600

No device needed - only a disk to measure. Not part of `pytest`: it takes
real wall-clock time and its answer is about the disk and machine it runs on,
not about this repository's correctness. Worth rerunning after a disk change,
an OS update, or on a different machine before trusting decision 22's numbers
there.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
import time

from rrr.video.archive import COMMIT_EVERY, SCHEMA

#: 33.3 ms/frame is the budget a 30 fps recorder has for everything.
FRAME_BUDGET_MS = 1000.0 / 30.0

_INSERT = (
    "INSERT OR REPLACE INTO frames"
    "(idx, color_timestamp_ms, depth_timestamp_ms, received_monotonic,"
    " depth, color, color_y, color_u, color_v, ir1, ir2, metadata) "
    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _insert_batch(
    connection: sqlite3.Connection, frames: int, blob_bytes: int
) -> list[float]:
    """Insert ``frames`` rows carrying a same-sized random blob each.

    Args:
        connection: An open connection with the schema already applied.
        frames: How many rows to insert.
        blob_bytes: Size of the ``depth`` blob each row carries - the other
            blob columns stay NULL, since only total bytes per insert is what
            this is measuring.

    Returns:
        Seconds spent in each individual ``execute()``, in order.

    One blob reused for every row rather than a fresh one per insert: decision
    22's own benchmark was about blob *size*, not content, and generating
    random bytes 300 times over would only add noise from the RNG itself.
    """
    durations = []
    blob = os.urandom(blob_bytes)
    for idx in range(frames):
        start = time.perf_counter()
        connection.execute(
            _INSERT,
            (idx, float(idx), float(idx), float(idx), blob, None, None, None, None, None, None, None),
        )
        durations.append(time.perf_counter() - start)
        if (idx + 1) % COMMIT_EVERY == 0:
            connection.commit()
    connection.commit()
    return durations


def _run(path: str, frames: int, blob_bytes: int, label: str) -> bool:
    """Benchmark one blob size, printing the result.

    Returns:
        Whether the mean insert time cleared the 30 fps frame budget.
    """
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        began = time.perf_counter()
        durations = _insert_batch(connection, frames, blob_bytes)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    # Until it is on the disk, not in the page cache. With synchronous=NORMAL an
    # insert returns once the kernel has the bytes, so the per-insert figure is
    # SQLite's cost and says nothing about the disk; on a machine with RAM to
    # spare, gigabytes fit in the cache and never wait for it. This one does.
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    durable_s = time.perf_counter() - began

    mean_ms = 1000 * sum(durations) / len(durations)
    worst_ms = 1000 * max(durations)
    ok = mean_ms < FRAME_BUDGET_MS
    verdict = "OK" if ok else f"OVER the {FRAME_BUDGET_MS:.1f} ms/frame budget"
    print(
        f"{label:>16}: {blob_bytes / 1024:7.0f} KB/blob -> "
        f"{mean_ms:6.2f} ms/insert mean, {worst_ms:6.2f} ms worst, "
        f"{blob_bytes / 1e6 / (mean_ms / 1000):6.1f} MB/s  [{verdict}]"
    )
    print(
        f"{'':>16}  {frames * blob_bytes / 1e9:.1f} GB durable in {durable_s:.1f} s"
        f" -> {frames * blob_bytes / 1e6 / durable_s:6.1f} MB/s to the disk"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    """Run both blob sizes decision 22 measured, plus whatever else is asked for.

    Args:
        argv: Command line arguments, or None to read them from the process.

    Returns:
        0 if every size measured cleared the frame budget, 1 otherwise - so a
        regression on a future machine or disk shows up as a failing command,
        not just a number to eyeball.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--frames",
        type=int,
        default=300,
        help="inserts per size - 300 is 10 s of a 30 fps recording",
    )
    parser.add_argument(
        "--compressed-kb",
        type=int,
        default=600,
        help="decision 22's measured size for the compressed six-image set",
    )
    parser.add_argument(
        "--raw-kb",
        type=int,
        default=1800,
        help="decision 22's measured size for the uncompressed six-image set",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="disk to measure: where the scratch archives go (default: the "
        "system temporary directory, which is usually not where recordings go)",
    )
    args = parser.parse_args(argv)

    ok = True
    with tempfile.TemporaryDirectory(prefix="rrr-sqlite-bench-", dir=args.dir) as tmp:
        ok &= _run(
            os.path.join(tmp, "compressed.rrdb"),
            args.frames,
            args.compressed_kb * 1024,
            "compressed-size",
        )
        ok &= _run(
            os.path.join(tmp, "raw.rrdb"),
            args.frames,
            args.raw_kb * 1024,
            "raw-size",
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
