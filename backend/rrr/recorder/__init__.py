"""Recording both devices at once, into one self-describing session.

Imports no web framework; the CLI and the server both record through this.
"""

from .audio_writer import AudioWriter
from .session import RecorderBusy, SessionRecorder
from .video_writer import VideoWriter

__all__ = [
    "AudioWriter",
    "RecorderBusy",
    "SessionRecorder",
    "VideoWriter",
]
