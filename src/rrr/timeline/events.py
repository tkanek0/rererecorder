"""Marks made by a person while a session was recording.

JSON lines, flushed on every write. **A mark is accurate to a person's
reaction time, not to a sample**: never align against it. See
docs/decisions.md 16.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

#: Suffix of the sidecar this module writes.
SUFFIX = ".jsonl"


@dataclass(frozen=True)
class Event:
    """One mark, and when it was made.

    Attributes:
        monotonic: ``time.monotonic()`` when the mark reached the recorder.
        realtime: ``time.time()`` at the same instant.
        label: What the mark means; free text, not interpreted.
        data: Anything else worth recording, such as run conditions; free
            form, not interpreted.
    """

    monotonic: float
    realtime: float
    label: str
    data: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def now(label: str, data: dict[str, Any] | None = None) -> Event:
        """Stamp a mark with the current time.

        Args:
            label: What the mark means.
            data: Anything else worth recording with it.

        Returns:
            The event, with both clocks read here.
        """
        return Event(
            monotonic=time.monotonic(),
            realtime=time.time(),
            label=label,
            data=dict(data or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this event."""
        return {
            "monotonic": self.monotonic,
            "realtime": self.realtime,
            "label": self.label,
            "data": dict(self.data),
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Event:
        """Rebuild an event from its stored form.

        Args:
            raw: A mapping as :meth:`as_dict` produced.

        Returns:
            The event.

        Raises:
            KeyError: If the time or the label is missing.
        """
        data = raw.get("data")
        return Event(
            monotonic=float(raw["monotonic"]),
            realtime=float(raw["realtime"]),
            label=str(raw["label"]),
            data=dict(data) if isinstance(data, dict) else {},
        )


class EventWriter:
    """Appends marks beside a recording, one JSON object per line."""

    def __init__(self, path: str) -> None:
        """Open the sidecar for writing.

        Args:
            path: File to create. Overwritten if it exists.
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._handle = open(path, "w", encoding="utf-8")
        self._path = path
        self._count = 0

    def __enter__(self) -> EventWriter:
        """Return the open writer."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the sidecar."""
        self.close()

    @property
    def path(self) -> str:
        """Where the sidecar is being written."""
        return self._path

    @property
    def count(self) -> int:
        """How many marks have been written."""
        return self._count

    def append(self, event: Event) -> None:
        """Write one mark, and flush it.

        Args:
            event: The mark to record.
        """
        self._handle.write(json.dumps(event.as_dict()) + "\n")
        self._handle.flush()
        self._count += 1

    def close(self) -> None:
        """Flush and close."""
        if not self._handle.closed:
            self._handle.close()


def read_events(path: str) -> list[Event]:
    """Read a sidecar back.

    Args:
        path: The file to read.

    Returns:
        Every mark, in the order it was written; an empty list if the file
        does not exist.

    Raises:
        ValueError: If a line is not a readable event.
    """
    if not os.path.exists(path):
        return []
    events: list[Event] = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"{path} line {number}: {error}") from error
    return events
