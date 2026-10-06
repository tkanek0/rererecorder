"""The sidecars beside a recording: one JSON object per line."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, Self, TypeVar

T = TypeVar("T")


class JsonlWriter:
    """Appends JSON objects to a file, each on disk before the next is written."""

    def __init__(self, path: str) -> None:
        """Create the file, and its directory if needed. An existing file is replaced.

        Args:
            path: File to write.
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # Held open across appends until close(), so not a with block.
        self._handle = open(path, "w", encoding="utf-8")
        self._count = 0

    def __enter__(self) -> Self:
        """Return the open writer."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the file."""
        self.close()

    @property
    def count(self) -> int:
        """How many objects have been written."""
        return self._count

    def append(self, entry: dict[str, Any]) -> None:
        """Write one object and flush it."""
        self._handle.write(json.dumps(entry) + "\n")
        self._handle.flush()
        self._count += 1

    def close(self) -> None:
        """Close the file; closing twice is harmless."""
        if not self._handle.closed:
            self._handle.close()


def read_jsonl(path: str, parse: Callable[[dict[str, Any]], T]) -> list[T]:
    """Read every object back, skipping blank lines.

    Args:
        path: The file to read.
        parse: Builds a value from one object; a ``KeyError``, ``TypeError`` or
            ``ValueError`` it raises is reported against its line.

    Returns:
        The values, in file order.

    Raises:
        FileNotFoundError: If there is no such file.
        ValueError: If a line is not JSON, or ``parse`` refuses it.
    """
    values: list[T] = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                values.append(parse(json.loads(line)))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path} line {number}: {error}") from error
    return values
