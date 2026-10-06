"""The recording format: ``ArchiveWriter`` and ``ArchiveSource`` write and read
``video.rrdb``. See docs/design.md "Module boundaries".
"""

from .archive import ArchiveSource, ArchiveWriter

__all__ = ["ArchiveSource", "ArchiveWriter"]
