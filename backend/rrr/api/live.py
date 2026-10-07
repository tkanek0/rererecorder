"""The live previews: MJPEG for the camera, server-sent levels for the array."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from realsense_adapter import FrameSet
from respeaker_adapter import Window, dbfs, rms

from rrr.devices import SharedWorker

from . import config, preview
from .state import state

router = APIRouter()

T = TypeVar("T")

#: Seconds a live stream waits between checks while its device has failed;
#: bounds how late it notices a reconnect or its client leaving.
_FAILED_WAIT_S = 1.0

#: Longest a live stream waits for new data before checking its client again.
_STREAM_WAIT_S = 1.0


def requested_kind(kind: str) -> str:
    """Check a requested preview kind.

    Raises:
        HTTPException: 404 for a stream there is no preview of.
    """
    if kind not in preview.KINDS:
        raise HTTPException(status_code=404, detail=f"no stream {kind!r}")
    return kind


def requested_width(query: Any) -> int:
    """The preview width a request asks for, or the configured one."""
    return int(query.get("width", config.PREVIEW_WIDTH))


def preview_jpeg(frames: FrameSet, kind: str, width: int) -> bytes | None:
    """Render one preview frame, or None if the set lacks that stream."""
    image = preview.render(frames, kind)  # type: ignore[arg-type]
    if image is None:
        return None
    return preview.encode_jpeg(preview.downscale(image, width), config.JPEG_QUALITY)


async def _poll(
    request: Request,
    worker: SharedWorker[Any],
    fetch: Callable[[int], T | None],
    max_hz: Callable[[], float],
    render: Callable[[T], bytes | str | None],
) -> AsyncIterator[bytes | str]:
    """Yield what ``render`` makes of a device's newest item until the client leaves.

    Holds the device for its own life, at no more than ``max_hz()``. Checks for
    the client itself, or a device delivering nothing would hold it forever -
    see docs/decisions.md 29.
    """
    worker.acquire()
    try:
        after = 0
        next_at = 0.0
        while not await request.is_disconnected():
            item = await run_in_threadpool(fetch, after)
            if item is None:
                # Starting or gone: keep the response open regardless.
                if worker.failed:
                    await asyncio.sleep(_FAILED_WAIT_S)
                continue
            after = item.index  # type: ignore[attr-defined]
            now = time.monotonic()
            if now < next_at:
                continue
            next_at = now + 1.0 / max_hz()
            rendered = await run_in_threadpool(render, item)
            if rendered is not None:
                yield rendered
    finally:
        worker.release()


@router.get("/stream/{kind}.mjpg")
def stream(kind: str, request: Request) -> StreamingResponse:
    """Serve one stream as MJPEG, at the preview rate caps in :mod:`.config`.

    Args:
        kind: ``color``, ``depth``, ``ir1`` or ``ir2``.
        request: Used for the optional ``width`` query parameter.

    Raises:
        HTTPException: 404 for an unknown stream.
    """
    kind = requested_kind(kind)
    width = requested_width(request.query_params)

    def render(frames: FrameSet) -> bytes | None:
        jpeg = preview_jpeg(frames, kind, width)
        return preview.mjpeg_part(jpeg) if jpeg is not None else None

    return StreamingResponse(
        _poll(
            request,
            state.hub,
            lambda after: state.hub.latest(_STREAM_WAIT_S, after),
            lambda: (
                config.PREVIEW_MAX_HZ_RECORDING
                if state.recorder.recording
                else config.PREVIEW_MAX_HZ_IDLE
            ),
            render,
        ),
        media_type=preview.MJPEG_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/stream/audio-levels")
def audio_levels(request: Request) -> StreamingResponse:
    """Stream each channel's current level, for a live meter.

    Returns:
        A ``text/event-stream`` response, one JSON object per update: ``mix``
        (the beamformed channel) and ``mic1``-``mic4`` (the raw microphones),
        each in dBFS or ``null`` for silence.

    Raises:
        HTTPException: 404 if this server was started with audio off.
    """
    tap = state.recorder.tap
    if tap is None:
        raise HTTPException(
            status_code=404, detail="this server was started with audio off"
        )

    def render(window: Window) -> str:
        mics = rms(window.mics)
        levels = {"mix": dbfs(float(rms(window.processed)))}
        levels.update(
            {f"mic{n + 1}": dbfs(float(level)) for n, level in enumerate(mics)}
        )
        return f"data: {json.dumps(levels)}\n\n"

    return StreamingResponse(
        _poll(
            request,
            tap,
            lambda after: tap.latest(
                config.AUDIO_LEVEL_WINDOW_S, _STREAM_WAIT_S, after
            ),
            lambda: config.AUDIO_LEVEL_HZ,
            render,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )
