"""The one sidecar a person writes.

What matters here is not the format - it is one JSON object per line, like the
others - but that a mark survives. A recording of an experiment is unusable if
the note saying which condition it was got lost, and unlike a dropped frame,
nothing downstream can detect that it is missing.
"""

from __future__ import annotations

import json
import os

import pytest

from rrr.timeline import SessionPaths
from rrr.timeline.events import Event, EventWriter, read_events
from rrr.tools.inspect import Check, _check_events


def test_a_mark_stamps_both_clocks() -> None:
    event = Event.now("clap")
    assert event.monotonic > 0
    assert event.realtime > 1_700_000_000
    assert event.label == "clap"
    assert event.data == {}


def test_marks_round_trip(tmp_path) -> None:
    path = str(tmp_path / "events.jsonl")
    written = [
        Event.now("speaker 45deg 2m", {"azimuth_deg": 45, "distance_m": 2.0}),
        Event.now("clap"),
    ]
    with EventWriter(path) as writer:
        for event in written:
            writer.append(event)
        assert writer.count == 2

    assert read_events(path) == written


def test_each_mark_is_on_disk_before_the_next_one(tmp_path) -> None:
    """Flushed on every write: a crash must not cost the marks already made."""
    path = str(tmp_path / "events.jsonl")
    with EventWriter(path) as writer:
        writer.append(Event.now("first"))
        with open(path, encoding="utf-8") as handle:
            assert json.loads(handle.readline())["label"] == "first"


def test_marks_keep_the_order_they_were_made(tmp_path) -> None:
    path = str(tmp_path / "events.jsonl")
    with EventWriter(path) as writer:
        for index in range(5):
            writer.append(Event.now(f"run {index}"))

    assert [event.label for event in read_events(path)] == [
        f"run {index}" for index in range(5)
    ]


def test_a_session_nobody_marked_reads_as_empty(tmp_path) -> None:
    """No file at all is ordinary, not a failure."""
    assert read_events(str(tmp_path / "absent.jsonl")) == []


def test_blank_lines_are_skipped(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"monotonic": 1.0, "realtime": 2.0, "label": "a", "data": {}}\n\n\n'
    )

    assert [event.label for event in read_events(str(path))] == ["a"]


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"realtime": 2.0, "label": "a"}',
        '{"monotonic": 1.0, "label": "a"}',
        '{"monotonic": 1.0, "realtime": 2.0}',
    ],
)
def test_a_malformed_line_is_reported_with_its_number(tmp_path, line) -> None:
    """Written by a program, so a bad line is a bug and worth raising over."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"monotonic": 1.0, "realtime": 2.0, "label": "a", "data": {}}\n' + line + "\n"
    )

    with pytest.raises(ValueError, match="line 2"):
        read_events(str(path))


def test_data_that_is_not_an_object_is_dropped(tmp_path) -> None:
    """The label is the mark; a malformed extra must not cost it."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"monotonic": 1.0, "realtime": 2.0, "label": "a", "data": [1, 2]}\n'
    )

    assert read_events(str(path))[0].data == {}


def test_the_writer_makes_the_directory(tmp_path) -> None:
    path = str(tmp_path / "new" / "events.jsonl")
    with EventWriter(path) as writer:
        writer.append(Event.now("x"))

    assert os.path.exists(path)


# -- what inspect makes of them ----------------------------------------------


def _session(tmp_path, times: list[float]) -> SessionPaths:
    paths = SessionPaths.create(str(tmp_path), "s")
    with EventWriter(paths.events) as writer:
        for index, monotonic in enumerate(times):
            writer.append(
                Event(monotonic=monotonic, realtime=1e9 + monotonic, label=f"m{index}")
            )
    return paths


def test_inspect_counts_marks_inside_the_recording(tmp_path) -> None:
    paths = _session(tmp_path, [105.0, 110.0])
    check = Check()

    result = _check_events(
        paths,
        {"first_monotonic": 100.0, "last_monotonic": 130.0},
        None,
        check,
    )

    assert result["marks"] == 2
    assert check.problems == []


def test_inspect_reports_a_mark_outside_the_recording(tmp_path) -> None:
    """A mark the recording does not span means the two do not belong together."""
    paths = _session(tmp_path, [105.0, 900.0])
    check = Check()

    _check_events(
        paths, {"first_monotonic": 100.0, "last_monotonic": 130.0}, None, check
    )

    assert any("outside the recording" in problem for problem in check.problems)


def test_inspect_spans_both_tracks_before_calling_a_mark_late(tmp_path) -> None:
    """The video may run past the audio; a mark in that stretch is still fine."""
    paths = _session(tmp_path, [140.0])
    check = Check()

    _check_events(
        paths,
        {"first_monotonic": 100.0, "last_monotonic": 130.0},
        {"first_monotonic": 100.0, "last_monotonic": 150.0},
        check,
    )

    assert check.problems == []


def test_inspect_says_nothing_when_nobody_marked(tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path), "s")
    assert _check_events(paths, None, None, Check()) is None
