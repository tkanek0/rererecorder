"""The live views: MJPEG for the camera; levels, direction and sound for the array."""

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
from respeaker_adapter import Window, dbfs, rms, to_int16

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


@router.get("/stream/array")
def array_state(request: Request) -> StreamingResponse:
    """Stream each channel's current level and the chip's direction, for a meter.

    Returns:
        A ``text/event-stream`` response, one JSON object per update: ``mix``
        (the processed channel) and ``mic1``-``mic4`` (the raw microphones),
        each in dBFS or ``null`` for silence, and ``doa``, the newest
        ``{"angle", "voice"}`` reading or ``null`` while there is none.
    """
    tap = state.recorder.tap
    doa = state.recorder.doa

    def render(window: Window) -> str:
        mics = rms(window.mics)
        levels: dict[str, Any] = {"mix": dbfs(float(rms(window.processed)))}
        levels.update(
            {f"mic{n + 1}": dbfs(float(level)) for n, level in enumerate(mics)}
        )
        reading = doa.latest(timeout=0)
        levels["doa"] = (
            {"angle": reading.angle, "voice": reading.voice_activity}
            if reading is not None
            else None
        )
        return f"data: {json.dumps(levels)}\n\n"

    async def events() -> AsyncIterator[str]:
        # Holds the direction for as long as the levels hold the audio.
        doa.acquire()
        try:
            async for event in _poll(
                request,
                tap,
                lambda after: tap.latest(
                    config.AUDIO_LEVEL_WINDOW_S, _STREAM_WAIT_S, after
                ),
                lambda: config.AUDIO_LEVEL_HZ,
                render,
            ):
                yield event  # type: ignore[misc]
        finally:
            doa.release()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/stream/audio.pcm")
def listen(request: Request, channel: int = 0) -> StreamingResponse:
    """Stream one channel of the array as it is captured, to listen to.

    Read forward from the moment of the request, as a recording reads it, so
    nothing is skipped unless the client falls a whole ring behind.

    Args:
        request: Used to notice the client leaving.
        channel: 0 for the processed channel, 1-4 for the microphones.

    Returns:
        Raw ``audio/L16`` (16-bit little-endian, mono) at the rate the
        content type names, for as long as the client stays.

    Raises:
        HTTPException: 404 for a channel the array does not have.
    """
    tap = state.recorder.tap
    if not 0 <= channel < tap.channels:
        raise HTTPException(
            status_code=404, detail=f"channel {channel} of {tap.channels}"
        )

    async def pcm() -> AsyncIterator[bytes]:
        tap.acquire()
        try:
            cursor = tap.cursor
            while not await request.is_disconnected():
                chunk = await run_in_threadpool(tap.stream, cursor, _STREAM_WAIT_S)
                if chunk is None:
                    if tap.failed:
                        await asyncio.sleep(_FAILED_WAIT_S)
                    continue
                cursor = chunk.cursor
                yield to_int16(chunk.samples[:, channel]).tobytes()
        finally:
            tap.release()

    return StreamingResponse(
        pcm(),
        # RFC 2586 names L16 big-endian; the page reads this one as little.
        media_type=f"audio/L16; rate={tap.rate}; channels=1; endian=little",
        headers={"Cache-Control": "no-store"},
    )
