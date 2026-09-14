"""Building what to record from the CLI's own flags, without a device."""

from __future__ import annotations

import argparse

import pytest

from rrr.recorder import config
from rrr.tools.record import _build_codecs, _build_streams


def _args(**overrides: object) -> argparse.Namespace:
    """Defaults matching what argparse produces when nothing is asked for."""
    base: dict[str, object] = {
        "no_video": False,
        "no_color": False,
        "no_depth": False,
        "no_infrared": False,
        "color_codec": None,
        "depth_codec": None,
        "infrared_codec": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_no_flags_keep_the_configured_defaults() -> None:
    assert _build_streams(_args()) is config.DEFAULT_STREAMS


def test_no_color_turns_off_just_the_colour_stream() -> None:
    streams = _build_streams(_args(no_color=True))
    assert streams.color is None
    assert streams.depth == config.DEFAULT_STREAMS.depth


def test_no_depth_and_no_infrared_together_is_the_color_only_profile() -> None:
    streams = _build_streams(_args(no_depth=True, no_infrared=True))
    assert streams.depth is None
    assert streams.infrared is False
    assert streams.color == config.DEFAULT_STREAMS.color


def test_no_depth_alone_is_refused_when_infrared_would_be_left_dangling() -> None:
    """Infrared is the depth sensor's own pair; StreamConfig itself refuses this."""
    if not config.DEFAULT_STREAMS.infrared:
        pytest.skip("infrared is off by configuration in this environment")
    with pytest.raises(ValueError, match="infrared"):
        _build_streams(_args(no_depth=True))


def test_an_invalid_combination_is_skipped_when_video_is_off_entirely() -> None:
    """A configuration nothing will use should not fail a recording without it."""
    streams = _build_streams(_args(no_video=True, no_color=True, no_depth=True))
    assert streams is config.DEFAULT_STREAMS


def test_codecs_default_to_the_configured_values() -> None:
    assert _build_codecs(_args()) == config.CODECS


def test_a_codec_flag_overrides_just_its_own_stream() -> None:
    codecs = _build_codecs(_args(color_codec="raw"))
    assert codecs["color"] == "raw"
    assert codecs["depth"] == config.CODECS["depth"]


def test_compressed_maps_to_each_streams_own_default_algorithm() -> None:
    codecs = _build_codecs(
        _args(
            color_codec="compressed",
            depth_codec="compressed",
            infrared_codec="compressed",
        )
    )
    assert codecs == {"color": "png", "depth": "zlib", "infrared": "png"}
