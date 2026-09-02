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

from server import app as server_app
from timeline import ClockPair, SessionManifest, SessionPaths, write_manifest


class FakeRecorder:
    """A SessionRecorder-shaped stand-in whose recording flag is settable."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.recording = False
        self.stopped = 0

    def state(self) -> dict[str, object]:
        return {
            "recording": self.recording,
            "session_id": None,
            "directory": None,
            "seconds": 0.0,
            "size_bytes": 0,
            "video": None,
            "audio": None,
            "errors": [],
        }

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
