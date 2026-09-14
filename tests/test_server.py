"""The control plane's HTTP surface.

The preview is not tested here and cannot be: Starlette's TestClient buffers a
response body in full, and an MJPEG response never ends - it runs until the
client disconnects. A test that requested one would hang rather than fail,
which is worse than not having it. What is tested is everything that changes
state, because those are the calls that can lose a recording.

The camera is never opened: nothing here acquires the hub, and the hub opens the
device only when something does.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from rrr.recorder import config as recording_config
from rrr.server import app as server_app
from rrr.timeline import (
    ClockPair,
    Event,
    SessionManifest,
    SessionPaths,
    write_manifest,
)


class FakeRecorder:
    """A SessionRecorder-shaped stand-in whose recording flag is settable."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.streams = recording_config.DEFAULT_STREAMS
        self.codecs = dict(recording_config.CODECS)
        self.recording = False
        self.stopped = 0
        self.marks: list[Event] = []

    def state(self) -> dict[str, object]:
        return {
            "recording": self.recording,
            "session_id": None,
            "directory": None,
            "seconds": 0.0,
            "size_bytes": 0,
            "video": None,
            "audio": None,
            "marks": len(self.marks),
            "errors": [],
        }

    def mark(self, label: str, data: dict[str, object] | None = None) -> Event:
        if not self.recording:
            raise RuntimeError("nothing is recording, so there is nothing to mark")
        event = Event.now(label, data)
        self.marks.append(event)
        return event

    def stop(self) -> None:
        self.stopped += 1
        self.recording = False

    def close(self) -> None:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """A client whose server writes into a temporary directory."""
    root = str(tmp_path / "sessions")
    os.makedirs(root)
    monkeypatch.setattr(server_app.state, "sessions_root", root)
    monkeypatch.setattr(server_app.state, "recorder", FakeRecorder(root))
    return TestClient(server_app.app)


# -- status ------------------------------------------------------------------


def test_health_reports_the_camera_without_opening_it(client) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["camera"]["active"] is False


def test_status_carries_what_the_page_polls_for(client) -> None:
    body = client.get("/api/status").json()
    assert set(body) == {"camera", "recording", "storage"}
    assert body["camera"]["streams"]["infrared"] is True
    assert body["camera"]["streams"]["align_to_color"] is False


def test_storage_reports_free_space_and_no_rate_when_idle(client) -> None:
    """The rate is measured, so there is none until something is recording.

    Reporting a guess would make the remaining time a guess too, and remaining
    time is the number this panel exists for.
    """
    storage = client.get("/api/status").json()["storage"]
    assert storage["free_bytes"] > 0
    assert storage["write_bytes_per_s"] is None
    assert storage["seconds_left"] is None


# -- settings ----------------------------------------------------------------


def test_settings_describe_what_can_change(client) -> None:
    body = client.get("/api/settings").json()
    assert body["writable"] is True
    # Read-only: resolution is settled when the pipeline starts.
    assert "streams" in body


def test_the_directory_can_be_moved(client, tmp_path) -> None:
    wanted = str(tmp_path / "elsewhere")
    body = client.put("/api/settings", json={"sessions_dir": wanted}).json()

    assert body["sessions_dir"] == wanted
    assert os.path.isdir(wanted), "created rather than refused"
    assert server_app.state.recorder.root == wanted, "the recorder follows"


def test_moving_the_directory_is_refused_while_recording(client, tmp_path) -> None:
    """Half a session on each disk would be described by neither manifest."""
    server_app.state.recorder.recording = True
    response = client.put(
        "/api/settings", json={"sessions_dir": str(tmp_path / "elsewhere")}
    )
    assert response.status_code == 409
    assert "recording" in response.json()["detail"]


def test_an_unwritable_directory_is_refused(client) -> None:
    """Checked by writing, not by reading permissions.

    A mount can be read-only, full, or owned by somebody else, and only an
    actual write finds all three.
    """
    response = client.put("/api/settings", json={"sessions_dir": "/proc/nope"})
    assert response.status_code == 400


def test_an_empty_directory_is_refused(client) -> None:
    response = client.put("/api/settings", json={"sessions_dir": "   "})
    assert response.status_code == 400


def test_streams_can_be_narrowed_to_color_only(client) -> None:
    body = client.put(
        "/api/settings",
        json={"streams": {"depth": False, "infrared": False}},
    ).json()

    assert body["streams"]["depth"] is None
    assert body["streams"]["infrared"] is False
    assert server_app.state.recorder.streams.depth is None


def test_turning_off_depth_alone_is_refused_when_infrared_is_still_on(client) -> None:
    """Infrared is the depth sensor's own pair; it needs depth enabled."""
    if not recording_config.DEFAULT_STREAMS.infrared:
        pytest.skip("infrared is off by configuration in this environment")
    response = client.put("/api/settings", json={"streams": {"depth": False}})
    assert response.status_code == 400
    assert "infrared" in response.json()["detail"]


def test_an_unknown_stream_setting_is_refused(client) -> None:
    response = client.put("/api/settings", json={"streams": {"nope": True}})
    assert response.status_code == 400


def test_streams_cannot_change_while_recording(client) -> None:
    server_app.state.recorder.recording = True
    response = client.put("/api/settings", json={"streams": {"depth": False}})
    assert response.status_code == 409


def test_codecs_choose_between_compressed_and_raw(client) -> None:
    body = client.put("/api/settings", json={"codecs": {"color": "raw"}}).json()

    assert body["codecs"]["color"] == "raw"
    assert body["codecs"]["depth"] != "raw", "an untouched stream keeps its codec"


def test_an_unknown_codec_choice_is_refused(client) -> None:
    response = client.put("/api/settings", json={"codecs": {"color": "lossy"}})
    assert response.status_code == 400


def test_codecs_cannot_change_while_recording(client) -> None:
    server_app.state.recorder.recording = True
    response = client.put("/api/settings", json={"codecs": {"color": "raw"}})
    assert response.status_code == 409


def test_an_empty_settings_body_is_refused(client) -> None:
    response = client.put("/api/settings", json={})
    assert response.status_code == 400


# -- sessions ----------------------------------------------------------------


def test_sessions_are_listed_newest_first(client, tmp_path) -> None:
    root = server_app.state.sessions_root
    for name, offset in (("older", 0.0), ("newer", 100.0)):
        paths = SessionPaths.create(root, name)
        write_manifest(
            paths,
            SessionManifest(
                session_id=name,
                started_at=ClockPair(1_000.0 + offset, 1_788_000_000.0 + offset),
            ),
        )

    body = client.get("/api/sessions").json()
    assert [entry["session_id"] for entry in body["sessions"]] == ["newer", "older"]


def test_an_unknown_stream_is_a_404(client) -> None:
    """Testable because the name is checked before the stream is opened.

    A valid name cannot be tested the same way - the response never ends, so
    TestClient would buffer it forever - which is the reason the check happens
    up front rather than inside the generator.
    """
    response = client.get("/stream/nope.mjpg")
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


# -- one session in detail, and deleting it ----------------------------------


def _write_session(root: str, name: str) -> SessionPaths:
    """A session directory with a manifest and a stand-in archive."""
    paths = SessionPaths.create(root, name)
    write_manifest(
        paths,
        SessionManifest(
            session_id=name,
            started_at=ClockPair(1_000.0, 1_788_000_000.0),
            stopped_at=ClockPair(1_030.0, 1_788_000_030.0),
        ),
    )
    with open(paths.video, "wb") as handle:
        handle.write(b"x" * 2048)
    return paths


def test_detail_reports_an_unreadable_archive_rather_than_failing(client) -> None:
    """A session whose archive is broken still has a manifest worth showing."""
    _write_session(server_app.state.sessions_root, "broken")
    body = client.get("/api/sessions/broken").json()

    assert body["session_id"] == "broken"
    assert body["size_bytes"] > 0
    assert "error" in body["archive"], "said so rather than raising"


def test_detail_of_a_missing_session_is_a_404(client) -> None:
    assert client.get("/api/sessions/nothing").status_code == 404


def test_a_session_id_cannot_escape_the_recordings_root(client, tmp_path) -> None:
    """The one check between a path parameter and the filesystem."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for attempt in ("..", "../outside", "..%2Foutside", "a/b"):
        assert client.get(f"/api/sessions/{attempt}").status_code in (404, 307), attempt
    assert outside.exists(), "and nothing outside was touched"


def test_a_session_can_be_deleted(client) -> None:
    """Offered because a session costs 1.7 GB for 34 seconds."""
    paths = _write_session(server_app.state.sessions_root, "unwanted")
    body = client.request("DELETE", "/api/sessions/unwanted").json()

    assert body["deleted"] == "unwanted"
    assert body["freed_bytes"] > 0
    assert not os.path.exists(paths.directory)


def test_deleting_the_running_session_is_refused(client) -> None:
    """Removing the file being written is not a recoverable mistake."""
    _write_session(server_app.state.sessions_root, "live")
    recorder = server_app.state.recorder
    recorder.recording = True
    recorder.state = lambda: {  # type: ignore[method-assign]
        "recording": True,
        "session_id": "live",
        "directory": None,
        "seconds": 1.0,
        "size_bytes": 1,
        "video": None,
        "audio": None,
        "errors": [],
    }

    response = client.request("DELETE", "/api/sessions/live")
    assert response.status_code == 409
    assert os.path.isdir(
        os.path.join(server_app.state.sessions_root, "live")
    ), "still there"


def test_deleting_a_missing_session_is_a_404(client) -> None:
    assert client.request("DELETE", "/api/sessions/nothing").status_code == 404


# -- marks --------------------------------------------------------------------


def test_a_mark_needs_a_running_recording(client) -> None:
    response = client.post("/api/events", json={"label": "clap"})
    assert response.status_code == 400
    assert "nothing is recording" in response.json()["detail"]


def test_a_mark_is_written_and_counted(client) -> None:
    server_app.state.recorder.recording = True

    response = client.post(
        "/api/events",
        json={"label": "speaker 45deg 2m", "data": {"azimuth_deg": 45}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event"]["label"] == "speaker 45deg 2m"
    assert body["event"]["data"] == {"azimuth_deg": 45}
    assert body["marks"] == 1


@pytest.mark.parametrize("label", ["", "   ", None])
def test_a_mark_without_a_label_is_refused(client, label) -> None:
    server_app.state.recorder.recording = True
    response = client.post("/api/events", json={"label": label})
    assert response.status_code == 400
    assert "label" in response.json()["detail"]


def test_mark_data_has_to_be_an_object(client) -> None:
    server_app.state.recorder.recording = True
    response = client.post("/api/events", json={"label": "x", "data": [1, 2]})
    assert response.status_code == 400


def test_a_session_detail_carries_its_marks(client, tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path / "sessions"), "marked")
    write_manifest(paths, SessionManifest(session_id="marked"))
    with open(paths.events, "w", encoding="utf-8") as handle:
        handle.write(
            '{"monotonic": 1.0, "realtime": 2.0, "label": "clap", "data": {}}\n'
        )

    body = client.get("/api/sessions/marked").json()
    assert [event["label"] for event in body["events"]] == ["clap"]


def test_a_session_without_marks_reports_none(client, tmp_path) -> None:
    paths = SessionPaths.create(str(tmp_path / "sessions"), "plain")
    write_manifest(paths, SessionManifest(session_id="plain"))

    assert client.get("/api/sessions/plain").json()["events"] == []


def test_an_unreadable_mark_does_not_break_the_session(client, tmp_path) -> None:
    """One bad line must not make a whole recording unopenable."""
    paths = SessionPaths.create(str(tmp_path / "sessions"), "broken")
    write_manifest(paths, SessionManifest(session_id="broken"))
    with open(paths.events, "w", encoding="utf-8") as handle:
        handle.write("not json\n")

    response = client.get("/api/sessions/broken")
    assert response.status_code == 200
    assert response.json()["events"] == []
