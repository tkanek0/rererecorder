"""Drawing parts for presentation copies of a recording.

Strips that show the sound and a compass that shows the array's direction,
each drawn onto plain BGR arrays. Where they come from and what they are encoded
into belong to the caller: see ``scripts/render_gif.py`` and
``scripts/render_mp4.py``. Nothing here is a measurement.
"""

from .compass import draw_compass
from .strips import (
    BACKGROUND,
    PAD,
    PLAYHEAD,
    VOLUME_HEIGHT,
    WAVEFORM_HEIGHT,
    loudness_profile,
    volume_strip,
    waveform_scale,
    waveform_strip,
)

__all__ = [
    "BACKGROUND",
    "PAD",
    "PLAYHEAD",
    "VOLUME_HEIGHT",
    "WAVEFORM_HEIGHT",
    "draw_compass",
    "loudness_profile",
    "volume_strip",
    "waveform_scale",
    "waveform_strip",
]
