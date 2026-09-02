"""The v2 archive: raw infrared, YUYV colour, and zlib depth.

These are the streams as the sensors produce them, which is the point - a
recording that keeps the infrared pair can have its depth recomputed by a
different stereo matcher years later, and one that keeps YUYV has not had a
colour conversion baked into it. All three codecs are checked by decoding and
comparing rather than by trusting the word "lossless".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from video import ArchiveSource, ArchiveWriter, Calibration, FrameSet, StreamConfig
from video.types import join_yuyv, split_yuyv

#: The camera's real maxima, and the sizes every measurement in this repository
#: was taken at.
DEPTH_W, DEPTH_H = 1280, 720
COLOR_W, COLOR_H = 1280, 800


@pytest.fixture
def native(intrinsics) -> Calibration:
    """Calibration for an unaligned recording at the sensors' own sizes."""
    from video import Extrinsics, Intrinsics

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
            assert restored.capture_monotonic == pytest.approx(
                original.capture_monotonic, abs=1e-9
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
    """A zlib blob carries no dimensions, so the calibration is not optional.

    Rather than reshape to whatever fits and hand back a plausible-looking
    image, this must say it cannot be read.
    """
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

    from video import StreamError

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
    """Not just reversible - the planes have to be the right planes.

    A reversible-but-wrong split would compress badly and mislead anyone
    reading a plane directly, and the round-trip test alone cannot see it.
    """
    # Y=1, U=2 and Y=3, V=4 in successive pixels.
    packed = np.array([[0x0201, 0x0403]], dtype=np.uint16)
    y, u, v = split_yuyv(packed)
    assert y.tolist() == [[1, 3]]
    assert u.tolist() == [[2]]
    assert v.tolist() == [[4]]


# -- what counts as a lost frame ----------------------------------------------


def test_startup_discards_are_counted_apart_from_losses() -> None:
    """The first few sets after pipeline.start are the syncer, not a fault.

    Measured on a D455, three sets are discarded within the same millisecond as
    ``pipeline.start`` - one stale depth frame paired with successive colour
    frames, 129 to 230 ms apart - and then nothing for the rest of the
    recording. Counting those as losses made a recording whose frame counters
    were provably continuous report "skipped 3".

    Reaches into the source's counter directly because the alternative is a
    camera.
    """
    from video import LiveSource

    source = LiveSource(StreamConfig())

    # Before anything has been delivered: the pipeline is still settling.
    source._count_skip("unpaired")
    source._count_skip("duplicate")
    assert source.skipped_warmup == 2
    assert source.skipped == 0, "startup must not read as a mid-stream loss"

    # Once a set has been delivered, the same discard means something else.
    source._index = 1
    source._count_skip("unpaired")
    source._count_skip("duplicate")
    assert source.skipped_warmup == 2, "unchanged"
    assert source.skipped == 2
    assert source.skipped_unpaired == 1
    assert source.skipped_duplicate == 1
