"""The archive: what goes in must come out, including when it was taken.

The lossless round trip is inherited from realsense-playground and re-checked
here because this repository changed the writer. What is new is the time axis:
a frame's ``capture_monotonic`` has to survive the file, because that is the
only thing that can be compared with an audio sample.
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

from .conftest import FPS, HEIGHT, MONO, OFFSET, REAL, WIDTH


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
            assert restored.capture_monotonic == pytest.approx(
                original.capture_monotonic, abs=1e-9
            )


def test_capture_time_is_the_frames_own_instant_not_its_arrival(written) -> None:
    """Independently: frame n was taken at MONO + n/30, and arrival was later.

    Checked against the arithmetic rather than against the object it came from,
    so that a writer storing ``received_at`` in the wrong column cannot pass.
    """
    path, _ = written(count=5)

    with ArchiveSource(path) as archive:
        for restored in archive.frames():
            assert restored.capture_monotonic == pytest.approx(
                MONO + restored.index / FPS, abs=1e-6
            )
            assert restored.received_at > restored.capture_monotonic


def test_intervals_between_frames_survive(written) -> None:
    """30 fps in must be 30 fps out, on the monotonic axis."""
    path, _ = written(count=10)

    with ArchiveSource(path) as archive:
        times = [frames.capture_monotonic for frames in archive.frames()]
    gaps = np.diff(times)
    assert gaps == pytest.approx(1.0 / FPS, abs=1e-6)


def test_the_domain_and_an_anchor_are_recorded(written) -> None:
    path, originals = written()

    with ArchiveSource(path) as archive:
        assert archive.timestamp_domain == "global_time"
        anchor = archive.clock_anchor
        assert anchor is not None
        assert anchor.offset == pytest.approx(OFFSET, abs=1e-6)
        # The anchor is what names the monotonic axis in wall-clock terms.
        assert anchor.to_realtime(originals[0].capture_monotonic) == pytest.approx(
            REAL + 1 / FPS, abs=1e-3
        )


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
        # Falls back to arrival, and the domain says why.
        assert restored.capture_monotonic == pytest.approx(
            restored.received_at, abs=1e-9
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


def test_an_upstream_archive_without_the_column_still_reads(written) -> None:
    """A file from realsense-playground has no capture_monotonic column.

    Its frames are identical - the pixels, the calibration, the inertial samples -
    and only the time axis is missing. Refusing to open it would be worse than
    saying so, because there is a lot in such a file that is still correct.
    """
    path, originals = written(count=3)

    # Rebuild the file as the upstream writer would have left it: v1, the old
    # column set, PNG16 depth, and no record of a codec choice.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE old AS
                SELECT idx, timestamp_ms, received_at, depth, color, metadata
                FROM frames;
            DROP TABLE frames;
            ALTER TABLE old RENAME TO frames;
            """
        )
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'format_version'")
        connection.execute("DELETE FROM meta WHERE key = 'codecs'")

    with ArchiveSource(path) as archive:
        assert archive.has_monotonic is False
        restored = list(archive.frames())

    assert len(restored) == 3
    assert np.array_equal(restored[0].depth, originals[0].depth)
    assert restored[0].clock is None
    # Without the column, the best available answer is when the frame arrived.
    assert restored[0].capture_monotonic == pytest.approx(
        originals[0].received_at, abs=1e-9
    )


def test_the_column_is_declared_in_the_meta(written) -> None:
    """A reader should be able to ask what a file carries, not just try it."""
    path, _ = written()
    with ArchiveSource(path) as archive:
        assert "capture_monotonic" in archive.meta["extensions"]


def test_written_files_declare_the_current_version(written) -> None:
    """Each version changed what an existing structure means.

    ``capture_monotonic`` was an added column and left the version alone, on
    the grounds that an older reader could ignore it. v2 and v3 are different:
    colour moved to three columns, depth may be zlib rather than PNG, and the
    per-frame ``motion`` table became ``imu`` at the sensor's own rate. A reader
    of an older version would misread or silently miss those, so it refuses.
    """
    path, _ = written()
    with ArchiveSource(path) as archive:
        assert archive.meta["format_version"] == 3


def test_the_file_is_readable_as_plain_sql(written) -> None:
    """No SDK, no library: the container is SQLite and stays inspectable."""
    path, originals = written(count=4)

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT idx, timestamp_ms, capture_monotonic FROM frames ORDER BY idx"
        ).fetchall()

    assert len(rows) == 4
    for (idx, timestamp_ms, capture_monotonic), original in zip(
        rows, originals, strict=True
    ):
        assert idx == original.index
        assert timestamp_ms == pytest.approx(original.timestamp_ms)
        # And the two agree with each other through the recorded offset.
        assert timestamp_ms / 1000.0 - capture_monotonic == pytest.approx(
            OFFSET, abs=1e-6
        )
