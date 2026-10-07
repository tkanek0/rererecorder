"""The one sidecar a person writes.

A lost mark is undetectable downstream, unlike a dropped frame, so these test
that marks survive.
"""

from __future__ import annotations

import json

import pytest
from rrr.inspection.checks import Check, _check_events
from rrr.timeline import Event, JsonlWriter, SessionPaths, read_events


def test_marks_survive_in_order_each_on_disk_before_the_next(tmp_path) -> None:
    """Flushed on every write: a crash must not cost the marks already made."""
    path = str(tmp_path / "new" / "events.jsonl")
    written = [
        Event.now("speaker 45deg 2m", {"azimuth_deg": 45, "distance_m": 2.0}),
        *(Event.now(f"run {index}") for index in range(3)),
    ]
    with JsonlWriter(path) as writer:
        for event in written:
            writer.append(event.as_dict())
            with open(path, encoding="utf-8") as handle:
                assert json.loads(handle.readlines()[-1])["label"] == event.label
        assert writer.count == 4

    assert read_events(path) == written
    assert written[1].monotonic > 0 and written[1].realtime > 1_700_000_000
    # No file at all is ordinary, not a failure.
    assert read_events(str(tmp_path / "absent.jsonl")) == []


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
        '{"monotonic": 1.0, "realtime": 2.0, "label": "a", "data": [1]}\n\n'
        + line
        + "\n"
    )
    with pytest.raises(ValueError, match="line 3"):
        read_events(str(path))

    # Blank lines are skipped, and a malformed extra does not cost the label.
    path.write_text(path.read_text().splitlines()[0] + "\n\n")
    [only] = read_events(str(path))
    assert (only.label, only.data) == ("a", {})


# -- what inspect makes of them ----------------------------------------------


def _session(tmp_path, times: list[float]) -> SessionPaths:
    paths = SessionPaths.create(str(tmp_path), "s")
    with JsonlWriter(paths.events) as writer:
        for index, monotonic in enumerate(times):
            writer.append(
                Event(
                    monotonic=monotonic, realtime=1e9 + monotonic, label=f"m{index}"
                ).as_dict()
            )
    return paths


def test_inspect_fails_a_mark_neither_track_spans(tmp_path) -> None:
    """A mark outside the recording means the two do not belong together; the
    video may run past the audio, and a mark there is still inside."""
    audio = {"first_monotonic": 100.0, "last_monotonic": 130.0}
    video = {"first_monotonic": 100.0, "last_monotonic": 150.0}

    check = Check()
    result = _check_events(
        _session(tmp_path / "a", [105.0, 140.0]), audio, video, check
    )
    assert result is not None and result["marks"] == 2
    assert check.problems == []

    check = Check()
    _check_events(_session(tmp_path / "b", [105.0, 900.0]), audio, video, check)
    assert any("outside the recording" in problem for problem in check.problems)

    # Nobody marked anything: nothing to say.
    assert (
        _check_events(SessionPaths.create(str(tmp_path), "c"), audio, None, Check())
        is None
    )
