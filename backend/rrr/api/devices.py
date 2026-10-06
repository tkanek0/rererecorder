"""What the page polls for, and reconnecting a device."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException
from respeaker_adapter import probe as probe_audio
from respeaker_adapter import rescan

from rrr.recorder import config as recording_config

from .state import state

logger = logging.getLogger(__name__)

router = APIRouter()


# -- status ------------------------------------------------------------------


@router.get("/api/status")
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


@router.post("/api/devices/{name}/reconnect")
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
