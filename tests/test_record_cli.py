"""Building what to record from the CLI's own flags, without a device."""

from __future__ import annotations

import argparse

import pytest
from record import _build_codecs, _build_streams, _status
from rrr.recorder import config
from rrr.timeline import SessionManifest, VideoTrack


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


def test_flags_change_only_what_they_name() -> None:
    assert _build_streams(_args()) == config.DEFAULT_STREAMS
    assert _build_codecs(_args()) == config.CODECS

    streams = _build_streams(_args(no_depth=True, no_infrared=True))
    assert (streams.depth, streams.infrared) == (None, False)
    assert streams.color == config.DEFAULT_STREAMS.color
    codecs = _build_codecs(_args(color_codec="raw"))
    assert codecs == {**config.CODECS, "color": "raw"}


def test_a_stream_combination_is_not_checked_when_video_is_off() -> None:
    """A configuration nothing will use should not fail a recording without it."""
    streams = _build_streams(_args(no_video=True, no_color=True, no_depth=True))
    assert streams == config.DEFAULT_STREAMS


@pytest.mark.parametrize(
    ("track", "min_fps", "status"),
    [
        (VideoTrack(frames=300, fps=30.0), None, 0),
        (VideoTrack(frames=0), None, 1),
        (VideoTrack(frames=300, dropped=1, fps=30.0), None, 2),
        (VideoTrack(frames=300, fps=30.0), 29.5, 0),
        (VideoTrack(frames=300, fps=20.0), 29.5, 2),
    ],
)
def test_exit_status_reports_nothing_recorded_and_losses(
    track: VideoTrack, min_fps: float | None, status: int
) -> None:
    manifest = SessionManifest(session_id="s", video=track)
    assert _status(manifest, min_fps) == status
