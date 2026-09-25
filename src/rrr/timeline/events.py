"""Marks made by a person while a session was recording.

Everything else in a session is a measurement a device made. This is the one
sidecar written by a human: a label, stamped with the instant the mark reached
the recorder, saying what was being done. It exists because a recording of an
experiment is unusable without knowing which part of it was which condition -
"speaker at 45 degrees, two metres" is not recoverable from the audio.

**A mark is accurate to a person's reaction time, not to a sample.** Somebody
presses a button after they notice something, which is a few hundred
milliseconds late and varies. So a mark is for *segmenting* a session into
conditions and for saying what a stretch of it was, never for aligning against
a frame or a sample. When an instant has to be exact, it comes from the signal
itself - an onset in the audio - and the mark only says what that onset was.

JSON lines, like the other sidecars, and flushed on every write. There are a
handful of these in a session rather than thousands, so the cost of flushing is
nothing next to losing the one that says what the recording was of.
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
        monotonic: ``time.monotonic()`` when the mark reached the recorder, on
            the axis every other measurement in the session uses.
        realtime: ``time.time()`` at the same instant, so a mark can be found
            again from a wall-clock note in a lab book.
        label: What the mark means. Short and free text - this repository does
            not interpret it.
        data: Anything else worth recording with the mark, such as the
            conditions of an experimental run. Not interpreted here either:
            what belongs in it depends on the experiment, and fixing a schema
            now would fix the wrong one.
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
            The event. Both clocks are read here rather than passed in, so that
            the stamp is as close to the button press as the process can make
            it.
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
            KeyError: If the time or the label is missing. Unlike the rig, this
                file is written by a program rather than edited by hand, so a
                malformed line is a bug rather than a typo and is worth
                reporting.
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
        Every mark, in the order it was written. An empty list if the file does
        not exist: a session where nobody marked anything is ordinary, not
        broken.

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
