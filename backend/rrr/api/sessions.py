"""Recorded sessions: listing, playing back and deleting them."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import wave
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request, Response
from realsense_adapter import (
    StreamError,
)

from rrr.timeline import (
    SessionError,
    SessionPaths,
    listing,
    read_manifest,
)
from rrr.video import ArchiveSource

from .live import preview_jpeg, requested_kind, requested_width
from .state import state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/sessions")
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


@router.get("/api/sessions/{session_id}")
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


@router.get("/api/sessions/{session_id}/frame/{index}.jpg")
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
    kind = requested_kind(query.get("kind", "color"))
    only = "infrared" if kind.startswith("ir") else kind

    paths = _resolve(session_id)
    try:
        with ArchiveSource(paths.video) as archive:
            frames = archive.frame_at(index, only=only)
    except StreamError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if frames is None:
        raise HTTPException(status_code=404, detail=f"no frame {index}")

    jpeg = preview_jpeg(frames, kind, requested_width(query))
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


@router.get("/api/sessions/{session_id}/frames.json")
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


@router.get("/api/sessions/{session_id}/audio.wav")
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


@router.delete("/api/sessions/{session_id}")
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
