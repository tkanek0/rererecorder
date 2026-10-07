"""The manifest, and the one check standing between an HTTP path and the disk."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from rrr.timeline.clock import ClockPair
from rrr.timeline.session import (
    FORMAT_VERSION,
    AudioTrack,
    Rig,
    SessionError,
    SessionManifest,
    SessionPaths,
    SyncCalibration,
    VideoTrack,
    listing,
    read_manifest,
    write_manifest,
)

MONO = 1_322_228.023434
REAL = 1_788_250_182.059000


def _manifest(
    session_id: str = "2026-09-01_17-30-00".replace(":", "-"),
) -> SessionManifest:
    """A manifest with both tracks and a start and stop anchor."""
    return SessionManifest(
        session_id=session_id,
        started_at=ClockPair(MONO, REAL),
        stopped_at=ClockPair(MONO + 20.0, REAL + 20.0),
        clock_samples=[
            ClockPair(MONO, REAL),
            ClockPair(MONO + 10.0, REAL + 10.0),
            ClockPair(MONO + 20.0, REAL + 20.0),
        ],
        video=VideoTrack(
            frames=600,
            dropped=0,
            skipped_duplicate=1,
            first_monotonic=MONO + 0.1,
            last_monotonic=MONO + 20.0,
            timestamp_domain="global_time",
            fps=29.9,
        ),
        audio=AudioTrack(
            rate=16_000,
            channels=6,
            samples=320_000,
            filled=0,
            overruns=0,
            first_monotonic=MONO + 0.05,
            timeline={"points": 312, "rate_error_ppm": -3.2},
        ),
        doa=True,
    )


# -- the manifest -------------------------------------------------------------


def test_manifest_round_trips_through_the_file_and_rewrites_in_place(
    tmp_path: Path,
) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    manifest = _manifest("session1")
    write_manifest(paths, manifest)
    rebuilt = read_manifest(paths)

    assert rebuilt == manifest
    assert rebuilt.rig.source == "unset" and not rebuilt.rig.known
    assert rebuilt.calibration.measured is False
    assert rebuilt.calibration.offset_s is None
    assert rebuilt.duration_s == pytest.approx(20.0)
    assert rebuilt.audio is not None
    assert rebuilt.audio.seconds == pytest.approx(20.0)

    # The recorder rewrites the manifest every second while recording.
    manifest.video = VideoTrack(frames=1200)
    manifest.stopped_at = None
    write_manifest(paths, manifest)
    assert read_manifest(paths).video == VideoTrack(frames=1200)
    assert read_manifest(paths).duration_s is None
    assert sorted(p.name for p in (tmp_path / "session1").iterdir()) == ["session.json"]


def test_a_manifest_it_cannot_read_is_reported(tmp_path: Path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    with pytest.raises(SessionError, match="no manifest"):
        read_manifest(paths)

    Path(paths.manifest).write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionError, match="unreadable"):
        read_manifest(paths)

    raw = _manifest().as_dict()
    raw["format_version"] = FORMAT_VERSION - 1
    with pytest.raises(SessionError, match="format"):
        SessionManifest.from_dict(raw)


def test_audio_seconds_is_zero_rather_than_dividing_by_zero() -> None:
    assert AudioTrack().seconds == 0.0


# -- names, and what they are not ---------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "..",
        "../escape",
        "a/b",
        "a\\b",
        "/absolute",
        ".hidden",
        "has space",
        "has.dot",
        "-leading-dash",
        "_leading-underscore",
        "trailing/",
    ],
)
def test_unusable_session_ids_are_refused(tmp_path, bad: str) -> None:
    """A session id reaches the filesystem from a URL, so it is rejected."""
    with pytest.raises(SessionError):
        SessionPaths.create(str(tmp_path), bad)
    with pytest.raises(SessionError):
        SessionPaths.resolve(str(tmp_path), bad)


@pytest.mark.parametrize("good", ["2026-09-01_17-30-00", "session1", "A", "a-b_c-123"])
def test_usable_session_ids_are_accepted(tmp_path, good: str) -> None:
    paths = SessionPaths.create(str(tmp_path), good)
    assert paths.session_id == good


def test_create_refuses_to_clobber_an_existing_session(tmp_path) -> None:
    """A recording cannot be repeated, so overwriting one is not recoverable."""
    SessionPaths.create(str(tmp_path), "session1")
    with pytest.raises(SessionError, match="already exists"):
        SessionPaths.create(str(tmp_path), "session1")


def test_resolve_refuses_a_session_that_does_not_exist(tmp_path) -> None:
    with pytest.raises(SessionError, match="no session"):
        SessionPaths.resolve(str(tmp_path), "nothing")


def test_size_counts_every_file_in_the_session_including_derived_ones(
    tmp_path: Path,
) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    Path(paths.audio).write_bytes(b"x" * 100)
    Path(paths.video).write_bytes(b"y" * 250)
    os.makedirs(os.path.join(paths.export, "color", "data"))
    (Path(paths.export) / "color" / "data" / "0.png").write_bytes(b"z" * 40)
    Path(paths.review).write_bytes(b"m" * 10)
    assert paths.size_bytes() == 400


# -- listing ------------------------------------------------------------------


def test_listing_is_newest_first_and_skips_what_it_cannot_read(
    tmp_path: Path,
) -> None:
    """A crashed session has no manifest, and must not break the listing."""
    for name, offset in (("older", 0.0), ("newer", 100.0), ("newest", 200.0)):
        paths = SessionPaths.create(str(tmp_path), name)
        manifest = _manifest(name)
        manifest.started_at = ClockPair(MONO + offset, REAL + offset)
        write_manifest(paths, manifest)
    SessionPaths.create(str(tmp_path), "crashed")

    assert [m.session_id for m in listing(str(tmp_path))] == [
        "newest",
        "newer",
        "older",
    ]
    assert listing(str(tmp_path / "nothing")) == []


# -- the rig ------------------------------------------------------------------
#
# Filled in by hand after recording, so the manifest must declare the field or
# a rewrite (e.g. `calibrate --apply`) would drop it.

NOMINAL = Rig(
    source="nominal",
    rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    translation=(0.0, -0.05, 0.0),
    microphones=(
        (0.0463, 0.0, 0.0),
        (0.0, 0.0463, 0.0),
        (-0.0463, 0.0, 0.0),
        (0.0, -0.0463, 0.0),
    ),
    channels=(1, 2, 3, 4),
    description="array on top of the camera, axes aligned",
)


def test_a_rig_edited_into_the_file_survives_a_rewrite(tmp_path: Path) -> None:
    paths = SessionPaths.create(str(tmp_path), "s")
    write_manifest(paths, _manifest("s"))
    on_disk = tmp_path / "s" / "session.json"

    stored = json.loads(on_disk.read_text())
    stored["rig"] = NOMINAL.as_dict()
    on_disk.write_text(json.dumps(stored))

    edited = read_manifest(paths)
    assert edited.rig == NOMINAL

    edited.calibration = SyncCalibration(offset_s=0.08)
    write_manifest(paths, edited)
    assert read_manifest(paths).rig == NOMINAL


@pytest.mark.parametrize(
    "field, value",
    [
        ("rotation", [1.0, 0.0, 0.0]),
        ("rotation", ["a"] * 9),
        ("translation", [0.0, 0.0]),
        ("microphones", [[0.0, 0.0, 0.0], [0.0, 0.0]]),
        ("channels", "1234"),
    ],
)
def test_a_malformed_rig_field_reads_as_unset(field, value) -> None:
    """A typo in a hand-edited mounting must not cost the whole session."""
    raw = _manifest().as_dict()
    raw["rig"] = NOMINAL.as_dict() | {field: value}

    rig = SessionManifest.from_dict(raw).rig
    assert getattr(rig, field) is None
    assert rig.description == NOMINAL.description


def test_a_partly_filled_rig_is_not_known() -> None:
    """Half a mounting places nothing: a consumer must not assume the rest."""
    raw = _manifest().as_dict()
    raw["rig"] = NOMINAL.as_dict() | {"translation": None}

    assert not SessionManifest.from_dict(raw).rig.known
