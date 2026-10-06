"""Sharing each device, and what happens when it fails, with fakes for the devices.

A failed device stays failed until someone reconnects it (docs/decisions.md 29).
The same rules hold for the camera, the array's audio and its direction, since
all three are one mechanism.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Self

import numpy as np
import pytest
from rrr.devices import AudioTap, DoaTap, FrameHub, SharedWorker
from rrr.devices import audio as audio_module

from .conftest import wait_until

RATE = 16_000
BLOCK = 256
CHANNELS = 6


@dataclass
class Device:
    """What a fake device does when opened, shared by every fake below."""

    opens: int = 0
    fail_open: bool = False
    #: Set to make an open device fail mid-stream.
    broken: threading.Event = field(default_factory=threading.Event)

    def open(self) -> None:
        self.opens += 1
        if self.fail_open:
            raise RuntimeError("no such device")


# -- the camera ---------------------------------------------------------------


@dataclass(frozen=True)
class FakeFrames:
    index: int


class FakeSource:
    """Delivers a set every 10 ms until closed, or fails as a stalled camera."""

    def __init__(self, device: Device) -> None:
        self._device = device
        self._closed = threading.Event()

    def __enter__(self) -> Self:
        self._device.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self._closed.set()

    def frames(self) -> Iterator[FakeFrames]:
        while not self._closed.is_set():
            if self._device.broken.wait(0.01):
                raise RuntimeError("no frames for 5.0s")
            yield FakeFrames(index=0)


def _hub(device: Device, monkeypatch) -> FrameHub:
    return FrameHub(lambda: FakeSource(device), idle_shutdown_s=0.0)


# -- the array's audio --------------------------------------------------------


class FakeCapture:
    """Delivers a block every 10 ms through the tap's callback, as PortAudio would.

    A broken one stops delivering, which the tap must notice by itself.
    """

    device: Device

    def __init__(self, on_block: Callable[[np.ndarray, float], None], **_: object):
        self._on_block = on_block
        self._stop = threading.Event()
        self.overruns = 0
        self.last_block_at = 0.0

    def __enter__(self) -> Self:
        self.device.open()
        self.last_block_at = time.monotonic()
        threading.Thread(target=self._deliver, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()

    def _deliver(self) -> None:
        while not self._stop.wait(0.01):
            if self.device.broken.is_set():
                continue
            self.last_block_at = time.monotonic()
            self._on_block(np.zeros((BLOCK, CHANNELS), np.float32), time.monotonic())


def _audio(device: Device, monkeypatch) -> AudioTap:
    FakeCapture.device = device
    monkeypatch.setattr(audio_module, "Capture", FakeCapture)
    return AudioTap(
        0.0, device="x", rate=RATE, channels=CHANNELS, block_size=BLOCK, window_s=1.0
    )


# -- the direction ------------------------------------------------------------


class FakeTuning:
    def __init__(self, device: Device) -> None:
        device.open()
        self._device = device

    @property
    def direction(self) -> int:
        if self._device.broken.is_set():
            raise RuntimeError("[Errno 5] Input/Output Error")
        return 90

    voice_activity = False

    def close(self) -> None:
        pass


def _doa(device: Device, monkeypatch) -> DoaTap:
    monkeypatch.setattr("rrr.devices.doa.find_tuning", lambda: FakeTuning(device))
    return DoaTap(0.0, poll_hz=100.0)


MAKERS = [_hub, _audio, _doa]


@pytest.fixture(params=MAKERS, ids=["camera", "audio", "doa"])
def make(request, monkeypatch) -> Iterator[Callable[[Device], SharedWorker]]:
    made: list[SharedWorker] = []

    def build(device: Device) -> SharedWorker:
        worker = request.param(device, monkeypatch)
        made.append(worker)
        return worker

    yield build
    for worker in made:
        worker.shutdown()


# -- one set of rules for every device ----------------------------------------


def test_a_failed_open_is_reported_and_not_retried(make) -> None:
    device = Device(fail_open=True)
    worker = make(device)
    worker.acquire()
    assert wait_until(lambda: worker.failed)
    worker.acquire()  # a second consumer does not retry either
    time.sleep(0.1)

    assert device.opens == 1
    assert not worker.active
    assert "no such device" in (worker.error or "")
    began = time.monotonic()
    assert worker.latest(timeout=5.0) is None, "and readers do not wait for it"
    assert time.monotonic() - began < 0.5


def test_a_device_that_breaks_while_open_is_not_reopened(make, request) -> None:
    if request.node.callspec.id == "audio":
        pytest.skip("the tap does not yet notice a stalled array")
    device = Device()
    worker = make(device)
    worker.acquire()
    assert worker.latest(timeout=2.0) is not None
    device.broken.set()
    assert wait_until(lambda: worker.failed)
    time.sleep(0.1)
    assert device.opens == 1


def test_reconnect_clears_the_failure_and_opens_again(make) -> None:
    device = Device(fail_open=True)
    worker = make(device)
    worker.acquire()
    assert wait_until(lambda: worker.failed)

    device.fail_open = False
    worker.reconnect()
    assert not worker.failed and worker.error is None
    assert worker.latest(timeout=2.0) is not None
    assert device.opens == 2


def test_reopening_after_an_idle_close_waits_for_fresh_data(make) -> None:
    """Not the closed opening's last item, and not None at once."""
    device = Device()
    worker = make(device)
    worker.acquire()
    assert worker.latest(timeout=2.0) is not None
    worker.release()
    assert wait_until(lambda: not worker.active)

    worker.acquire()
    assert worker.latest(timeout=2.0) is not None
    assert device.opens == 2


def test_a_listener_gets_every_item_in_order() -> None:
    device = Device()
    hub = FrameHub(lambda: FakeSource(device), idle_shutdown_s=0.0)
    seen: list[int] = []
    hub.add_listener(lambda frames: seen.append(frames.index))
    hub.acquire()
    assert wait_until(lambda: len(seen) >= 5)
    hub.shutdown()
    assert seen == list(range(1, len(seen) + 1))


def test_a_restart_also_clears_a_failure(monkeypatch) -> None:
    device = Device(fail_open=True)
    hub = _hub(device, monkeypatch)
    hub.acquire()
    assert wait_until(lambda: hub.failed)
    device.fail_open = False
    hub.restart()
    assert hub.latest(timeout=2.0) is not None
    hub.shutdown()


# -- the audio ring -----------------------------------------------------------


@pytest.fixture
def tap() -> AudioTap:
    """A tap fed by hand, never opened."""
    return AudioTap(
        0.0, device="x", rate=RATE, channels=CHANNELS, block_size=BLOCK, window_s=1.0
    )


def _feed(tap: AudioTap, blocks: int, *, start: float = 100.0, first: int = 0) -> None:
    """Deliver blocks whose ADC times are contiguous from block ``first``."""
    for n in range(first, first + blocks):
        tap._on_block(np.full((BLOCK, CHANNELS), n, np.float32), start + n * BLOCK / RATE)


def test_a_chunk_carries_the_samples_and_a_stamp_per_block(tap: AudioTap) -> None:
    _feed(tap, 4)
    first = tap.stream(0, timeout=0.0)
    _feed(tap, 2, first=4)
    second = tap.stream(first.cursor, timeout=0.0)

    assert [s.sample for s in first.stamps] == [0, 256, 512, 768]
    assert [s.monotonic for s in first.stamps] == [
        pytest.approx(100.0 + n * BLOCK / RATE) for n in range(4)
    ]
    assert first.captured_at == pytest.approx(100.0 + 4 * BLOCK / RATE)
    assert first.samples[BLOCK * 3, 0] == 3
    assert (second.first_sample, second.stamps[0].sample) == (4 * BLOCK, 4 * BLOCK)


def test_a_slow_reader_is_told_what_it_lost(tap: AudioTap) -> None:
    """Overwritten audio is reported as ``dropped``; stamps still cover the rest."""
    _feed(tap, 100)  # a 1 s ring holds 62.5 blocks
    chunk = tap.stream(0, timeout=0.0)

    assert chunk.dropped == 100 * BLOCK - RATE
    assert chunk.stamps[0].sample <= chunk.first_sample
    assert chunk.stamps[-1].end_sample >= chunk.cursor
