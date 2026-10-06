"""The devices, each read on one thread and shared by every consumer.

:class:`SharedWorker` is the one mechanism: reference counted, closed after an
idle period, failures reported and never retried until a reconnect
(docs/decisions.md 29). The camera, the array's audio and its direction are
each a thin :class:`SharedWorker` over a primitive from the device adapters.
"""

from .audio import AudioTap
from .camera import FrameHub
from .doa import DoaTap, Reading
from .worker import SharedWorker

__all__ = ["AudioTap", "DoaTap", "FrameHub", "Reading", "SharedWorker"]
