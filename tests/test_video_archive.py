"""The archive: what goes in must come out, including when it was taken.

Every codec is checked by decoding and comparing, not by trusting the word
"lossless". See docs/decisions.md 3-5, 8, 12 and 22.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from realsense_adapter import (
    Calibration,
    DeviceInfo,
    FrameSet,
    LiveSource,
    StreamConfig,
    StreamError,
)
from realsense_adapter.types import MotionSample, color_to_bgr, color_to_rgb
from rrr.timeline import ClockPair
from rrr.video import ArchiveSource, ArchiveWriter
from rrr.video.archive import FORMAT_VERSION, join_yuyv, split_yuyv

from .conftest import FPS, HEIGHT, MONO, OFFSET, REAL, WIDTH

COMPRESSED = {"depth": "zlib", "color": "png", "infrared": "png"}
RAW = {"depth": "raw", "color": "raw", "infrared": "raw"}


def _write(
    path: str,
    calibration: Calibration,
    frames: list[FrameSet],
    *,
    color_format: str = "rgb8",
    codecs: dict[str, str] | None = None,
    motion: list[MotionSample] | None = None,
) -> None:
    with ArchiveWriter(
        path,
        calibration=calibration,
        config=StreamConfig(color_format=color_format, infrared=True, motion=True),
        codecs=codecs,
        device=DeviceInfo(
            name="Intel RealSense D455",
            serial="311322302077",
            firmware="5.17.3.10",
            usb_type="3.2",
        ),
        clock_anchor=ClockPair(MONO, REAL),
    ) as writer:
        for frame_set in frames:
            assert writer.append(frame_set, timeout=10.0)
        if motion:
            assert writer.append_motion(motion)
        assert writer.drain()


def _frames(
    make_frames: Callable[..., FrameSet], color_format: str, count: int = 3
) -> list[FrameSet]:
    """Incompressible images, the full 16-bit depth range and an unmeasured band."""
    rng = np.random.default_rng(0)
    frames = []
    for i in range(count):
        depth = rng.integers(0, 65536, (HEIGHT, WIDTH), dtype=np.uint16)
        depth[:40] = 0
        depth[50, 50] = 65535
        color = (
            rng.integers(0, 65536, (HEIGHT, WIDTH), dtype=np.uint16)
            if color_format == "yuyv"
            else rng.integers(0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8)
        )
        infrared = tuple(
            rng.integers(0, 256, (HEIGHT, WIDTH), dtype=np.uint8) for _ in range(2)
        )
        frame_set = make_frames(
            index=i + 1,
            depth=depth,
            color=color,
            color_format=color_format,
            infrared=infrared,
        )
        frames.append(
            FrameSet(
                **{**frame_set.__dict__, "metadata": {"depth": {"frame_counter": i}}}
            )
        )
    return frames


@pytest.mark.parametrize(
    ("color_format", "codecs"),
    [("rgb8", COMPRESSED), ("yuyv", COMPRESSED), ("rgb8", RAW), ("yuyv", RAW)],
)
def test_every_stream_and_its_time_survive_the_file(
    tmp_path: Path,
    calibration: Calibration,
    make_frames: Callable[..., FrameSet],
    color_format: str,
    codecs: dict[str, str],
) -> None:
    path = str(tmp_path / "video.rrdb")
    originals = _frames(make_frames, color_format)
    _write(path, calibration, originals, color_format=color_format, codecs=codecs)

    with ArchiveSource(path) as archive:
        restored = list(archive.frames())
        assert archive.meta["format_version"] == FORMAT_VERSION
        assert archive.meta["codecs"] == codecs
        assert archive.calibration.as_dict() == calibration.as_dict()
        assert archive.device is not None and archive.device.serial == "311322302077"
        assert archive.timestamp_domain == "global_time"
        assert archive.clock_anchor is not None
        assert archive.clock_anchor.offset == pytest.approx(OFFSET, abs=1e-6)
        assert archive.frame_times() == [
            (f.index, pytest.approx(f.received_monotonic, abs=1e-9)) for f in originals
        ]
        assert list(archive.motion_samples()) == []
        assert archive.motion_rate() == {}

    for original, back in zip(originals, restored, strict=True):
        assert back.received_monotonic == pytest.approx(
            original.received_monotonic, abs=1e-9
        )
        expected_ms = (MONO + back.index / FPS + OFFSET) * 1000.0
        assert back.color_timestamp_ms == pytest.approx(expected_ms, abs=1e-3)
        assert back.depth_timestamp_ms == pytest.approx(expected_ms, abs=1e-3)
        assert back.color_format == color_format
        assert np.array_equal(back.depth, original.depth)
        assert np.array_equal(back.color, original.color)
        assert back.infrared is not None and original.infrared is not None
        assert np.array_equal(back.infrared[0], original.infrared[0])
        assert np.array_equal(back.infrared[1], original.infrared[1])
        assert back.metadata == original.metadata

    # No SDK, no library: the container is SQLite and stays inspectable.
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT idx, received_monotonic FROM frames ORDER BY idx"
        ).fetchall()
    assert [row[0] for row in rows] == [f.index for f in originals]


def test_one_stream_can_be_read_alone(
    tmp_path: Path, calibration: Calibration, make_frames: Callable[..., FrameSet]
) -> None:
    path = str(tmp_path / "video.rrdb")
    originals = _frames(make_frames, "rgb8", count=2)
    _write(path, calibration, originals)

    with ArchiveSource(path) as archive:
        depth_only = archive.frame_at(2, only="depth")
        assert archive.frame_at(99) is None
        assert archive.bounds() == (
            1,
            2,
            pytest.approx(originals[0].received_monotonic),
            pytest.approx(originals[1].received_monotonic),
        )
    assert depth_only is not None
    assert np.array_equal(depth_only.depth, originals[1].depth)
    assert depth_only.color is None and depth_only.infrared is None


def test_every_inertial_sample_survives_and_is_placed_on_the_clock(
    tmp_path: Path, calibration: Calibration, make_frames: Callable[..., FrameSet]
) -> None:
    """480 Hz in, 480 Hz out, on the audio's axis. See docs/decisions.md 12."""
    start_ms = (REAL + 1.0) * 1000.0
    samples = [
        MotionSample(stream, start_ms + n * 1000.0 / rate, 0.01 * n, -9.63, -0.91)
        for n in range(500)
        for stream, rate in (("accel", 482.0), ("gyro", 478.0))
    ]
    path = str(tmp_path / "video.rrdb")
    _write(path, calibration, _frames(make_frames, "rgb8", count=1), motion=samples)

    with ArchiveSource(path) as archive:
        restored = list(archive.motion_samples())
        rates = archive.motion_rate()

    assert len(restored) == 1000
    accel = [s for s in restored if s.stream == "accel"]
    assert accel[7].x == pytest.approx(0.07) and accel[7].y == pytest.approx(-9.63)
    assert accel[0].capture_monotonic == pytest.approx(
        accel[0].timestamp_ms / 1000.0 - OFFSET, abs=1e-6
    )
    assert rates == {
        "accel": pytest.approx(482.0, rel=0.01),
        "gyro": pytest.approx(478.0, rel=0.01),
    }


@pytest.mark.parametrize("stream", ["depth", "color", "infrared"])
def test_an_unknown_codec_is_refused(
    tmp_path: Path, calibration: Calibration, stream: str
) -> None:
    with pytest.raises(ValueError, match=f"{stream} codec"):
        ArchiveWriter(
            str(tmp_path / "x.rrdb"),
            calibration=calibration,
            config=StreamConfig(),
            codecs={stream: "jpeg"},
        )


def test_a_file_it_cannot_read_is_refused_rather_than_guessed(
    tmp_path: Path, calibration: Calibration, make_frames: Callable[..., FrameSet]
) -> None:
    """Another format version, or a shapeless blob with no calibration to shape it."""
    path = str(tmp_path / "video.rrdb")
    _write(path, calibration, _frames(make_frames, "rgb8", count=1))

    with sqlite3.connect(path) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT value FROM meta WHERE key = 'calibration'"
            ).fetchone()[0]
        )
        raw["depth"] = None
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'calibration'", (json.dumps(raw),)
        )
    with ArchiveSource(path) as archive, pytest.raises(StreamError, match="shape"):
        next(archive.frames())

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'format_version'",
            (str(FORMAT_VERSION - 1),),
        )
    with pytest.raises(StreamError, match="format version"):
        ArchiveSource(path).open()


def test_the_yuyv_split_separates_luma_from_chroma_and_is_inverse() -> None:
    # Y=1, U=2 and Y=3, V=4 in successive pixels.
    y, u, v = split_yuyv(np.array([[0x0201, 0x0403]], dtype=np.uint16))
    assert (y.tolist(), u.tolist(), v.tolist()) == ([[1, 3]], [[2]], [[4]])

    color = np.random.default_rng(0).integers(0, 65536, (48, 424), dtype=np.uint16)
    assert np.array_equal(join_yuyv(*split_yuyv(color)), color)


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


def test_startup_discards_are_counted_apart_from_losses() -> None:
    """A duplicate before the first delivery is the syncer settling, not a fault."""
    source = LiveSource(StreamConfig())
    source._count_skip()
    assert (source.skipped_warmup, source.skipped_duplicate) == (1, 0)
    source._index = 1
    source._count_skip()
    assert (source.skipped_warmup, source.skipped_duplicate) == (1, 1)
