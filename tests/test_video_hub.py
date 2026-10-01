"""The frame hub's failure handling, driven by a source that fails on demand.

A failed hub stays stopped until someone reconnects it. See docs/decisions.md 29.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from rrr.video import FrameHub, StreamError


def _wait_until(ready: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``ready`` until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.01)
    return ready()


@dataclass(frozen=True)
class FakeFrames:
    """The parts of a frame set the hub itself reads."""

    index: int
    received_monotonic: float


class FakeSource:
    """A source whose open fails or succeeds as the test says.

    A successful one delivers a set every 10 ms until the test sets
    ``break_stream``, which makes it fail as a stalled device would.
    """

    def __init__(self, factory: FakeFactory) -> None:
        self._factory = factory
        self._closed = threading.Event()

    def __enter__(self) -> FakeSource:
        self._factory.opens += 1
        if self._factory.fail_open:
            raise StreamError("no RealSense device connected")
        return self

    def __exit__(self, *exc: object) -> None:
        self._closed.set()

    def frames(self) -> Iterator[FakeFrames]:
        index = 0
        while not self._closed.is_set():
            if self._factory.break_stream.wait(0.01):
                raise StreamError("no frames for 5.0s")
            index += 1
            yield FakeFrames(index=index, received_monotonic=time.monotonic())


class FakeFactory:
    """Hands the hub a fresh ``FakeSource`` per open, counting the opens."""

    def __init__(self) -> None:
        self.opens = 0
        self.fail_open = False
        self.break_stream = threading.Event()

    def __call__(self) -> FakeSource:
        return FakeSource(self)


@pytest.fixture
def factory() -> FakeFactory:
    return FakeFactory()


@pytest.fixture
def hub(factory: FakeFactory) -> Iterator[FrameHub]:
    made = FrameHub(factory, idle_shutdown_s=0.0)
    yield made
    made.stop()


def test_a_failed_open_is_not_retried(hub: FrameHub, factory: FakeFactory) -> None:
    factory.fail_open = True
    hub.acquire()
    assert _wait_until(lambda: hub.failed)
    time.sleep(0.2)
    assert factory.opens == 1
    assert not hub.active
    assert hub.error == "no RealSense device connected"


def test_a_new_consumer_does_not_retry_a_failed_hub(
    hub: FrameHub, factory: FakeFactory
) -> None:
    factory.fail_open = True
    hub.acquire()
    assert _wait_until(lambda: hub.failed)
    hub.acquire()
    time.sleep(0.1)
    assert factory.opens == 1


def test_latest_does_not_wait_while_failed(
    hub: FrameHub, factory: FakeFactory
) -> None:
    factory.fail_open = True
    hub.acquire()
    assert _wait_until(lambda: hub.failed)
    began = time.monotonic()
    assert hub.latest(timeout=5.0) is None
    assert time.monotonic() - began < 0.5


def test_a_stream_that_breaks_is_not_reopened(
    hub: FrameHub, factory: FakeFactory
) -> None:
    hub.acquire()
    assert _wait_until(lambda: factory.opens == 1 and hub.active)
    factory.break_stream.set()
    assert _wait_until(lambda: hub.failed)
    time.sleep(0.1)
    assert factory.opens == 1
    assert hub.error == "no frames for 5.0s"


def test_reconnect_opens_again_and_clears_the_error(
    hub: FrameHub, factory: FakeFactory
) -> None:
    factory.fail_open = True
    hub.acquire()
    assert _wait_until(lambda: hub.failed)

    factory.fail_open = False
    hub.reconnect()
    assert not hub.failed
    assert hub.error is None
    assert _wait_until(lambda: factory.opens == 2 and hub.active)


def test_reconnect_with_no_consumer_only_clears_the_failure(
    hub: FrameHub, factory: FakeFactory
) -> None:
    factory.fail_open = True
    hub.acquire()
    assert _wait_until(lambda: hub.failed)
    hub.release()

    hub.reconnect()
    time.sleep(0.1)
    assert not hub.failed
    assert factory.opens == 1, "nobody is waiting, so nothing should open"


def test_restart_also_clears_a_failure(hub: FrameHub, factory: FakeFactory) -> None:
    factory.fail_open = True
    hub.acquire()
    assert _wait_until(lambda: hub.failed)

    factory.fail_open = False
    hub.restart()
    assert _wait_until(lambda: factory.opens == 2 and hub.active)
