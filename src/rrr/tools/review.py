"""Reading a raw session for presentation copies.

Shared by the review renderers (``render_mp4``, ``render_gif``). The archive and
WAV remain the measurements; these helpers only decide which channel to show,
how to place it on the video clock, and how to turn a colour frame into BGR.
"""

from __future__ import annotations

import wave
from typing import Any

import cv2
import numpy as np

from rrr.timeline import AudioClockPoint, AudioTimeline, Rig


def describe_audio(path: str, requested: str, rig: Rig) -> dict[str, Any] | None:
    """Check the WAV and resolve the channel selection.

    Args:
        path: The session's WAV.
        requested: ``processed``, ``mix``, or a zero-based WAV channel.
        rig: The session's rig, whose ``channels`` name the physical
            microphones for ``mix``.

    Returns:
        The WAV layout and the selected channels, or None if there is no
        readable WAV.

    Raises:
        ValueError: If the WAV is not 16-bit PCM or the selection is invalid.
    """
    try:
        with wave.open(path, "rb") as handle:
            if handle.getsampwidth() != 2:
                raise ValueError("only 16-bit PCM ReSpeaker WAV files are supported")
            channels = handle.getnchannels()
            rate = handle.getframerate()
            samples = handle.getnframes()
    except (FileNotFoundError, wave.Error):
        return None

    if requested == "processed":
        selected = (0,)
        label = "processed channel 0"
    elif requested == "mix":
        if rig.channels:
            selected = tuple(rig.channels)
            label = "physical microphone mix from rig"
        elif channels >= 5:
            selected = tuple(range(1, 5))
            label = "nominal ReSpeaker microphone mix (channels 1-4)"
        else:
            selected = tuple(range(channels))
            label = "all-channel mix"
    else:
        try:
            selected = (int(requested),)
        except ValueError as error:
            raise ValueError(
                "--audio-channel must be processed, mix, or a channel number"
            ) from error
        label = f"channel {selected[0]}"
    if not selected or any(channel < 0 or channel >= channels for channel in selected):
        raise ValueError(f"audio channel selection {selected} is outside {channels}-ch WAV")
    return {
        "path": path,
        "rate": rate,
        "samples": samples,
        "channels": channels,
        "selected": selected,
        "label": label,
    }


def audio_timeline(
    clock_path: str,
    rate: int,
    first_monotonic: float | None,
    *,
    fallback_start: float,
) -> tuple[AudioTimeline, bool]:
    """Use the measured sidecar, or an honest nominal fallback.

    Args:
        clock_path: The session's ``audio.clock.jsonl``.
        rate: Nominal sample rate from the WAV header.
        first_monotonic: When the first sample arrived, if the manifest says.
        fallback_start: Where sample zero goes when nothing else is known.

    Returns:
        The timeline, and whether it came from the measured sidecar.
    """
    try:
        return AudioTimeline.read(clock_path, rate), True
    except (OSError, ValueError):
        start = first_monotonic if first_monotonic is not None else fallback_start
        return AudioTimeline([AudioClockPoint(sample=0, monotonic=start)], rate), False


def bgr(frames: Any) -> np.ndarray:
    """Convert either recorded colour representation into encoder-ready BGR.

    Args:
        frames: A frame set whose colour image is present.

    Returns:
        ``(height, width, 3)`` uint8 BGR.
    """
    if frames.color_format == "yuyv":
        height, width = frames.color.shape
        packed = frames.color.view(np.uint8).reshape(height, width, 2)
        return cv2.cvtColor(packed, cv2.COLOR_YUV2BGR_YUY2)
    return cv2.cvtColor(frames.color, cv2.COLOR_RGB2BGR)
