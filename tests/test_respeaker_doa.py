"""The direction tap's failure handling, with the USB device faked.

A failed device stops the tap until someone reconnects it; nothing retries on
its own. See docs/decisions.md 29.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from respeaker_adapter.doa import DoaTap
from respeaker_adapter.tuning import DeviceNotFound


def _wait_until(ready: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``ready`` until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.01)
    return ready()


class FakeTuning:
    """The two readings the tap asks for, and the close it always makes."""

    direction = 90
    voice_activity = False

    def close(self) -> None:
        pass


@pytest.fixture
def failing(monkeypatch):
    """A tap whose array is not attached, and a count of the attempts."""
    attempts = []

    def find() -> FakeTuning:
        attempts.append(time.monotonic())
        raise DeviceNotFound("no ReSpeaker on the bus")

    monkeypatch.setattr("respeaker_adapter.doa.find", find)
    tap = DoaTap(poll_hz=100.0)
    yield tap, attempts
    tap.shutdown()


def test_a_missing_device_is_not_retried(failing) -> None:
    tap, attempts = failing
    tap.acquire()
    assert _wait_until(lambda: tap.failed)
    tap.acquire()
    time.sleep(0.2)
    assert len(attempts) == 1
    assert not tap.active
    assert tap.error == "no ReSpeaker on the bus"


def test_latest_does_not_wait_while_failed(failing) -> None:
    tap, _ = failing
    tap.acquire()
    assert _wait_until(lambda: tap.failed)
    began = time.monotonic()
    assert tap.latest(timeout=5.0) is None
    assert time.monotonic() - began < 0.5


def test_a_read_that_fails_is_not_retried(monkeypatch) -> None:
    class Breaking(FakeTuning):
        @property
        def direction(self) -> int:
            raise OSError("pipe error")

    opens = []
    monkeypatch.setattr(
        "respeaker_adapter.doa.find", lambda: opens.append(1) or Breaking()
    )
    tap = DoaTap(poll_hz=100.0)
    try:
        tap.acquire()
        assert _wait_until(lambda: tap.failed)
        time.sleep(0.1)
        assert len(opens) == 1
        assert tap.error == "pipe error"
    finally:
        tap.shutdown()


def test_reconnect_polls_again(failing, monkeypatch) -> None:
    tap, _ = failing
    tap.acquire()
    assert _wait_until(lambda: tap.failed)

    monkeypatch.setattr("respeaker_adapter.doa.find", FakeTuning)
    tap.reconnect()
    assert not tap.failed
    reading = tap.latest(timeout=1.0)
    assert reading is not None and reading.angle == 90
