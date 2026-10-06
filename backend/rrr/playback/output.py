"""Writing a file derived from a recording without leaving half of one behind."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def replacing(output: Path, overwrite: bool) -> Iterator[Path]:
    """Yield a temporary file beside ``output``, moved into place once complete.

    Args:
        output: Where the file belongs.
        overwrite: Whether an existing ``output`` may be replaced.

    Raises:
        FileExistsError: If ``output`` exists and ``overwrite`` is false.
    """
    if output.exists() and not overwrite:
        raise FileExistsError(f"{output} exists; pass --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    os.chmod(temporary, 0o644)
    try:
        yield temporary
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
