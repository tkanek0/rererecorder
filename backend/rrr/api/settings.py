"""What the next recording does, changed from the page."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from rrr.recorder import config as recording_config

from . import config
from .state import state

logger = logging.getLogger(__name__)

router = APIRouter()


# -- settings ----------------------------------------------------------------


@router.get("/api/settings")
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


@router.put("/api/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    """Change the recording directory, which streams are captured, or their codecs.

    Args:
        request: JSON body with any of:
            ``sessions_dir``: a directory path.
            ``streams``: an object with any of ``color``, ``depth``,
                ``infrared``, ``motion`` as booleans.
            ``codecs``: an object with any of ``color``, ``depth``,
                ``infrared`` mapped to ``"compressed"`` or ``"raw"``. See
                ``rrr.recorder.config.with_codecs``.

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
    try:
        updated = recording_config.with_streams(state.streams, raw)
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

    try:
        updated = recording_config.with_codecs(state.codecs, raw)
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
