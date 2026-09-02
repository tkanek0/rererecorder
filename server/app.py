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
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from recorder import RecorderBusy, SessionRecorder
from recorder import config as recording_config
from timeline import SessionError, listing
from video import FrameHub, LiveSource

from . import config, preview

logger = logging.getLogger(__name__)


class State:
    """Everything the server owns, for the life of the process.

    One hub, one recorder. The recorder is long-lived rather than made per
    session because it holds the audio taps, and the array is left in a state
    where the next open fails if a capture stream is not closed properly.
    """

    def __init__(self) -> None:
        self.streams = recording_config.DEFAULT_STREAMS
        self.sessions_root = recording_config.SESSIONS_ROOT
        self.hub = FrameHub(
            self._open_camera,
            idle_shutdown_s=config.IDLE_SHUTDOWN_S,
            reconnect_delay_s=config.RECONNECT_DELAY_S,
        )
        self.recorder = SessionRecorder(
            self.sessions_root,
            streams=self.streams,
            serial=recording_config.SERIAL,
            record_video=recording_config.RECORD_VIDEO,
            record_audio=recording_config.RECORD_AUDIO,
            record_doa=recording_config.RECORD_DOA,
            codecs=recording_config.CODECS,
            hub=self.hub,
        )

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

    rate = _write_rate()
    return {
        "sessions_dir": root,
        "free_bytes": free,
        "total_bytes": total,
        "write_bytes_per_s": rate,
        "seconds_left": free / rate if rate else None,
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


@app.get("/api/sessions")
def sessions() -> dict[str, Any]:
    """Every readable session in the current directory, newest first."""
    return {
        "sessions_dir": state.sessions_root,
        "sessions": [manifest.as_dict() for manifest in listing(state.sessions_root)],
    }


# -- settings ----------------------------------------------------------------


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """What can be changed from the page, and what it is now."""
    return {
        "sessions_dir": state.sessions_root,
        "writable": config.ALLOW_SETTINGS_WRITE,
        # Read-only here: resolution and frame rate are settled when the
        # pipeline starts, so changing them means restarting the camera. Left
        # to the environment so that a session's conditions cannot drift
        # between recordings without someone meaning it.
        "streams": state.streams.as_dict(),
        "codecs": recording_config.CODECS,
    }


@app.put("/api/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    """Change the recording directory.

    Args:
        request: JSON body with ``sessions_dir``.

    Returns:
        The settings afterwards.

    Raises:
        HTTPException: 403 if changing settings is disabled, 409 while a
            recording is running - moving the directory mid-session would
            split it across two disks - and 400 if the path cannot be written.
    """
    if not config.ALLOW_SETTINGS_WRITE:
        raise HTTPException(status_code=403, detail="settings are read-only")
    if state.recorder.recording:
        raise HTTPException(
            status_code=409, detail="stop the recording before moving its directory"
        )

    body = await request.json()
    wanted = str(body.get("sessions_dir") or "").strip()
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
    return get_settings()


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
