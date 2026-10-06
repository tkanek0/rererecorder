"""Starting and stopping a recording, and marking it."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from rrr.recorder import RecorderBusy
from rrr.timeline import (
    SessionError,
)

from .state import state

logger = logging.getLogger(__name__)

router = APIRouter()


# -- recording ---------------------------------------------------------------


@router.put("/api/recording")
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


@router.post("/api/events")
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
