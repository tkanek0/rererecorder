"""Changing what to record, shared by the command line and the page."""

from __future__ import annotations

import pytest
from rrr.recorder import config


def test_codec_choices_name_each_streams_own_algorithm() -> None:
    codecs = config.with_codecs(
        config.CODECS, {"depth": "compressed", "color": "compressed", "infrared": "raw"}
    )
    # zlib beats PNG16 on depth; see docs/decisions.md 5.
    assert codecs == {"depth": "zlib", "color": "png", "infrared": "raw"}
    assert config.with_codecs(codecs, {"color": "raw"})["depth"] == "zlib"


def test_a_stream_turned_back_on_comes_back_at_its_configured_size() -> None:
    off = config.with_streams(
        config.DEFAULT_STREAMS, {"depth": False, "infrared": False, "motion": False}
    )
    assert (off.depth, off.infrared, off.motion) == (None, False, False)
    on = config.with_streams(off, {"depth": True})
    assert on.depth == config.DEFAULT_STREAMS.depth
    assert on.color == config.DEFAULT_STREAMS.color


@pytest.mark.parametrize(
    "change",
    [
        lambda: config.with_codecs(config.CODECS, {"color": "lossy"}),
        lambda: config.with_codecs(config.CODECS, {"nope": "raw"}),
        lambda: config.with_streams(config.DEFAULT_STREAMS, {"nope": True}),
        # Infrared is the depth sensor's own pair; StreamConfig refuses this.
        lambda: config.with_streams(
            config.DEFAULT_STREAMS, {"depth": False, "infrared": True}
        ),
    ],
)
def test_an_unusable_change_is_refused(change) -> None:
    with pytest.raises(ValueError):
        change()
