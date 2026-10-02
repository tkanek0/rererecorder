"""The v2 archive: raw infrared, YUYV colour, and zlib depth.

All three codecs are checked by decoding and comparing, not by trusting the
word "lossless". See docs/decisions.md 3-5.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from rrr.video import ArchiveSource, ArchiveWriter, Calibration, FrameSet, StreamConfig
from rrr.video.types import color_to_bgr, color_to_rgb, join_yuyv, split_yuyv

#: The camera's real maxima, and the sizes every measurement in this repository
#: was taken at.
DEPTH_W, DEPTH_H = 1280, 720
COLOR_W, COLOR_H = 1280, 800


@pytest.fixture
def native(intrinsics) -> Calibration:
    """Calibration for an unaligned recording at the sensors' own sizes."""
    from rrr.video import Extrinsics, Intrinsics

    depth = Intrinsics(
        width=DEPTH_W, height=DEPTH_H, fx=653.36, fy=653.36,
        ppx=640.09, ppy=358.66, model="brown_conrady", coeffs=(0.0,) * 5,
    )
    color = Intrinsics(
        width=COLOR_W, height=COLOR_H, fx=643.59, fy=643.59,
        ppx=654.56, ppy=409.25, model="brown_conrady", coeffs=(0.0,) * 5,
    )
    return Calibration(
        color=color,
        depth=depth,
        depth_scale=0.001,
        # Not identity, and that is the point of not aligning: the two cameras
        # are 59 mm apart and the transform is what puts them together later.
        depth_to_color=Extrinsics(
            rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            translation=(0.059, 0.0, 0.0),
        ),
        aligned=False,
    )


@pytest.fixture
def written_native(
    tmp_path: Path, native: Calibration, make_frames: Callable[..., FrameSet]
):
    """Write frame sets holding every stream, and close the archive."""

    def build(count: int = 4, seed: int = 0, **writer_kwargs):
        rng = np.random.default_rng(seed)
        originals = []
        for i in range(count):
            depth = np.concatenate(
                [
                    np.zeros((180, DEPTH_W), np.uint16),
                    rng.integers(0, 65536, (DEPTH_H - 180, DEPTH_W), dtype=np.uint16),
                ]
            )
            depth[10, 10] = 65535
            originals.append(
                make_frames(
                    index=i + 1,
                    depth=depth,
                    color=rng.integers(
                        0, 65536, (COLOR_H, COLOR_W), dtype=np.uint16
                    ),
                    color_format="yuyv",
                    infrared=(
                        rng.integers(0, 256, (DEPTH_H, DEPTH_W), dtype=np.uint8),
                        rng.integers(0, 256, (DEPTH_H, DEPTH_W), dtype=np.uint8),
                    ),
                )
            )

        path = str(tmp_path / "video.rrdb")
        with ArchiveWriter(
            path,
            calibration=native,
            config=StreamConfig(
                depth=(DEPTH_W, DEPTH_H, 30),
                color=(COLOR_W, COLOR_H, 30),
                color_format="yuyv",
                infrared=True,
            ),
            **writer_kwargs,
        ) as writer:
            for frames in originals:
                assert writer.append(frames, timeout=30.0)
            assert writer.drain()
        return path, originals

    return build


# -- the streams survive -------------------------------------------------------


def test_infrared_survives_exactly(written_native) -> None:
    """The pair is the measurement the depth was computed from."""
    path, originals = written_native()

    with ArchiveSource(path) as archive:
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert restored.infrared is not None
            assert np.array_equal(restored.infrared[0], original.infrared[0])
            assert np.array_equal(restored.infrared[1], original.infrared[1])


def test_yuyv_colour_survives_exactly(written_native) -> None:
    """Byte for byte, including the chroma the split had to interleave back."""
    path, originals = written_native()

    with ArchiveSource(path) as archive:
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert restored.color_format == "yuyv"
            assert np.array_equal(restored.color, original.color)


def test_zlib_depth_survives_exactly(written_native) -> None:
    path, originals = written_native()

    with ArchiveSource(path) as archive:
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert np.array_equal(restored.depth, original.depth)


def test_the_time_axis_still_works(written_native) -> None:
    """Everything else changed; this must not have."""
    path, originals = written_native()

    with ArchiveSource(path) as archive:
        for original, restored in zip(originals, archive.frames(), strict=True):
            assert restored.received_monotonic == pytest.approx(
                original.received_monotonic, abs=1e-9
            )


# -- what the file says about itself ------------------------------------------


def test_the_codecs_are_recorded(written_native) -> None:
    """A zlib depth blob is values and nothing else; the reader must be told."""
    path, _ = written_native()
    with ArchiveSource(path) as archive:
        assert archive.meta["codecs"]["depth"] == "zlib"
        assert archive.meta["color_format"] == "yuyv"


def test_the_recording_says_it_is_not_aligned(written_native) -> None:
    """Alignment cannot be undone, so a consumer has to know it was not applied."""
    path, _ = written_native()
    with ArchiveSource(path) as archive:
        assert archive.calibration.aligned is False
        # And it carries what is needed to align later.
        assert archive.calibration.depth_to_color is not None
        assert archive.calibration.depth_to_color.translation[0] == pytest.approx(0.059)


def test_depth_can_be_png16_instead(written_native) -> None:
    """The choice exists for anyone who wants a file image tools can open."""
    path, originals = written_native(codecs={"depth": "png16"})
    with ArchiveSource(path) as archive:
        assert archive.meta["codecs"]["depth"] == "png16"
        restored = next(archive.frames())
    assert np.array_equal(restored.depth, originals[0].depth)


def test_an_unknown_depth_codec_is_refused(tmp_path, native) -> None:
    with pytest.raises(ValueError, match="depth codec"):
        ArchiveWriter(
            str(tmp_path / "x.rrdb"),
            calibration=native,
            config=StreamConfig(),
            codecs={"depth": "jpeg"},
        )


def test_zlib_depth_without_a_shape_is_refused(written_native, tmp_path) -> None:
    """A zlib blob has no dimensions; without calibration it is refused, not guessed."""
    path, _ = written_native(count=1)
    import json

    with sqlite3.connect(path) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT value FROM meta WHERE key = 'calibration'"
            ).fetchone()[0]
        )
        raw["depth"] = None
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'calibration'",
            (json.dumps(raw),),
        )

    from rrr.video import StreamError

    with ArchiveSource(path) as archive:
        with pytest.raises(StreamError, match="shape is unknown"):
            next(archive.frames())


# -- the split itself ---------------------------------------------------------


@pytest.mark.parametrize("width", [1280, 640, 424])
def test_yuyv_split_and_join_are_inverse(width: int) -> None:
    rng = np.random.default_rng(20260902)
    color = rng.integers(0, 65536, (480, width), dtype=np.uint16)
    assert np.array_equal(join_yuyv(*split_yuyv(color)), color)


def test_the_split_separates_luma_from_chroma() -> None:
    """The planes must be the right planes, which a round trip alone cannot see."""
    # Y=1, U=2 and Y=3, V=4 in successive pixels.
    packed = np.array([[0x0201, 0x0403]], dtype=np.uint16)
    y, u, v = split_yuyv(packed)
    assert y.tolist() == [[1, 3]]
    assert u.tolist() == [[2]]
    assert v.tolist() == [[4]]


def test_colour_conversions_agree_across_recorded_formats(
    make_frames: Callable[..., FrameSet],
) -> None:
    """YUYV and RGB recordings of the same grey come out the same, in either order."""
    # Y=128 with neutral chroma: grey 130, since YUYV is BT.601 video range.
    yuyv = np.full((2, 4), 0x8080, dtype=np.uint16)
    rgb = np.full((2, 4, 3), 130, dtype=np.uint8)
    from_yuyv = make_frames(color=yuyv, color_format="yuyv")
    from_rgb = make_frames(color=rgb)

    assert color_to_rgb(from_rgb) is rgb
    for convert in (color_to_bgr, color_to_rgb):
        a, b = convert(from_yuyv), convert(from_rgb)
        assert a is not None and b is not None
        assert a.shape == (2, 4, 3) and np.abs(a.astype(int) - b).max() <= 1
    assert color_to_bgr(make_frames(color=None)) is None
    assert color_to_rgb(make_frames(color=None)) is None


# -- what counts as a lost frame ----------------------------------------------


def test_startup_discards_are_counted_apart_from_losses() -> None:
    """A duplicate before the first delivery is the syncer settling, not a fault.

    See docs/frame-loss.md, "What is still discarded, on purpose".
    """
    from rrr.video import LiveSource

    source = LiveSource(StreamConfig())

    # Before anything has been delivered: the pipeline is still settling.
    source._count_skip("duplicate")
    assert source.skipped_warmup == 1
    assert source.skipped == 0, "startup must not read as a mid-stream loss"

    # Once a set has been delivered, the same discard means something else.
    source._index = 1
    source._count_skip("duplicate")
    assert source.skipped_warmup == 1, "unchanged"
    assert source.skipped == 1
    assert source.skipped_duplicate == 1


# -- the inertial sensor at its own rate --------------------------------------


def _imu_burst(count: int, *, start_ms: float = 1_788_000_000_000.0) -> list:
    """Samples at the rates a D455 actually produces: 482 and 478 Hz."""
    from rrr.video.types import MotionSample

    samples = []
    for n in range(count):
        samples.append(
            MotionSample("accel", start_ms + n * 1000.0 / 482.0, 0.01 * n, -9.63, -0.91)
        )
        samples.append(
            MotionSample("gyro", start_ms + n * 1000.0 / 478.0, 0.001, 0.002, -0.003)
        )
    return samples


def test_every_inertial_sample_survives(tmp_path, native) -> None:
    """480 Hz in, 480 Hz out. See docs/decisions.md 12."""
    path = str(tmp_path / "video.rrdb")
    original = _imu_burst(500)

    with ArchiveWriter(
        path, calibration=native, config=StreamConfig(motion=True)
    ) as writer:
        # In batches, as a recorder drains once per frame.
        for start in range(0, len(original), 32):
            assert writer.append_motion(original[start : start + 32])
        assert writer.drain()
        assert writer.stats.motion == 1000

    with ArchiveSource(path) as archive:
        restored = list(archive.motion_samples())

    assert len(restored) == 1000
    by_stream = {"accel": [], "gyro": []}
    for sample in restored:
        by_stream[sample.stream].append(sample)
    assert len(by_stream["accel"]) == 500
    assert len(by_stream["gyro"]) == 500
    assert by_stream["accel"][7].y == pytest.approx(-9.63)
    assert by_stream["accel"][7].x == pytest.approx(0.07)


def test_the_measured_rate_is_reported(tmp_path, native) -> None:
    """Measured from the timestamps, so a one-per-frame recording shows 30 Hz."""
    path = str(tmp_path / "video.rrdb")
    with ArchiveWriter(
        path, calibration=native, config=StreamConfig(motion=True)
    ) as writer:
        assert writer.append_motion(_imu_burst(500))
        assert writer.drain()

    with ArchiveSource(path) as archive:
        rates = archive.motion_rate()

    assert rates["accel"] == pytest.approx(482.0, rel=0.01)
    assert rates["gyro"] == pytest.approx(478.0, rel=0.01)


def test_samples_carry_the_clock_so_they_can_be_placed(tmp_path, native, make_frames) -> None:
    """An inertial sample is only useful if it lands on the common axis."""
    from rrr.timeline import ClockPair

    from .conftest import MONO, OFFSET, REAL

    path = str(tmp_path / "video.rrdb")
    with ArchiveWriter(
        path,
        calibration=native,
        config=StreamConfig(motion=True),
        # Fixed, rather than read from the host, so OFFSET applies.
        clock_anchor=ClockPair(MONO, REAL),
    ) as writer:
        assert writer.append(
            make_frames(index=1, depth=np.zeros((DEPTH_H, DEPTH_W), np.uint16)),
            timeout=10.0,
        )
        assert writer.append_motion(_imu_burst(10))
        assert writer.drain()

    with ArchiveSource(path) as archive:
        sample = next(archive.motion_samples())

    assert sample.capture_monotonic is not None
    assert sample.capture_monotonic == pytest.approx(
        sample.timestamp_ms / 1000.0 - OFFSET, abs=1e-6
    )


def test_a_v2_recording_still_yields_its_samples(tmp_path, native, make_frames) -> None:
    """v2's one-per-frame samples read through the same call, in the same shape."""
    path = str(tmp_path / "video.rrdb")
    with ArchiveWriter(
        path, calibration=native, config=StreamConfig(motion=True)
    ) as writer:
        assert writer.append(
            make_frames(index=1, depth=np.zeros((DEPTH_H, DEPTH_W), np.uint16)),
            timeout=10.0,
        )
        assert writer.drain()

    # Rebuild the file as v2 left it: a per-frame motion table, no imu.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE imu;
            CREATE TABLE motion(
                idx INTEGER PRIMARY KEY, timestamp_ms REAL NOT NULL,
                ax REAL, ay REAL, az REAL, gx REAL, gy REAL, gz REAL);
            INSERT INTO motion VALUES(1, 1788000000000.0, 0.0, -9.65, -0.9,
                                      0.001, 0.002, 0.003);
            INSERT INTO motion VALUES(2, 1788000000033.4, 0.1, -9.66, -0.9,
                                      0.001, 0.002, 0.003);
            """
        )
        connection.execute("UPDATE meta SET value = '2' WHERE key = 'format_version'")

    with ArchiveSource(path) as archive:
        restored = list(archive.motion_samples())
        rates = archive.motion_rate()

    assert [s.stream for s in restored] == ["accel", "gyro", "accel", "gyro"]
    assert restored[0].y == pytest.approx(-9.65)
    assert restored[1].z == pytest.approx(0.003)
    # 33.4 ms apart - the video frame interval, which is the give-away.
    assert rates["both"] == pytest.approx(29.94, rel=0.01)


def test_an_archive_without_inertial_data_says_so(written_native) -> None:
    path, _ = written_native()
    with ArchiveSource(path) as archive:
        assert list(archive.motion_samples()) == []
        assert archive.motion_rate() == {}
