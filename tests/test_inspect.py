"""What inspect makes of a recording's video track."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

import inspect_session as inspect_cli
import numpy as np
from realsense_adapter import Calibration, FrameSet, StreamConfig
from rrr.inspection import inspect_session
from rrr.inspection.checks import Check, _check_video, count_missing
from rrr.timeline import SessionManifest, SessionPaths, VideoTrack, read_manifest
from rrr.video import ArchiveWriter

from .conftest import HEIGHT, WIDTH
from .conftest import SMALL_FRAMES as FRAMES


def test_a_gapless_counter_misses_nothing() -> None:
    assert count_missing([5, 6, 7, 8]) == {
        "span": 4,
        "delivered": 4,
        "missing": 0,
        "restarts": 0,
    }


def test_a_gap_in_the_counter_is_a_lost_frame() -> None:
    assert count_missing([5, 6, 9, 10])["missing"] == 2


def test_a_repeated_frame_is_not_a_loss() -> None:
    """The syncer may pair a frame twice (decision 21); that is not a gap."""
    assert count_missing([5, 6, 6, 7])["missing"] == 0


def test_a_counter_that_restarts_is_counted_per_run() -> None:
    """A restarted stream numbers from zero again; each run is checked alone."""
    count = count_missing([5, 6, 8, 0, 1, 3])
    assert count["restarts"] == 1
    assert count["missing"] == 2


def _session(
    tmp_path,
    make_frames: Callable[..., FrameSet],
    calibration: Calibration,
    counters: list[int] | None,
) -> tuple[SessionPaths, SessionManifest]:
    """A short depth-only session whose frames carry the given counters."""
    paths = SessionPaths.create(str(tmp_path), "s")
    depth = np.zeros((HEIGHT, WIDTH), dtype=np.uint16)
    with ArchiveWriter(
        paths.video, calibration=calibration, config=StreamConfig(color=None)
    ) as writer:
        for n in range(1, 6):
            frames = make_frames(depth=depth, index=n)
            if counters is not None:
                frames = replace(
                    frames, metadata={"depth": {"frame_counter": counters[n - 1]}}
                )
            writer.append(frames)
    return paths, SessionManifest(session_id="s", video=VideoTrack(frames=5))


def test_frames_the_camera_numbered_but_never_delivered_fail_the_check(
    tmp_path, make_frames, calibration
) -> None:
    """Even with every interval looking normal, a counter gap is a loss."""
    paths, manifest = _session(tmp_path, make_frames, calibration, [10, 11, 14, 15, 16])
    check = Check()

    video = _check_video(paths, manifest, check)

    assert video["missing"]["depth"]["missing"] == 2
    assert any("never reached the recorder" in p for p in check.problems)


def test_without_counters_losses_are_named_as_uncounted(
    tmp_path, make_frames, calibration
) -> None:
    """No counter is not the same as no loss, so it is said, not passed."""
    paths, manifest = _session(tmp_path, make_frames, calibration, None)
    check = Check()

    video = _check_video(paths, manifest, check)

    assert video["missing"] == {}
    assert check.problems == []
    assert any("not counted" in note for note in check.notes)


def test_the_script_reports_a_whole_session_as_json(small_session, capsys) -> None:
    """The thin command line runs every check and prints what the package found."""
    status = inspect_cli.main([small_session.directory, "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["session_id"] == small_session.session_id
    assert report["video"]["frames"] == FRAMES
    assert status == (1 if report["problems"] else 0)


def test_a_session_the_recorder_flagged_does_not_pass(small_session) -> None:
    """What the recorder knew went wrong must not read as "every check agreed"."""
    manifest = read_manifest(small_session)
    assert inspect_session(small_session, manifest).doa["readings"] > 0

    open(small_session.doa, "w").close()
    flagged = replace(manifest, errors=["video: no frames within 15s"])
    problems = inspect_session(small_session, flagged).problems
    assert "the recorder reported: video: no frames within 15s" in problems
    assert "the direction was recorded and holds no readings" in problems

    empty = SessionManifest(session_id=manifest.session_id)
    assert (
        "neither device was recorded" in inspect_session(small_session, empty).problems
    )
