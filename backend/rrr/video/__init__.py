"""The camera inside the recorder: one shared pipeline, and the archive.

``FrameHub`` shares one :class:`realsense_adapter.LiveSource` between preview
and recording; ``ArchiveWriter`` and ``ArchiveSource`` write and read
``video.rrdb``. The device itself is reached through ``realsense_adapter``,
whose types this package uses but does not re-export. See docs/design.md
"Module boundaries".
"""

from .archive import ArchiveSource, ArchiveWriter
from .hub import FrameHub

__all__ = ["ArchiveSource", "ArchiveWriter", "FrameHub"]
