"""The control plane: preview out, recording commands in.

Transport only. Every decision about what a recording contains lives in
:mod:`recorder` and :mod:`video`, and this module knows none of it - which is
what lets the CLI record without a server running and lets the server record
through exactly the same code rather than a copy of it.

The camera is opened once, by one hub, and shared. A recording registers as a
listener on that hub - so it receives every frame, in order, with none dropped -
while the preview polls for the newest. Starting a recording therefore does not
restart the pipeline: the preview keeps running, and auto-exposure does not have
to settle again in the middle of what is being recorded.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import time
import wave
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from rrr.recorder import RecorderBusy, SessionRecorder
from rrr.recorder import config as recording_config
from rrr.timeline import (
    SessionError,
    SessionPaths,
    listing,
    read_events,
    read_manifest,
)
from rrr.video import ArchiveSource, FrameHub, LiveSource, StreamConfig, StreamError

from . import config, preview

logger = logging.getLogger(__name__)


class State:
    """Everything the server owns, for the life of the process.

    One hub, one recorder. The recorder is long-lived rather than made per
    session because it holds the audio taps, and the array is left in a state
    where the next open fails if a capture stream is not closed properly.
    """

    def __init__(self) -> None:
        self.sessions_root = recording_config.SESSIONS_ROOT
        self.hub = FrameHub(
            self._open_camera,
            idle_shutdown_s=config.IDLE_SHUTDOWN_S,
            reconnect_delay_s=config.RECONNECT_DELAY_S,
        )
        self.recorder = SessionRecorder(
            self.sessions_root,
            streams=recording_config.DEFAULT_STREAMS,
            serial=recording_config.SERIAL,
            record_video=recording_config.RECORD_VIDEO,
            record_audio=recording_config.RECORD_AUDIO,
            record_doa=recording_config.RECORD_DOA,
            codecs=dict(recording_config.CODECS),
            hub=self.hub,
        )
        #: Bytes per second the last recording achieved. Kept so that the
        #: remaining-time estimate survives the recording ending: the number is
        #: what makes free space meaningful, and "measured while recording" is
        #: not an answer to "how long can I record".
        self.last_write_rate: float | None = None

    @property
    def streams(self) -> StreamConfig:
        """What the camera is asked for.

        The recorder is the source of truth - reading through it here rather
        than keeping a second copy is what keeps this and the archive's own
        metadata from being able to disagree.
        """
        return self.recorder.streams

    @property
    def codecs(self) -> dict[str, str]:
        """How each stream's archive is encoded. See ``streams`` for why this
        reads through the recorder rather than keeping its own copy.
        """
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
)


# -- status ------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Whether the server is up, and what it can see."""
    return {
        "ok": True,
        "camera": _camera(),
        "recording": state.recorder.recording,
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    """Everything the page polls for: camera, recording and disk."""
    return {
        "camera": _camera(),
        "recording": state.recorder.state(),
        "storage": _storage(),
    }


def _camera() -> dict[str, Any]:
    """Describe the camera and what it is streaming."""
    hub = state.hub
    device = hub.device
    source = hub.source
    return {
        "active": hub.active,
        "fps": round(hub.fps, 2),
        "error": hub.error,
        "device": device.as_dict() if device else None,
        "streams": state.streams.as_dict(),
        "timestamp_domain": getattr(source, "timestamp_domain", "unknown"),
        "listeners": hub.listeners,
    }


def _storage() -> dict[str, Any]:
    """Where recordings go, and how much room is left there.

    Returns:
        The directory, its free and total bytes, and how long that lasts at the
        rate this recording is actually writing.

    The remaining time is the number worth showing. Free bytes alone do not say
    much when a session costs 195 GB an hour: 200 GB free reads as plenty and is
    an hour.
    """
    root = state.sessions_root
    probe = root if os.path.isdir(root) else os.path.dirname(os.path.abspath(root))
    try:
        usage = shutil.disk_usage(probe)
        free, total = usage.free, usage.total
    except OSError as error:
        return {"sessions_dir": root, "error": str(error)}

    live = _write_rate()
    if live:
        state.last_write_rate = live
    # Falls back to what the last recording achieved. Still measured, just not
    # right now - and said so, because an estimate from a different scene is
    # worth less than one from this one.
    basis = live or state.last_write_rate
    return {
        "sessions_dir": root,
        "free_bytes": free,
        "total_bytes": total,
        "write_bytes_per_s": live,
        "rate_is_live": live is not None,
        "seconds_left": free / basis if basis else None,
    }


def _write_rate() -> float | None:
    """How fast the current recording is growing, in bytes per second.

    Returns:
        The measured rate, or None when nothing is being recorded. Measured
        rather than assumed, because it depends on the scene: a featureless
        wall compresses far better than a cluttered room.
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

    Runs in a thread: starting waits for the camera's first frame and stopping
    drains the encoder queue, both of which take long enough to block the event
    loop and with it the preview.
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

    Not run in a thread: appending one line and flushing it is microseconds,
    and a mark is worth stamping as close to the request as possible.
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
        "sessions_dir": state.sessions_root,
        "sessions": [manifest.as_dict() for manifest in listing(state.sessions_root)],
    }


def _resolve(session_id: str) -> SessionPaths:
    """Turn a session id from a URL into paths, or a 404.

    Args:
        session_id: What the client asked for.

    Returns:
        The paths.

    Raises:
        HTTPException: 404 if it is not a session id or no such session exists.
            ``SessionPaths.resolve`` is the only thing between a path parameter
            and the filesystem, and it rejects rather than sanitises.
    """
    try:
        return SessionPaths.resolve(state.sessions_root, session_id)
    except SessionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str) -> dict[str, Any]:
    """Everything known about one session, enough to play it back.

    Args:
        session_id: Directory name.

    Returns:
        The manifest, plus the archive's frame range and which streams it
        holds - a player needs the range to seek within, and the stream list to
        know what it can show.

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
    # Read rather than counted from the manifest: the file is what a session
    # actually holds, and a session recorded before marks existed has none.
    try:
        detail["events"] = [event.as_dict() for event in read_events(paths.events)]
    except ValueError as error:
        logger.warning("unreadable marks in %s: %s", session_id, error)
        detail["events"] = []
    return detail


def _archive_detail(path: str) -> dict[str, Any]:
    """Describe an archive's frames without decoding any of them.

    Args:
        path: The archive to open.

    Returns:
        Its frame range, count and available streams, or an ``error``. A
        session whose archive is unreadable still has a manifest worth showing,
        so this reports rather than raises.
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
                # Measured from the stored timestamps, not read from the
                # configuration: a recording that kept one sample per frame
                # reports 30 Hz here, which is how it gives itself away.
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
        request: Used for ``kind``, ``width``, ``near``, ``far`` and
            ``colormap``.

    Returns:
        The JPEG, with the frame's own capture time in ``X-Capture-Monotonic``.
        A player reads that rather than assuming frames are evenly spaced: they
        are not, because a set the camera mispaired leaves a gap.

    Raises:
        HTTPException: 404 for an unknown session, stream or frame.

    The archive is opened per request. Measured: opening, reading one colour
    frame and closing costs 15.7 ms against 16.3 ms with the archive already
    open - the open is 1.3 ms and the operating system's page cache absorbs the
    rest. Keeping one open would save nothing measurable and would need a lock,
    because a synchronous endpoint runs on whichever thread is free.
    """
    kind = request.query_params.get("kind", "color")
    if kind not in ("color", "depth", "ir1", "ir2"):
        raise HTTPException(status_code=404, detail=f"no stream {kind!r}")
    only = "infrared" if kind.startswith("ir") else kind

    paths = _resolve(session_id)
    query = request.query_params
    try:
        with ArchiveSource(paths.video) as archive:
            frames = archive.frame_at(index, only=only)
    except StreamError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if frames is None:
        raise HTTPException(status_code=404, detail=f"no frame {index}")

    image = preview.render(
        frames,
        kind,  # type: ignore[arg-type]
        near_m=float(query.get("near", config.DEPTH_NEAR_M)),
        far_m=float(query.get("far", config.DEPTH_FAR_M)),
        colormap=query.get("colormap", config.DEPTH_COLORMAP),
    )
    if image is None:
        raise HTTPException(
            status_code=404, detail=f"this recording has no {kind} stream"
        )
    jpeg = preview.encode_jpeg(
        preview.downscale(image, int(query.get("width", config.PREVIEW_WIDTH))),
        config.JPEG_QUALITY,
    )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "X-Frame-Index": str(frames.index),
            "X-Received-Monotonic": repr(frames.received_monotonic),
            # A recorded frame never changes, so the browser may keep it. This
            # is what makes seeking backwards and looping feel immediate.
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

    What a player needs to turn "the audio is 4.2 seconds in" into "show frame
    1234". Interpolating from the first and last would be close but not exact,
    because a set the camera mispaired leaves a gap. About 30 KB for a 30 second
    recording, 3 MB for an hour, fetched once.
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
        A single-channel WAV. Not the recorded file: that has six channels, and
        a browser would fold them together into something nobody recorded. One
        channel at a time is what a person listening actually wants.

    Raises:
        HTTPException: 404 if there is no audio or no such channel.

    The whole file is built in memory and returned at once. At 16 kHz mono that
    is 1.9 MB a minute, so this is fine for the sessions this records and would
    need range requests for something much longer.
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

    Deleting is offered because a session costs 1.7 GB for 34 seconds: without
    it, the only way to reclaim space is a shell. It removes the directory and
    its contents and nothing else - the id cannot name anything outside the
    recordings root, which ``SessionPaths.resolve`` enforces.
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
        "sessions_dir": state.sessions_root,
        "writable": config.ALLOW_SETTINGS_WRITE,
        # Resolution and frame rate are settled when the pipeline starts and
        # stay read-only here; which streams are asked for at all, and how
        # each is encoded, can both be changed - see _apply_streams and
        # _apply_codecs.
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
                ``infrared`` as booleans - whether to ask the camera for that
                stream at all. Resolution and frame rate stay as the
                environment set them.
            ``codecs``: an object with any of ``color``, ``depth``,
                ``infrared`` mapped to ``"compressed"`` or ``"raw"``. See
                ``rrr.recorder.config.codec_for``.

    Returns:
        The settings afterwards.

    Raises:
        HTTPException: 403 if changing settings is disabled, 409 while a
            recording is running - a session cannot describe two
            configurations at once - and 400 if a value cannot be applied.
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

    state.sessions_root = wanted
    # The recorder is long-lived - it holds the audio taps, which must be closed
    # properly - so the root moves rather than the recorder being replaced.
    state.recorder.root = wanted
    logger.info("recordings now go to %s", wanted)


_STREAM_KEYS = ("color", "depth", "infrared")


def _apply_streams(raw: Any) -> None:
    """Change which streams the camera is asked for.

    Args:
        raw: What the request body carried under ``streams`` - a mapping of
            any of ``color``, ``depth``, ``infrared`` to a boolean. A key left
            out keeps its current value.

    Raises:
        HTTPException: 400 for a key this does not recognise, or a
            combination :class:`~rrr.video.StreamConfig` refuses - most
            commonly infrared left on with depth turned off, since infrared is
            the depth sensor's own pair.

    Takes effect the next time the camera opens: the SDK settles resolution
    and frame rate at pipeline start, so a hub already running - because a
    preview or a recording holds it open - is restarted, exactly as a
    resolution change would need.
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

    Nothing about the camera restarts for this: the archive is created fresh
    at the start of each recording, so this only has to be set before the
    next one begins.
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
        OSError: If it cannot be created or written. Checked by writing rather
            than by reading permissions: a mount can be read-only, full, or
            owned by somebody else, and only an actual write finds all three.
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
        request: Used for the optional ``near``, ``far``, ``colormap`` and
            ``width`` query parameters.

    Returns:
        A ``multipart/x-mixed-replace`` response, which an ``<img>`` renders as
        live video with no JavaScript at all.

    Raises:
        HTTPException: 404 for an unknown stream.
    """
    if kind not in ("color", "depth", "ir1", "ir2"):
        raise HTTPException(status_code=404, detail=f"no stream {kind!r}")

    query = request.query_params
    near = float(query.get("near", config.DEPTH_NEAR_M))
    far = float(query.get("far", config.DEPTH_FAR_M))
    colormap = query.get("colormap", config.DEPTH_COLORMAP)
    width = int(query.get("width", config.PREVIEW_WIDTH))

    return StreamingResponse(
        _frames(kind, near, far, colormap, width),
        media_type=preview.MJPEG_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


def _frames(kind: str, near: float, far: float, colormap: str, width: int):
    """Yield MJPEG parts until the client goes away.

    The hub is held for the life of the generator, so the camera closes shortly
    after the last preview disconnects - unless a recording is holding it, which
    it does by its own reference.
    """
    hub = state.hub
    hub.acquire()
    try:
        after = 0
        interval = 1.0 / config.PREVIEW_MAX_HZ if config.PREVIEW_MAX_HZ > 0 else 0.0
        next_at = 0.0
        while True:
            frames = hub.latest(timeout=5.0, after=after)
            if frames is None:
                # Nothing arrived: the camera may be starting or gone. Keep the
                # response open rather than ending it, so the page does not have
                # to distinguish "no frames yet" from "stream over".
                continue
            after = frames.index
            now = time.monotonic()
            if now < next_at:
                continue
            next_at = now + interval

            image = preview.render(
                frames, kind, near_m=near, far_m=far, colormap=colormap
            )
            if image is None:
                continue
            jpeg = preview.encode_jpeg(
                preview.downscale(image, width), config.JPEG_QUALITY
            )
            yield preview.mjpeg_part(jpeg)
    except (GeneratorExit, ConnectionError):
        pass
    finally:
        hub.release()


# -- the page ----------------------------------------------------------------

if os.path.isdir(config.STATIC_DIR):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(config.STATIC_DIR, "assets")),
        name="assets",
    )

    @app.get("/")
    def index() -> FileResponse:
        """Serve the built page."""
        return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))

else:

    @app.get("/")
    def no_page() -> dict[str, Any]:
        """Explain where the page is, when it has not been built.

        Normal during development: vite serves it on its own port and talks to
        this server across origins.
        """
        return {
            "detail": (
                f"no built frontend at {config.STATIC_DIR!r}. Run `make web` for "
                "the dev server, or `make web-build` to build it into this one."
            )
        }
