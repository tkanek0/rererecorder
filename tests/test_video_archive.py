"""The archive: what goes in must come out, including when it was taken.

The lossless round trip is inherited from realsense-playground and re-checked
here because this repository changed the writer. What is new is the time axis:
a frame's ``received_monotonic`` has to survive the file, because that is the
only thing that can be compared with an audio sample - and, since decision 21,
so must ``color_timestamp_ms`` / ``depth_timestamp_ms``, which a consumer
uses to judge how far apart the two sensors' own frames were.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from rrr.timeline import ClockPair
from rrr.video import (
    ArchiveSource,
    ArchiveWriter,
    Calibration,
    DeviceInfo,
    FrameSet,
    Motion,
    StreamConfig,
)

from .conftest import ARRIVAL_LAG_S, FPS, HEIGHT, MONO, OFFSET, REAL, WIDTH


@pytest.fixture
def written(
    tmp_path: Path,
    calibration: Calibration,
    make_frames: Callable[..., FrameSet],
) -> Callable[..., tuple[str, list[FrameSet]]]:
    """Return a factory that writes frame sets to an archive and closes it."""

    def build(count: int = 5, seed: int = 0, **writer_kwargs):
        rng = np.random.default_rng(seed)
        originals = [
            make_frames(
                index=i + 1,
                # Deliberately awkward: the full 16-bit range, an unmeasured
                # band, and colour noise that will not compress. A codec that
                # is quietly lossy fails here rather than on a real scene.
                depth=np.concatenate(
                    [
                        np.zeros((40, WIDTH), np.uint16),
                        rng.integers(0, 65536, (HEIGHT - 40, WIDTH), dtype=np.uint16),
                    ]
                ),
                color=rng.integers(0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8),
                motion=Motion(accel=(0.01 * i, -9.65, -0.94), gyro=(0.1, 0.2, 0.3)),
            )
            for i in range(count)
        ]
        for frames in originals:
            frames.depth[10, 10] = 65535

        path = str(tmp_path / "video.rrdb")
        with ArchiveWriter(
            path,
            calibration=calibration,
            config=StreamConfig(color_format="rgb8"),
            codecs={"depth": "png16"},
            device=DeviceInfo(
                name="Intel RealSense D455",
                serial="311322302077",
                firmware="5.17.3.10",
                usb_type="3.2",
            ),
            **writer_kwargs,
        ) as writer:
            for frames in originals:
                assert writer.append(frames, timeout=10.0)
            assert writer.drain()
        return path, originals

    return build


# -- the time axis ------------------------------------------------------------


def test_capture_time_survives_the_file(written) -> None:
    """The one number the audio can be compared with has to come back exactly."""
    path, originals = written()

    with ArchiveSource(path) as archive:
        assert archive.has_monotonic
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert restored.received_monotonic == pytest.approx(
                original.received_monotonic, abs=1e-9
            )


def test_each_streams_own_timestamp_survives(written) -> None:
    """Independently: frame n's colour and depth were both taken at MONO + n/30.

    Checked against the arithmetic rather than against the object it came
    from, so that a writer storing the wrong column cannot pass.
    """
    path, _ = written(count=5)

    with ArchiveSource(path) as archive:
        for restored in archive.frames():
            expected_ms = (MONO + restored.index / FPS + OFFSET) * 1000.0
            assert restored.color_timestamp_ms == pytest.approx(expected_ms, abs=1e-3)
            assert restored.depth_timestamp_ms == pytest.approx(expected_ms, abs=1e-3)


def test_intervals_between_frames_survive(written) -> None:
    """30 fps in must be 30 fps out, on the monotonic axis."""
    path, _ = written(count=10)

    with ArchiveSource(path) as archive:
        times = [frames.received_monotonic for frames in archive.frames()]
    gaps = np.diff(times)
    assert gaps == pytest.approx(1.0 / FPS, abs=1e-6)


def test_the_domain_and_an_anchor_are_recorded(written) -> None:
    path, _ = written(clock_anchor=ClockPair(MONO, REAL))

    with ArchiveSource(path) as archive:
        assert archive.timestamp_domain == "global_time"
        anchor = archive.clock_anchor
        assert anchor is not None
        assert anchor.offset == pytest.approx(OFFSET, abs=1e-6)


def test_a_hardware_clock_recording_says_so(
    tmp_path, calibration, make_frames
) -> None:
    """A recording the audio cannot be lined up against must be marked as such."""
    path = str(tmp_path / "video.rrdb")
    with ArchiveWriter(
        path, calibration=calibration, config=StreamConfig(color_format="rgb8")
    ) as writer:
        frames = make_frames(
            index=1,
            depth=np.zeros((HEIGHT, WIDTH), np.uint16),
            timestamp_domain="hardware_clock",
        )
        assert writer.append(frames, timeout=10.0)
        assert writer.drain()

    with ArchiveSource(path) as archive:
        assert archive.timestamp_domain == "hardware_clock"
        restored = next(archive.frames())
        # received_monotonic is stored as-is regardless of domain; the domain
        # itself is what tells a consumer color_timestamp_ms/depth_timestamp_ms
        # are not to be trusted against anything but each other.
        assert restored.received_monotonic == pytest.approx(
            MONO + 1 / FPS + ARRIVAL_LAG_S, abs=1e-9
        )


# -- the lossless round trip, re-checked after the writer changed -------------


def test_depth_survives_exactly(written) -> None:
    """A depth value that changed is a measurement destroyed."""
    path, originals = written()

    with ArchiveSource(path) as archive:
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert np.array_equal(original.depth, restored.depth)


def test_colour_survives_exactly(written) -> None:
    path, originals = written()

    with ArchiveSource(path) as archive:
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert np.array_equal(original.color, restored.color)


def test_metadata_survives(written) -> None:
    path, originals = written()

    with ArchiveSource(path) as archive:
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert restored.metadata == original.metadata


def test_a_frames_own_motion_is_not_stored(written) -> None:
    """FrameSet.motion is a convenience, not the inertial data.

    It holds whichever samples were newest when the frame was assembled - one
    per frame against the sensor's 480 Hz. Storing it would put a fourteenth of
    the data in the file twice; the samples themselves go to `imu`, and
    ``motion_samples`` is what reads them.
    """
    path, _ = written()

    with ArchiveSource(path) as archive:
        assert next(archive.frames()).motion is None
        assert list(archive.motion_samples()) == []


def test_calibration_and_device_survive(written, calibration) -> None:
    path, _ = written()

    with ArchiveSource(path) as archive:
        assert archive.calibration.as_dict() == calibration.as_dict()
        assert archive.device.serial == "311322302077"


# -- compatibility, both ways -------------------------------------------------


def test_an_upstream_archive_without_the_new_columns_still_reads(written) -> None:
    """A file from realsense-playground (or this repo's own v1-v3) has none of
    ``color_timestamp_ms`` / ``depth_timestamp_ms`` / ``received_monotonic``.

    Its frames are identical - the pixels, the calibration, the inertial samples -
    and only the newer, finer-grained timing is missing. Refusing to open it
    would be worse than saying so, because there is a lot in such a file that
    is still correct.
    """
    path, originals = written(count=3)

    # Rebuild the file as the upstream writer would have left it: v1, the old
    # column set, PNG16 depth, and no record of a codec choice.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE old AS
                SELECT idx, depth_timestamp_ms AS timestamp_ms,
                       received_monotonic AS received_at, depth, color, metadata
                FROM frames;
            DROP TABLE frames;
            ALTER TABLE old RENAME TO frames;
            """
        )
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'format_version'")
        connection.execute("DELETE FROM meta WHERE key = 'codecs'")

    with ArchiveSource(path) as archive:
        # received_at alone is still enough to place a frame against audio -
        # just without color_timestamp_ms/depth_timestamp_ms's finer detail.
        assert archive.has_monotonic is True
        restored = list(archive.frames())

    assert len(restored) == 3
    assert np.array_equal(restored[0].depth, originals[0].depth)
    assert restored[0].color_timestamp_ms is None
    assert restored[0].depth_timestamp_ms is None
    # Without the newer columns, the best available answer is arrival time -
    # the same value this format always used for received_monotonic anyway.
    assert restored[0].received_monotonic == pytest.approx(
        originals[0].received_monotonic, abs=1e-9
    )


def test_written_files_declare_the_current_version(written) -> None:
    """Each version changed what an existing structure means.

    v2 and v3 are different: colour moved to three columns, depth may be zlib
    rather than PNG, and the per-frame ``motion`` table became ``imu`` at the
    sensor's own rate. v4 replaces ``timestamp_ms`` / ``received_at`` /
    ``capture_monotonic`` with ``color_timestamp_ms`` / ``depth_timestamp_ms``
    / ``received_monotonic`` (decision 21). A reader of an older version would
    misread or silently miss all of these, so it refuses.
    """
    path, _ = written()
    with ArchiveSource(path) as archive:
        assert archive.meta["format_version"] == 4


def test_the_file_is_readable_as_plain_sql(written) -> None:
    """No SDK, no library: the container is SQLite and stays inspectable."""
    path, originals = written(count=4)

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT idx, color_timestamp_ms, depth_timestamp_ms, received_monotonic "
            "FROM frames ORDER BY idx"
        ).fetchall()

    assert len(rows) == 4
    for (idx, color_ms, depth_ms, monotonic), original in zip(
        rows, originals, strict=True
    ):
        assert idx == original.index
        assert color_ms == pytest.approx(original.color_timestamp_ms)
        assert depth_ms == pytest.approx(original.depth_timestamp_ms)
        assert monotonic == pytest.approx(original.received_monotonic)
