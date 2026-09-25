"""The manifest, and the one check standing between an HTTP path and the disk."""

from __future__ import annotations

import json
import os

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


def _manifest(session_id: str = "2026-09-01_17-30-00".replace(":", "-")) -> SessionManifest:
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
        doa_file="doa.jsonl",
    )


# -- round trips --------------------------------------------------------------


def test_manifest_round_trips_through_json() -> None:
    original = _manifest()
    rebuilt = SessionManifest.from_dict(json.loads(json.dumps(original.as_dict())))

    assert rebuilt.session_id == original.session_id
    assert rebuilt.started_at == original.started_at
    assert rebuilt.stopped_at == original.stopped_at
    assert [pair.offset for pair in rebuilt.clock_samples] == [
        pair.offset for pair in original.clock_samples
    ]
    assert rebuilt.video == original.video
    assert rebuilt.audio == original.audio
    assert rebuilt.calibration == original.calibration


def test_duration_comes_from_the_monotonic_anchors() -> None:
    assert _manifest().duration_s == pytest.approx(20.0)


def test_duration_is_none_while_recording() -> None:
    manifest = _manifest()
    manifest.stopped_at = None
    assert manifest.duration_s is None


def test_clock_track_can_look_up_the_offset_in_force() -> None:
    """The samples have to be usable as a track, not just stored."""
    manifest = _manifest()
    track = manifest.clock_track
    assert track.at(MONO + 15.0) is not None
    assert track.at(MONO + 15.0).offset == pytest.approx(REAL - MONO, abs=1e-6)


def test_a_newer_format_is_refused_rather_than_guessed() -> None:
    raw = _manifest().as_dict()
    raw["format_version"] = FORMAT_VERSION + 1
    with pytest.raises(SessionError, match="newer"):
        SessionManifest.from_dict(raw)


def test_audio_seconds_uses_the_files_own_header() -> None:
    track = AudioTrack(rate=16_000, samples=320_000)
    assert track.seconds == pytest.approx(20.0)


def test_audio_seconds_is_zero_rather_than_dividing_by_zero() -> None:
    assert AudioTrack().seconds == 0.0


# -- calibration --------------------------------------------------------------


def test_an_unmeasured_calibration_says_so() -> None:
    calibration = SyncCalibration()
    assert calibration.measured is False
    assert calibration.offset_s is None


def test_calibration_can_be_added_long_after_the_recording() -> None:
    """A measurement must not require rewriting the session's tracks."""
    original = _manifest()
    measured = SyncCalibration(
        offset_s=-0.042, uncertainty_s=0.005, method="handclap", measured_at=REAL
    )
    updated = original.with_calibration(measured)

    assert updated.calibration.measured is True
    assert updated.video == original.video
    assert original.calibration.measured is False, "the original is untouched"


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


@pytest.mark.parametrize(
    "good", ["2026-09-01_17-30-00", "session1", "A", "a-b_c-123"]
)
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


def test_paths_name_every_part_of_a_session(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    assert paths.manifest.endswith("session1/session.json")
    assert paths.video.endswith("session1/video.rrdb")
    assert paths.audio.endswith("session1/audio.wav")
    assert paths.audio_clock.endswith("session1/audio.clock.jsonl")
    assert paths.doa.endswith("session1/doa.jsonl")


# -- on disk ------------------------------------------------------------------


def test_write_then_read(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    original = _manifest("session1")
    write_manifest(paths, original)

    rebuilt = read_manifest(paths)
    assert rebuilt.video == original.video
    assert rebuilt.audio == original.audio


def test_write_leaves_no_temporary_behind(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    write_manifest(paths, _manifest("session1"))
    assert sorted(p.name for p in tmp_path.joinpath("session1").iterdir()) == [
        "session.json"
    ]


def test_a_rewrite_replaces_rather_than_appends(tmp_path) -> None:
    """The recorder rewrites the manifest while recording, so this happens often."""
    paths = SessionPaths.create(str(tmp_path), "session1")
    manifest = _manifest("session1")
    write_manifest(paths, manifest)
    manifest.video = VideoTrack(frames=1200)
    write_manifest(paths, manifest)

    assert read_manifest(paths).video.frames == 1200


def test_read_reports_a_missing_manifest(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    with pytest.raises(SessionError, match="no manifest"):
        read_manifest(paths)


def test_read_reports_an_unreadable_manifest(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    open(paths.manifest, "w", encoding="utf-8").write("{not json")
    with pytest.raises(SessionError, match="unreadable"):
        read_manifest(paths)


def test_size_counts_the_files_in_the_session(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    open(paths.audio, "wb").write(b"x" * 100)
    open(paths.video, "wb").write(b"y" * 250)
    assert paths.size_bytes() == 350


def test_size_counts_what_was_derived_inside_the_session(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "session1")
    open(paths.video, "wb").write(b"y" * 250)
    os.makedirs(os.path.join(paths.export, "color", "data"))
    open(os.path.join(paths.export, "color", "data", "0.png"), "wb").write(b"z" * 40)
    open(paths.review, "wb").write(b"m" * 10)
    assert paths.size_bytes() == 300


# -- listing ------------------------------------------------------------------


def test_listing_is_newest_first(tmp_path) -> None:
    for name, offset in (("older", 0.0), ("newer", 100.0), ("newest", 200.0)):
        paths = SessionPaths.create(str(tmp_path), name)
        manifest = _manifest(name)
        manifest.started_at = ClockPair(MONO + offset, REAL + offset)
        write_manifest(paths, manifest)

    assert [m.session_id for m in listing(str(tmp_path))] == [
        "newest",
        "newer",
        "older",
    ]


def test_listing_skips_a_session_without_a_readable_manifest(tmp_path) -> None:
    """A crashed session has no manifest, and must not break the listing."""
    SessionPaths.create(str(tmp_path), "crashed")
    good = SessionPaths.create(str(tmp_path), "good")
    write_manifest(good, _manifest("good"))

    assert [m.session_id for m in listing(str(tmp_path))] == ["good"]


def test_listing_of_a_missing_root_is_empty(tmp_path) -> None:
    assert listing(str(tmp_path / "nothing")) == []


def test_a_manifest_from_before_mispairing_was_retired_reads_back_as_undated() -> None:
    """Old ``skipped`` and ``skipped_unpaired`` counted a reason recording no
    longer has: a set no longer gets discarded for its streams disagreeing
    about the moment (decision 21), so neither old field means anything a
    current recording can produce, and neither is read back.
    """
    track = VideoTrack.from_dict(
        {"frames": 290, "dropped": 0, "skipped": 45, "skipped_unpaired": 45}
    )
    assert track.skipped == 0
    assert track.skipped_duplicate == 0
    assert track.skipped_warmup == 0


def test_skipped_duplicate_is_read_over_a_stale_total() -> None:
    track = VideoTrack.from_dict(
        {
            "frames": 300,
            "skipped": 99,  # stale total, must not win
            "skipped_duplicate": 1,
            "skipped_warmup": 3,
        }
    )
    assert track.skipped == 1
    assert track.skipped_warmup == 3


# -- the rig ------------------------------------------------------------------
#
# The mounting is expected to be filled in by hand, in the file, long after the
# recording. That only works if the manifest declares the field: an unknown key
# would be dropped the first time anything rewrote the session - which
# `calibrate --apply` does.

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


def test_a_new_session_says_its_rig_is_unset() -> None:
    manifest = _manifest()
    assert manifest.rig.source == "unset"
    assert not manifest.rig.known
    assert manifest.as_dict()["rig"]["source"] == "unset"


def test_a_rig_edited_into_the_file_survives_a_rewrite(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "s")
    write_manifest(paths, _manifest("s"))
    on_disk = tmp_path / "s" / "session.json"

    stored = json.loads(on_disk.read_text())
    stored["rig"] = NOMINAL.as_dict()
    on_disk.write_text(json.dumps(stored))

    edited = read_manifest(paths)
    assert edited.rig == NOMINAL

    write_manifest(paths, edited.with_calibration(SyncCalibration(offset_s=0.08)))
    assert read_manifest(paths).rig == NOMINAL


def test_with_rig_leaves_the_recording_alone() -> None:
    manifest = _manifest()
    updated = manifest.with_rig(NOMINAL)
    assert updated.rig == NOMINAL
    assert updated.video == manifest.video
    assert updated.clock_samples == manifest.clock_samples
    assert manifest.rig.source == "unset"


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


def test_a_manifest_from_before_the_rig_reads_as_unset() -> None:
    raw = _manifest().as_dict()
    del raw["rig"]

    assert SessionManifest.from_dict(raw).rig == Rig()
