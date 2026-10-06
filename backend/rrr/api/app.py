"""The control plane: preview out, recording commands in.

Transport only; what a recording contains is decided in :mod:`rrr.recorder`
and :mod:`rrr.video`. One hub owns the camera and is shared by the preview and
the recording - see docs/design.md "The camera is shared".
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import threading
import time
import wave
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from realsense_adapter import (
    DeviceInfo,
    LiveSource,
    StreamConfig,
    StreamError,
    list_devices,
)
from respeaker_adapter import AudioTap, dbfs, rescan, rms
from respeaker_adapter import probe as probe_audio

from rrr.recorder import RecorderBusy, SessionRecorder
from rrr.recorder import config as recording_config
from rrr.timeline import (
    SessionError,
    SessionPaths,
    listing,
    read_manifest,
)
from rrr.video import ArchiveSource, FrameHub

from . import config, preview

logger = logging.getLogger(__name__)


class State:
    """Everything the server owns, for the life of the process.

    One hub, one recorder. The recorder is long-lived because it holds the
    audio taps, which must be closed properly (see ``SessionRecorder.close``).
    """

    def __init__(self) -> None:
        self.hub = FrameHub(
            self._open_camera,
            idle_shutdown_s=config.IDLE_SHUTDOWN_S,
        )
        self.recorder = SessionRecorder(
            recording_config.SESSIONS_ROOT,
            streams=recording_config.DEFAULT_STREAMS,
            serial=recording_config.SERIAL,
            record_video=recording_config.RECORD_VIDEO,
            record_audio=recording_config.RECORD_AUDIO,
            record_doa=recording_config.RECORD_DOA,
            codecs=dict(recording_config.CODECS),
            hub=self.hub,
        )
        #: Bytes per second the last recording achieved, so the remaining-time
        #: estimate survives the recording ending.
        self.last_write_rate: float | None = None
        #: RealSense devices as last enumerated, or None before the first time.
        self.realsense_found: list[DeviceInfo] | None = None
        #: The hub failure the enumeration above already reflects, by its
        #: error_at, so that each failure costs one enumeration and no more.
        self.realsense_found_after = 0.0
        self.realsense_lock = threading.Lock()

    def enumerate_realsense(self) -> list[DeviceInfo]:
        """Enumerate RealSense devices now, and keep the result."""
        with self.realsense_lock:
            self.realsense_found = list_devices()
            self.realsense_found_after = self.hub.error_at
            return self.realsense_found

    def known_realsense(self) -> list[DeviceInfo]:
        """The kept enumeration, redone only when it may have gone stale."""
        with self.realsense_lock:
            found = self.realsense_found
            stale = found is None or self.hub.error_at > self.realsense_found_after
        return self.enumerate_realsense() if stale else found

    @property
    def streams(self) -> StreamConfig:
        """What the camera is asked for, read through the recorder."""
        return self.recorder.streams

    @property
    def codecs(self) -> dict[str, str]:
        """How each stream's archive is encoded, read through the recorder."""
        return self.recorder.codecs or {}

    def _open_camera(self) -> LiveSource:
        """Open the camera. Called by the hub, and again after a failure."""
        return LiveSource(self.streams, serial=recording_config.SERIAL)

    def close(self) -> None:
        """Stop everything, in the order that leaves the devices usable."""
        if self.recorder.recording:
            logger.warning("shutting down with a recording running; stopping it")
            self.recorder.stop()
        self.recorder.close()
        self.hub.stop()


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hold the devices for the life of the server."""
    logger.info("server starting on %s:%d", config.HOST, config.PORT)
    yield
    state.close()


app = FastAPI(title="rererecorder", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    # The page is cross-origin; unlisted headers are hidden from the player.
    expose_headers=["X-Frame-Index", "X-Received-Monotonic"],
)


# -- status ------------------------------------------------------------------


@app.get("/api/status")
def status() -> dict[str, Any]:
    """Everything the page polls for: recording, disk and devices."""
    return {
        "recording": state.recorder.state(),
        "storage": _storage(),
        "devices": _devices(),
    }


def _devices() -> dict[str, Any]:
    """Whether each device is plugged in, independent of whether it is in use."""
    return {
        "realsense": _realsense_device(),
        "respeaker": _respeaker_device(),
    }


def _realsense_device() -> dict[str, Any]:
    """Describe the D455 as the SDK currently sees it, streaming or not.

    Enumeration is a slow USB stall, so it is skipped while the hub is active
    and otherwise cached until the next hub failure or Reconnect - see
    docs/decisions.md 29.
    """
    hub = state.hub
    if hub.active:
        device = hub.device
    else:
        try:
            found = state.known_realsense()
        except Exception as error:  # noqa: BLE001 - reported to the page
            return {"connected": False, "failed": hub.failed, "error": str(error)}
        serial = recording_config.SERIAL
        device = next((d for d in found if not serial or d.serial == serial), None)
    return {
        "connected": device is not None,
        "device": device.as_dict() if device else None,
        # What is currently being asked for, shown here rather than in a
        # separate area: it describes this device, not the page in general.
        "streams": state.streams.as_dict(),
        "failed": hub.failed,
        "error": hub.error,
    }


def _respeaker_device() -> dict[str, Any]:
    """Describe the array as PortAudio currently sees it, recording or not.

    ``failed`` covers both the audio and the direction taps.
    """
    found = probe_audio(recording_config.AUDIO_DEVICE)
    tap = state.recorder.tap
    doa = state.recorder.doa
    failures = [t.error for t in (tap, doa) if t is not None and t.failed]
    return {
        "connected": found.connected,
        "name": found.name,
        "host_api": found.host_api,
        "channels": found.channels,
        "rate": found.rate,
        "failed": bool(failures),
        "error": failures[0] if failures else found.error,
        "recording": tap.active if tap else False,
        "overruns": tap.overruns if tap else 0,
    }


@app.post("/api/devices/{name}/reconnect")
def reconnect_device(name: str) -> dict[str, Any]:
    """Try a device again after it failed, or after it was plugged in.

    Args:
        name: ``realsense`` or ``respeaker``.

    Returns:
        The devices, as ``/api/status`` reports them.

    Raises:
        HTTPException: 404 for an unknown device; 409 while recording.

    See docs/decisions.md 29.
    """
    if name not in ("realsense", "respeaker"):
        raise HTTPException(status_code=404, detail=f"no device {name!r}")
    if state.recorder.recording:
        raise HTTPException(
            status_code=409, detail="stop the recording before reconnecting"
        )

    if name == "realsense":
        try:
            state.enumerate_realsense()
        except Exception as error:  # noqa: BLE001 - the hub reports it too
            logger.warning("could not enumerate RealSense devices: %s", error)
        state.hub.reconnect()
    else:
        tap = state.recorder.tap
        doa = state.recorder.doa
        # Re-initialising PortAudio (to see a newly plugged array) would pull
        # an open stream from under its reader, so only with the tap stopped.
        if tap is None or not tap.active:
            rescan()
        for device in (tap, doa):
            if device is not None:
                device.reconnect()
    return _devices()


def _storage() -> dict[str, Any]:
    """Where recordings go, and how much room is left there.

    Returns:
        The directory, its free and total bytes, and how long that lasts at the
        rate this recording is actually writing.
    """
    root = state.recorder.root
    probe = root if os.path.isdir(root) else os.path.dirname(os.path.abspath(root))
    try:
        usage = shutil.disk_usage(probe)
        free, total = usage.free, usage.total
    except OSError as error:
        return {"sessions_dir": root, "error": str(error)}

    live = _write_rate()
    if live:
        state.last_write_rate = live
    # Falls back to the last recording's rate, flagged as not live.
    basis = live or state.last_write_rate
    return {
        "sessions_dir": root,
        "free_bytes": free,
        "total_bytes": total,
        "write_bytes_per_s": live,
        "seconds_left": free / basis if basis else None,
    }


def _write_rate() -> float | None:
    """How fast the current recording is growing, in bytes per second.

    Returns:
        The measured rate, or None when nothing is being recorded. Measured,
        because compression depends on the scene.
    """
    recording = state.recorder.state()
    seconds = recording.get("seconds") or 0.0
    size = recording.get("size_bytes") or 0
    if not recording.get("recording") or seconds < 1.0:
        return None
    return size / seconds


# -- recording ---------------------------------------------------------------


@app.put("/api/recording")
async def set_recording(request: Request) -> dict[str, Any]:
    """Start or stop recording.

    Args:
        request: JSON body with ``recording`` (bool) and optionally
            ``session`` (a directory name; the time is used when absent).

    Returns:
        The recorder's state afterwards.

    Raises:
        HTTPException: 400 for an unusable session name or a busy recorder,
            503 if the camera could not be started.

    Runs in a thread: starting and stopping both block long enough to stall
    the event loop and the preview.
    """
    body = await request.json()
    wanted = bool(body.get("recording"))
    name = body.get("session") or None

    if wanted:
        try:
            paths = await asyncio.to_thread(state.recorder.start, name)
        except RecorderBusy as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except SessionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001 - reported to the page
            logger.exception("could not start recording")
            raise HTTPException(status_code=503, detail=str(error)) from error
        logger.info("recording to %s", paths.directory)
    elif state.recorder.recording:
        await asyncio.to_thread(state.recorder.stop)

    return state.recorder.state()


@app.post("/api/events")
async def add_event(request: Request) -> dict[str, Any]:
    """Mark the running recording.

    Args:
        request: JSON body with ``label`` (a non-empty string) and optionally
            ``data`` (an object of anything else worth keeping).

    Returns:
        The mark as written, and how many the session now holds.

    Raises:
        HTTPException: 400 for an empty label, a ``data`` that is not an
            object, or when nothing is recording.

    Not run in a thread, so the mark is stamped as close to the request as
    possible.
    """
    body = await request.json()
    label = str(body.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="a mark needs a label")
    data = body.get("data")
    if data is not None and not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="data must be an object")

    try:
        event = state.recorder.mark(label, data)
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {"event": event.as_dict(), "marks": state.recorder.state()["marks"]}


@app.get("/api/sessions")
def sessions() -> dict[str, Any]:
    """Every readable session in the current directory, newest first."""
    return {
        "sessions_dir": state.recorder.root,
        "sessions": [manifest.as_dict() for manifest in listing(state.recorder.root)],
    }


def _resolve(session_id: str) -> SessionPaths:
    """Turn a session id from a URL into paths, or a 404.

    Args:
        session_id: What the client asked for.

    Returns:
        The paths.

    Raises:
        HTTPException: 404 if it is not a session id or no such session exists.
            ``SessionPaths.resolve`` is the only guard between a path parameter
            and the filesystem.
    """
    try:
        return SessionPaths.resolve(state.recorder.root, session_id)
    except SessionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str) -> dict[str, Any]:
    """Everything known about one session, enough to play it back.

    Args:
        session_id: Directory name.

    Returns:
        The manifest, plus the archive's frame range and which streams it
        holds.

    Raises:
        HTTPException: 404 if the session or its manifest cannot be read.
    """
    paths = _resolve(session_id)
    try:
        manifest = read_manifest(paths)
    except SessionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    detail = manifest.as_dict()
    detail["size_bytes"] = paths.size_bytes()
    detail["archive"] = _archive_detail(paths.video)
    return detail


def _archive_detail(path: str) -> dict[str, Any]:
    """Describe an archive's frames without decoding any of them.

    Args:
        path: The archive to open.

    Returns:
        Its frame range, count and available streams, or an ``error`` rather
        than raising, so the manifest can still be shown.
    """
    if not os.path.isfile(path):
        return {"error": "no archive in this session"}
    try:
        with ArchiveSource(path) as archive:
            bounds = archive.bounds()
            config = archive.meta.get("config") or {}
            return {
                "frames": len(archive),
                "first_index": bounds[0] if bounds else None,
                "last_index": bounds[1] if bounds else None,
                "first_monotonic": bounds[2] if bounds else None,
                "last_monotonic": bounds[3] if bounds else None,
                "streams": {
                    "color": config.get("color") is not None,
                    "depth": config.get("depth") is not None,
                    "infrared": bool(config.get("infrared")),
                },
                "aligned": archive.calibration.aligned,
                "codecs": archive.meta.get("codecs"),
                "color_format": archive.meta.get("color_format"),
                # Measured from stored timestamps, not the configuration.
                "motion_rate": archive.motion_rate(),
            }
    except StreamError as error:
        return {"error": str(error)}


@app.get("/api/sessions/{session_id}/frame/{index}.jpg")
def session_frame(session_id: str, index: int, request: Request) -> Response:
    """Render one recorded frame as a JPEG.

    Args:
        session_id: Directory name.
        index: The archive's own frame index.
        request: Used for ``kind`` and ``width``.

    Returns:
        The JPEG, with its index in ``X-Frame-Index`` and its capture time in
        ``X-Received-Monotonic``, since frames are not evenly spaced.

    Raises:
        HTTPException: 404 for an unknown session, stream or frame.

    The archive is opened per request; see docs/decisions.md 10.
    """
    query = request.query_params
    kind = _kind(query.get("kind", "color"))
    only = "infrared" if kind.startswith("ir") else kind

    paths = _resolve(session_id)
    try:
        with ArchiveSource(paths.video) as archive:
            frames = archive.frame_at(index, only=only)
    except StreamError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if frames is None:
        raise HTTPException(status_code=404, detail=f"no frame {index}")

    jpeg = _preview_jpeg(frames, kind, _width(query))
    if jpeg is None:
        raise HTTPException(
            status_code=404, detail=f"this recording has no {kind} stream"
        )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "X-Frame-Index": str(frames.index),
            "X-Received-Monotonic": repr(frames.received_monotonic),
            # A recorded frame never changes, so the browser may cache it.
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.get("/api/sessions/{session_id}/frames.json")
def session_frames(session_id: str) -> dict[str, Any]:
    """Every frame's index and capture time.

    Args:
        session_id: Directory name.

    Returns:
        ``times`` as ``[[index, received_monotonic], ...]`` in order.

    Raises:
        HTTPException: 404 if the session or its archive cannot be read.

    Lets a player map an audio time to a frame exactly, since frames are not
    evenly spaced.
    """
    paths = _resolve(session_id)
    try:
        with ArchiveSource(paths.video) as archive:
            return {"times": archive.frame_times()}
    except StreamError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/sessions/{session_id}/audio.wav")
def session_audio(session_id: str, request: Request) -> Response:
    """One channel of a session's audio, as a WAV a browser can play.

    Args:
        session_id: Directory name.
        request: Used for ``channel`` - 0 is the array's processed channel, 1
            to 4 the raw microphones, 5 the playback loopback.

    Returns:
        A single-channel WAV, so a browser does not downmix the six.

    Raises:
        HTTPException: 404 if there is no audio or no such channel.

    Built whole in memory (about 1.9 MB a minute); no range requests.
    """
    paths = _resolve(session_id)
    channel = int(request.query_params.get("channel", 0))
    try:
        with wave.open(paths.audio, "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            raw = handle.readframes(handle.getnframes())
    except (OSError, wave.Error) as error:
        raise HTTPException(
            status_code=404, detail=f"no readable audio: {error}"
        ) from error
    if not 0 <= channel < channels:
        raise HTTPException(
            status_code=404, detail=f"channel {channel} of {channels}"
        )

    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples[:, channel].tobytes())

    return Response(
        content=buffer.getvalue(),
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    """Delete a session and everything in it.

    Args:
        session_id: Directory name.

    Returns:
        What was removed.

    Raises:
        HTTPException: 404 if there is no such session, 409 if it is the one
            being recorded.

    ``SessionPaths.resolve`` keeps the id inside the recordings root.
    """
    paths = _resolve(session_id)
    running = state.recorder.state()
    if running.get("recording") and running.get("session_id") == session_id:
        raise HTTPException(
            status_code=409, detail="this session is being recorded right now"
        )

    size = paths.size_bytes()
    await asyncio.to_thread(shutil.rmtree, paths.directory)
    logger.info("deleted session %s (%.1f MB)", session_id, size / 1e6)
    return {"deleted": session_id, "freed_bytes": size}


# -- settings ----------------------------------------------------------------


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """What can be changed from the page, and what it is now."""
    return {
        "sessions_dir": state.recorder.root,
        "writable": config.ALLOW_SETTINGS_WRITE,
        # Resolution and frame rate are read-only here; stream toggles and
        # codecs are not (docs/decisions.md 23).
        "streams": state.streams.as_dict(),
        "codecs": state.codecs,
    }


@app.put("/api/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    """Change the recording directory, which streams are captured, or their codecs.

    Args:
        request: JSON body with any of:
            ``sessions_dir``: a directory path.
            ``streams``: an object with any of ``color``, ``depth``,
                ``infrared``, ``motion`` as booleans.
            ``codecs``: an object with any of ``color``, ``depth``,
                ``infrared`` mapped to ``"compressed"`` or ``"raw"``. See
                ``rrr.recorder.config.codec_for``.

    Returns:
        The settings afterwards.

    Raises:
        HTTPException: 403 if changing settings is disabled, 409 while a
            recording is running, and 400 if a value cannot be applied.
    """
    if not config.ALLOW_SETTINGS_WRITE:
        raise HTTPException(status_code=403, detail="settings are read-only")

    body = await request.json()
    changing = [key for key in ("sessions_dir", "streams", "codecs") if key in body]
    if not changing:
        raise HTTPException(status_code=400, detail="nothing to change")
    if state.recorder.recording:
        raise HTTPException(
            status_code=409,
            detail="stop the recording before changing settings",
        )

    if "sessions_dir" in body:
        await _apply_sessions_dir(body["sessions_dir"])
    if "streams" in body:
        _apply_streams(body["streams"])
    if "codecs" in body:
        _apply_codecs(body["codecs"])
    return get_settings()


async def _apply_sessions_dir(raw: Any) -> None:
    """Move where future recordings are written.

    Args:
        raw: What the request body carried under ``sessions_dir``.

    Raises:
        HTTPException: 400 if it is empty or cannot be written to.
    """
    wanted = str(raw or "").strip()
    if not wanted:
        raise HTTPException(status_code=400, detail="sessions_dir is required")
    try:
        await asyncio.to_thread(_check_writable, wanted)
    except OSError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    # The recorder is long-lived (see State), so its root moves.
    state.recorder.root = wanted
    logger.info("recordings now go to %s", wanted)


_STREAM_KEYS = ("color", "depth", "infrared", "motion")


def _apply_streams(raw: Any) -> None:
    """Change which streams the camera is asked for.

    Args:
        raw: What the request body carried under ``streams`` - a mapping of
            any of ``color``, ``depth``, ``infrared``, ``motion`` to a
            boolean. A key left out keeps its current value.

    Raises:
        HTTPException: 400 for an unknown key, or a combination
            :class:`~realsense_adapter.StreamConfig` refuses (e.g. infrared without
            depth).

    Restarts the hub, since streams are settled at pipeline start.
    """
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="streams must be an object")
    unknown = set(raw) - set(_STREAM_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"unknown stream setting: {', '.join(unknown)}"
        )

    current = state.streams
    defaults = recording_config.DEFAULT_STREAMS
    fields: dict[str, Any] = {}
    if "color" in raw:
        fields["color"] = defaults.color if raw["color"] else None
    if "depth" in raw:
        fields["depth"] = defaults.depth if raw["depth"] else None
    if "infrared" in raw:
        fields["infrared"] = bool(raw["infrared"])
    if "motion" in raw:
        fields["motion"] = bool(raw["motion"])

    try:
        updated = current.with_changes(**fields)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    state.recorder.streams = updated
    state.hub.restart()
    logger.info("streams now %s", updated.as_dict())


def _apply_codecs(raw: Any) -> None:
    """Change how each stream's archive is encoded.

    Args:
        raw: What the request body carried under ``codecs`` - a mapping of any
            of ``color``, ``depth``, ``infrared`` to ``"compressed"`` or
            ``"raw"``. A key left out keeps its current codec.

    Raises:
        HTTPException: 400 for an unknown stream name or codec choice.

    No restart needed: it applies to the next archive created.
    """
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="codecs must be an object")

    updated = dict(state.codecs)
    for stream, choice in raw.items():
        try:
            updated[stream] = recording_config.codec_for(stream, choice)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    state.recorder.codecs = updated
    logger.info("codecs now %s", updated)


def _check_writable(path: str) -> None:
    """Make sure a directory exists and can be written to.

    Args:
        path: The directory to check, created if absent.

    Raises:
        OSError: If it cannot be created or written. Checked by an actual
            write, which catches read-only, full and foreign-owned mounts.
    """
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".rererecorder-write-test")
    with open(probe, "wb") as handle:
        handle.write(b"ok")
    os.unlink(probe)


# -- preview -----------------------------------------------------------------


@app.get("/stream/{kind}.mjpg")
def stream(kind: str, request: Request) -> StreamingResponse:
    """Serve one stream as MJPEG.

    Args:
        kind: ``color``, ``depth``, ``ir1`` or ``ir2``.
        request: Used for the optional ``width`` query parameter.

    Returns:
        A ``multipart/x-mixed-replace`` response.

    Raises:
        HTTPException: 404 for an unknown stream.
    """
    return StreamingResponse(
        _frames(request, _kind(kind), _width(request.query_params)),
        media_type=preview.MJPEG_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


def _preview_max_hz() -> float:
    """The preview's current rate cap: lower while recording, capped even idle.

    See docs/windows-native.md "A devices panel".
    """
    if state.recorder.recording:
        return config.PREVIEW_MAX_HZ_RECORDING
    return config.PREVIEW_MAX_HZ_IDLE


#: Seconds a live stream waits between checks while its device has failed;
#: bounds how late it notices a reconnect or its client leaving.
_FAILED_WAIT_S = 1.0

#: Longest a live stream waits for new data before checking its client again.
_STREAM_WAIT_S = 1.0


async def _frames(request: Request, kind: str, width: int):
    """Yield MJPEG parts until the client goes away.

    Holds the hub for its own life. Must check for disconnect itself, or a
    stream with no frames holds the hub forever - see docs/decisions.md 29.
    """
    hub = state.hub
    hub.acquire()
    try:
        after = 0
        next_at = 0.0
        while not await request.is_disconnected():
            frames = await run_in_threadpool(hub.latest, _STREAM_WAIT_S, after)
            if frames is None:
                # Camera starting or gone: keep the response open regardless.
                if hub.failed:
                    await asyncio.sleep(_FAILED_WAIT_S)
                continue
            after = frames.index
            now = time.monotonic()
            if now < next_at:
                continue
            next_at = now + 1.0 / _preview_max_hz()

            jpeg = await run_in_threadpool(_preview_jpeg, frames, kind, width)
            if jpeg is not None:
                yield preview.mjpeg_part(jpeg)
    finally:
        hub.release()


def _kind(kind: str) -> str:
    """Check a requested preview kind.

    Raises:
        HTTPException: 404 for a stream there is no preview of.
    """
    if kind not in preview.KINDS:
        raise HTTPException(status_code=404, detail=f"no stream {kind!r}")
    return kind


def _width(query: Any) -> int:
    """The preview width a request asks for, or the configured one."""
    return int(query.get("width", config.PREVIEW_WIDTH))


def _preview_jpeg(frames, kind: str, width: int) -> bytes | None:
    """Render one preview frame, or None if the set lacks that stream."""
    image = preview.render(frames, kind)  # type: ignore[arg-type]
    if image is None:
        return None
    return preview.encode_jpeg(preview.downscale(image, width), config.JPEG_QUALITY)


@app.get("/stream/audio-levels")
def audio_levels(request: Request) -> StreamingResponse:
    """Stream each channel's current level, for a live meter.

    Returns:
        A ``text/event-stream`` response, one JSON object per update: ``mix``
        (the beamformed channel) and ``mic1``-``mic4`` (the raw microphones),
        each in dBFS or ``null`` for silence.

    Raises:
        HTTPException: 404 if this server was started with audio off.

    Holds the recorder's tap for as long as the connection lasts.
    """
    tap = state.recorder.tap
    if tap is None:
        raise HTTPException(
            status_code=404, detail="this server was started with audio off"
        )
    return StreamingResponse(
        _levels(request, tap),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


async def _levels(request: Request, tap: AudioTap):
    """Yield one SSE event per interval with each channel's level.

    Checks for its client itself, for the reason given in ``_frames``.
    """
    tap.acquire()
    try:
        after = 0
        interval = (
            1.0 / config.AUDIO_LEVEL_HZ if config.AUDIO_LEVEL_HZ > 0 else 0.0
        )
        next_at = 0.0
        while not await request.is_disconnected():
            window = await run_in_threadpool(
                tap.latest, config.AUDIO_LEVEL_WINDOW_S, _STREAM_WAIT_S, after
            )
            if window is None:
                # Array starting or gone: keep the connection open.
                if tap.failed:
                    await asyncio.sleep(_FAILED_WAIT_S)
                continue
            after = window.index
            now = time.monotonic()
            if now < next_at:
                continue
            next_at = now + interval

            mics = rms(window.mics)
            payload = {
                "mix": dbfs(float(rms(window.processed))),
                "mic1": dbfs(float(mics[0])),
                "mic2": dbfs(float(mics[1])),
                "mic3": dbfs(float(mics[2])),
                "mic4": dbfs(float(mics[3])),
            }
            yield f"data: {json.dumps(payload)}\n\n"
    finally:
        tap.release()

