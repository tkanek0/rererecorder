"""The control plane's HTTP surface.

The MJPEG preview is untested: it never ends, and TestClient buffers a body in
full, so a test would hang. Nothing here acquires the hub, so no camera opens.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from rrr.api import app as api_app
from rrr.api import devices as api_devices
from rrr.api import state as api_state
from rrr.recorder import config as recording_config
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
        self.session_id: str | None = None
        self.marks: list[Event] = []
        self.tap = None
        self.doa = None

    def state(self) -> dict[str, object]:
        return {
            "recording": self.recording,
            "session_id": self.session_id,
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
        self.recording = False

    def close(self) -> None:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """A client whose server writes into a temporary directory."""
    root = str(tmp_path / "sessions")
    os.makedirs(root)
    monkeypatch.setattr(api_app.state, "recorder", FakeRecorder(root))
    return TestClient(api_app.app)


def _recording(session_id: str | None = None) -> None:
    recorder = api_app.state.recorder
    recorder.recording = True
    recorder.session_id = session_id


def _write_session(name: str, offset: float = 0.0) -> SessionPaths:
    """A session directory with a manifest and a stand-in archive."""
    paths = SessionPaths.create(api_app.state.recorder.root, name)
    write_manifest(
        paths,
        SessionManifest(
            session_id=name,
            started_at=ClockPair(1_000.0 + offset, 1_788_000_000.0 + offset),
            stopped_at=ClockPair(1_030.0 + offset, 1_788_000_030.0 + offset),
        ),
    )
    with open(paths.video, "wb") as handle:
        handle.write(b"x" * 2048)
    return paths


# -- status and devices -------------------------------------------------------


def test_status_carries_what_the_page_polls_for_without_opening_anything(
    client,
) -> None:
    body = client.get("/api/status", headers={"Origin": "http://localhost:5177"}).json()
    assert set(body) == {"recording", "storage", "devices"}
    assert body["devices"]["realsense"]["streams"]["align_to_color"] is False
    assert body["devices"]["respeaker"]["recording"] is False
    # The rate is measured, so there is none (and no time left) until recording.
    assert body["storage"]["free_bytes"] > 0
    assert body["storage"]["seconds_left"] is None


def test_the_array_reads_as_recorded_only_while_a_session_records_it(client) -> None:
    """The level meter opens the array as well; that is not a recording."""
    from types import SimpleNamespace

    recorder = api_app.state.recorder
    recorder.tap = SimpleNamespace(active=True, failed=False, error=None, overruns=0)

    def respeaker() -> dict:
        return client.get("/api/status").json()["devices"]["respeaker"]

    assert respeaker()["recording"] is False
    _recording()
    assert respeaker()["recording"] is True


def test_frame_headers_are_readable_cross_origin(client) -> None:
    """The page is served from another port; unlisted headers stay hidden."""
    response = client.get("/api/status", headers={"Origin": "http://localhost:5177"})
    exposed = response.headers["access-control-expose-headers"]
    assert "X-Frame-Index" in exposed and "X-Received-Monotonic" in exposed


def test_the_realsense_enumeration_is_kept_until_a_reconnect(
    client, monkeypatch
) -> None:
    """Each enumeration holds the GIL for 0.1-0.2 s, so not one per poll."""
    enumerations: list[int] = []
    reconnects: list[int] = []
    rescans: list[int] = []
    monkeypatch.setattr(api_state, "list_devices", lambda: enumerations.append(1) or [])
    monkeypatch.setattr(api_app.state, "realsense_found", None)
    monkeypatch.setattr(api_app.state, "realsense_found_after", 0.0)
    monkeypatch.setattr(api_app.state.hub, "reconnect", lambda: reconnects.append(1))
    monkeypatch.setattr(api_devices, "rescan", lambda: rescans.append(1))

    for _ in range(3):
        client.get("/api/status")
    assert len(enumerations) == 1

    assert client.post("/api/devices/realsense/reconnect").status_code == 200
    assert (len(enumerations), reconnects) == (2, [1])
    assert client.post("/api/devices/respeaker/reconnect").status_code == 200
    assert rescans == [1]


# -- settings ----------------------------------------------------------------


def test_settings_change_what_the_next_recording_does(client, tmp_path) -> None:
    recorder = api_app.state.recorder
    assert client.get("/api/settings").json()["writable"] is True

    wanted = str(tmp_path / "elsewhere")
    body = client.put("/api/settings", json={"sessions_dir": wanted}).json()
    assert body["sessions_dir"] == wanted
    assert os.path.isdir(wanted), "created rather than refused"
    assert recorder.root == wanted, "the recorder follows"

    body = client.put(
        "/api/settings",
        json={"streams": {"depth": False, "infrared": False, "motion": False}},
    ).json()
    assert body["streams"]["depth"] is None
    assert recorder.streams.motion is False

    body = client.put("/api/settings", json={"codecs": {"color": "raw"}}).json()
    assert body["codecs"]["color"] == "raw"
    assert body["codecs"]["depth"] != "raw", "an untouched stream keeps its codec"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"sessions_dir": "   "},
        # Checked by writing: only a write finds read-only and foreign mounts.
        {"sessions_dir": "/proc/nope"},
        {"streams": {"nope": True}},
        # Infrared is the depth sensor's own pair; it needs depth enabled.
        {"streams": {"depth": False, "infrared": True}},
        {"codecs": {"color": "lossy"}},
    ],
)
def test_an_unusable_setting_is_refused(client, body) -> None:
    assert client.put("/api/settings", json=body).status_code == 400


# -- refused while recording --------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/api/devices/realsense/reconnect", None),
        # Half a session on each disk would be described by neither manifest.
        ("PUT", "/api/settings", {"sessions_dir": "/tmp"}),
        ("PUT", "/api/settings", {"streams": {"depth": False}}),
        ("PUT", "/api/settings", {"codecs": {"color": "raw"}}),
        # Removing the file being written is not a recoverable mistake.
        ("DELETE", "/api/sessions/live", None),
    ],
)
def test_changes_are_refused_while_recording(client, method, url, body) -> None:
    _write_session("live")
    _recording("live")
    assert client.request(method, url, json=body).status_code == 409
    assert os.path.isdir(os.path.join(api_app.state.recorder.root, "live"))


# -- sessions ----------------------------------------------------------------


def test_sessions_are_listed_newest_first_and_described(client) -> None:
    _write_session("older")
    _write_session("newer", offset=100.0)

    body = client.get("/api/sessions").json()
    assert [entry["session_id"] for entry in body["sessions"]] == ["newer", "older"]

    # A session whose archive is broken still has a manifest worth showing.
    detail = client.get("/api/sessions/older").json()
    assert detail["session_id"] == "older"
    assert detail["size_bytes"] > 0
    assert "error" in detail["archive"], "said so rather than raising"


def test_a_session_can_be_deleted(client) -> None:
    """Offered because a session costs 1.7 GB for 34 seconds."""
    paths = _write_session("unwanted")
    body = client.request("DELETE", "/api/sessions/unwanted").json()

    assert body["deleted"] == "unwanted"
    assert body["freed_bytes"] > 0
    assert not os.path.exists(paths.directory)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/api/sessions/nothing"),
        ("DELETE", "/api/sessions/nothing"),
        ("GET", "/api/sessions/nothing/frame/1.jpg"),
        ("POST", "/api/devices/nope/reconnect"),
        # Checked before the never-ending stream opens, so this is testable.
        ("GET", "/stream/nope.mjpg"),
    ],
)
def test_what_does_not_exist_is_a_404(client, method, url) -> None:
    assert client.request(method, url).status_code == 404


def test_a_session_id_cannot_escape_the_recordings_root(client, tmp_path) -> None:
    """The one check between a path parameter and the filesystem."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for attempt in ("..", "../outside", "..%2Foutside", "a/b"):
        assert client.get(f"/api/sessions/{attempt}").status_code in (404, 307), attempt
    assert outside.exists(), "and nothing outside was touched"


# -- marks --------------------------------------------------------------------


def test_a_mark_is_written_and_counted_while_recording(client) -> None:
    response = client.post("/api/events", json={"label": "clap"})
    assert response.status_code == 400
    assert "nothing is recording" in response.json()["detail"]

    _recording()
    body = client.post(
        "/api/events", json={"label": "speaker 45deg 2m", "data": {"azimuth_deg": 45}}
    ).json()
    assert body["event"]["label"] == "speaker 45deg 2m"
    assert body["event"]["data"] == {"azimuth_deg": 45}
    assert body["marks"] == 1


@pytest.mark.parametrize(
    "body",
    [{"label": ""}, {"label": "   "}, {"label": None}, {"label": "x", "data": [1, 2]}],
)
def test_a_malformed_mark_is_refused(client, body) -> None:
    _recording()
    assert client.post("/api/events", json=body).status_code == 400
