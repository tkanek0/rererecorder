"""Recording both devices at once, into one self-describing session.

Depends on :mod:`video`, :mod:`audio` and :mod:`timeline`, and on no web
framework: the CLI records without a server running, and the server records
through exactly this code rather than a copy of it.
"""

from .audio_writer import AudioStats, AudioWriter
from .session import RecorderBusy, SessionRecorder
from .video_writer import VideoStats, VideoWriter

__all__ = [
    "AudioStats",
    "AudioWriter",
    "RecorderBusy",
    "SessionRecorder",
    "VideoStats",
    "VideoWriter",
]
