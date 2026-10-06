"""Measuring the offset between the camera's clock and the array's.

From handclaps: an impulse in the audio, matched to the sharpest movement in
the video. Measures and reports; whether to store the result in
``session.json`` belongs to the caller - see ``scripts/calibrate.py``.
"""

from .handclap import OffsetError, measure_offset

__all__ = ["OffsetError", "measure_offset"]
