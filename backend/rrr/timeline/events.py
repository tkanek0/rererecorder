"""Marks made by a person while a session was recording.

JSON lines (:class:`~rrr.timeline.jsonl.JsonlWriter`), flushed on every write. **A mark is accurate to a person's
reaction time, not to a sample**: never align against it. See
docs/decisions.md 16.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .jsonl import read_jsonl


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


def read_events(path: str) -> list[Event]:
    """Read the marks back, in the order they were made.

    Args:
        path: The sidecar.

    Returns:
        The marks; none if nobody marked anything, so there is no file.

    Raises:
        ValueError: If a line is not a readable mark.
    """
    try:
        return read_jsonl(path, Event.from_dict)
    except FileNotFoundError:
        return []
