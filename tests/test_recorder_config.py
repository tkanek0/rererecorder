"""Translating a "compressed"/"raw" choice into an actual codec name."""

from __future__ import annotations

import pytest

from rrr.recorder.config import codec_for


@pytest.mark.parametrize("stream", ["color", "depth", "infrared"])
def test_raw_is_raw_for_every_stream(stream: str) -> None:
    assert codec_for(stream, "raw") == "raw"


def test_compressed_depth_is_zlib_not_png16() -> None:
    """zlib beats PNG16 on this data - see video.archive.encode_depth_zlib."""
    assert codec_for("depth", "compressed") == "zlib"


@pytest.mark.parametrize("stream", ["color", "infrared"])
def test_compressed_color_and_infrared_are_png(stream: str) -> None:
    assert codec_for(stream, "compressed") == "png"


def test_an_unknown_choice_is_refused() -> None:
    with pytest.raises(ValueError, match="compressed.*raw"):
        codec_for("color", "lossy")


def test_an_unknown_stream_is_refused() -> None:
    with pytest.raises(ValueError, match="nope"):
        codec_for("nope", "compressed")
