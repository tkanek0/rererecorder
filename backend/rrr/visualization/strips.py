"""Horizontal strips that show a recording's sound under its pictures."""

from __future__ import annotations

import cv2
import numpy as np

PAD = 4
VOLUME_HEIGHT = 30
WAVEFORM_HEIGHT = 70
BACKGROUND = (16, 16, 16)
# BGR.
PLAYED = (210, 180, 150)
UNPLAYED = (110, 90, 70)
PLAYHEAD = (60, 60, 255)
WAVE = (140, 220, 120)
AXIS = (50, 50, 50)
LABEL = (190, 190, 190)
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.35
# Amplitude that fills the waveform strip; a high percentile so that one click
# does not flatten the rest of the recording.
WAVEFORM_PERCENTILE = 99.95


def loudness_profile(samples: np.ndarray, width: int) -> np.ndarray:
    """RMS of ``width`` equal slices, scaled so the loudest is 1.

    Args:
        samples: The whole recording, mono.
        width: Number of slices, one per strip column.

    Returns:
        ``width`` values in ``[0, 1]``.
    """
    rms = np.array(
        [
            float(np.sqrt(np.mean(np.square(part, dtype=np.float64))))
            if len(part)
            else 0.0
            for part in np.array_split(samples, width)
        ]
    )
    peak = rms.max() if len(rms) else 0.0
    return rms / peak if peak > 0 else rms


def waveform_scale(samples: np.ndarray) -> float:
    """The amplitude that fills the waveform strip, shared by every frame.

    Args:
        samples: The whole recording, mono.

    Returns:
        A positive amplitude.
    """
    if not len(samples):
        return 1.0
    value = float(np.percentile(np.abs(samples), WAVEFORM_PERCENTILE))
    return value if value > 0 else 1.0


def volume_strip(loudness: np.ndarray, width: int, progress: float) -> np.ndarray:
    """Draw the whole recording's loudness with a playhead.

    Args:
        loudness: Per-column loudness, as :func:`loudness_profile` returns.
        width: Strip width in pixels.
        progress: Where the playhead is, as a fraction of the recording.

    Returns:
        ``(VOLUME_HEIGHT, width, 3)`` uint8 BGR.
    """
    strip = np.full((VOLUME_HEIGHT, width, 3), BACKGROUND, dtype=np.uint8)
    head = int(np.clip(progress, 0.0, 1.0) * (width - 1))
    for x, value in enumerate(loudness):
        top = VOLUME_HEIGHT - 1 - round(value * (VOLUME_HEIGHT - 1))
        strip[top:, x] = PLAYED if x <= head else UNPLAYED
    cv2.line(strip, (head, 0), (head, VOLUME_HEIGHT - 1), PLAYHEAD, 2)
    return strip


def waveform_strip(
    samples: np.ndarray,
    rate: int,
    scale: float,
    width: int,
    end: int,
    window_s: float,
    *,
    label: str,
    elapsed: float,
) -> np.ndarray:
    """Draw the ``window_s`` seconds ending at sample ``end`` as a min/max envelope.

    Args:
        samples: The whole recording, mono.
        rate: Its sample rate.
        scale: Amplitude that fills the strip, as :func:`waveform_scale` returns.
        width: Strip width in pixels.
        end: Sample the window ends at; before the recording is silence.
        window_s: Length of the window.
        label: Text for the top left.
        elapsed: Seconds shown at the top right.

    Returns:
        ``(WAVEFORM_HEIGHT, width, 3)`` uint8 BGR.
    """
    length = max(round(window_s * rate), 1)
    window = np.zeros(length, dtype=np.float32)
    first = end - length
    source = samples[max(first, 0) : max(min(end, len(samples)), 0)]
    if len(source):
        window[max(-first, 0) : max(-first, 0) + len(source)] = source

    strip = np.full((WAVEFORM_HEIGHT, width, 3), BACKGROUND, dtype=np.uint8)
    middle = (WAVEFORM_HEIGHT - 1) / 2
    cv2.line(strip, (0, round(middle)), (width - 1, round(middle)), AXIS, 1)
    for x, part in enumerate(np.array_split(window, width)):
        if not len(part):
            continue
        high = float(np.clip(part.max() / scale, -1.0, 1.0))
        low = float(np.clip(part.min() / scale, -1.0, 1.0))
        cv2.line(
            strip,
            (x, round(middle - high * middle)),
            (x, round(middle - low * middle)),
            WAVE,
            1,
        )
    cv2.putText(strip, label, (PAD, 11), FONT, FONT_SCALE, LABEL, 1, cv2.LINE_AA)
    cv2.putText(
        strip,
        f"t={elapsed:5.1f} s",
        (width - 62, 11),
        FONT,
        FONT_SCALE,
        LABEL,
        1,
        cv2.LINE_AA,
    )
    return strip
