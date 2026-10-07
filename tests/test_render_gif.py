from __future__ import annotations

import os
from pathlib import Path

import av
import numpy as np
import pytest
from render_gif import DEFAULT_OUTPUT, main, render
from rrr.timeline import SessionPaths
from rrr.visualization import PAD, PLAYHEAD, VOLUME_HEIGHT, WAVEFORM_HEIGHT

from .conftest import SMALL_FRAMES as FRAMES

WIDTH = 32
HEIGHT = 24


def _frames(path: Path) -> list[np.ndarray]:
    with av.open(str(path)) as container:
        return [frame.to_ndarray(format="bgr24") for frame in container.decode(video=0)]


def test_plain_gif_holds_every_strided_frame(small_session, tmp_path: Path) -> None:
    output = tmp_path / "plain.gif"
    report = render(small_session.directory, output, stride=1, width=WIDTH)

    frames = _frames(output)
    assert len(frames) == report.frames == FRAMES
    assert frames[0].shape == (HEIGHT, WIDTH, 3)
    assert report.audio_channel is None
    assert (
        render(
            small_session.directory, tmp_path / "half.gif", stride=2, width=WIDTH
        ).frames
        == 2
    )


def test_each_strip_adds_its_own_height(small_session, tmp_path: Path) -> None:
    heights = {}
    for name, volume, waveform in [
        ("volume", True, False),
        ("waveform", False, True),
        ("both", True, True),
    ]:
        output = tmp_path / f"{name}.gif"
        render(
            small_session.directory,
            output,
            volume=volume,
            waveform=waveform,
            width=WIDTH,
        )
        heights[name] = _frames(output)[0].shape[0]

    assert heights["volume"] == HEIGHT + 2 * PAD + VOLUME_HEIGHT
    assert heights["waveform"] == HEIGHT + 2 * PAD + WAVEFORM_HEIGHT
    assert heights["both"] == HEIGHT + 3 * PAD + VOLUME_HEIGHT + WAVEFORM_HEIGHT


def test_playhead_follows_the_recorded_clock(small_session, tmp_path: Path) -> None:
    output = tmp_path / "volume.gif"
    render(small_session.directory, output, volume=True, stride=1, width=WIDTH)

    heads = []
    for frame in _frames(output):
        row = frame[HEIGHT + PAD + VOLUME_HEIGHT // 2].astype(int)
        distance = np.abs(row - np.array(PLAYHEAD)).sum(axis=1)
        heads.append(int(np.argmin(distance)))
    # The fixture's audio spans exactly the four frames, so the playhead
    # starts at the left edge and advances a quarter of the strip per frame.
    assert heads[0] <= 1
    assert heads == sorted(heads)
    assert heads[-1] == pytest.approx(0.75 * (WIDTH - 1), abs=2)


def test_a_strip_without_audio_is_refused(small_session, tmp_path: Path) -> None:
    os.remove(small_session.audio)
    with pytest.raises(ValueError, match="WAV"):
        render(small_session.directory, tmp_path / "volume.gif", volume=True)
    render(small_session.directory, tmp_path / "plain.gif")


def test_existing_gif_is_not_replaced(small_session, tmp_path: Path) -> None:
    output = tmp_path / "video.gif"
    output.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        render(small_session.directory, output)
    assert output.read_bytes() == b"keep"

    render(small_session.directory, output, overwrite=True)
    assert len(_frames(output)) == 1
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_the_command_line_writes_to_the_working_directory_by_default(
    small_session: SessionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main([small_session.directory, "--volume", "--stride", "1"]) == 0

    assert len(_frames(tmp_path / DEFAULT_OUTPUT)) == FRAMES
