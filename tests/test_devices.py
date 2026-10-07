"""Sharing each device, and what happens when it fails, with fakes for the devices.

A failed device stays failed until someone reconnects it (docs/decisions.md 29).
The same rules hold for the camera, the array's audio and its direction, since
all three are one mechanism.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator

import numpy as np
import pytest
from rrr.devices import AudioTap, DoaTap, FrameHub, SharedWorker
from rrr.devices import audio as audio_module

from .conftest import Device, FakeCapture, FakeSource, FakeTuning, wait_until

RATE = 16_000
BLOCK = 256
CHANNELS = 6


# -- each device, faked --------------------------------------------------


def _hub(device: Device, monkeypatch) -> FrameHub:
    return FrameHub(lambda: FakeSource(device), idle_shutdown_s=0.0)


def _audio(device: Device, monkeypatch) -> AudioTap:
    FakeCapture.device = device
    monkeypatch.setattr(audio_module, "Capture", FakeCapture)
    monkeypatch.setattr(audio_module, "STALL_S", 0.1)
    return AudioTap(
        0.0, device="x", rate=RATE, channels=CHANNELS, block_size=BLOCK, window_s=1.0
    )


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


def test_a_device_that_breaks_while_open_is_not_reopened(make) -> None:
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
        tap._on_block(
            np.full((BLOCK, CHANNELS), n, np.float32), start + n * BLOCK / RATE
        )


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


def test_a_stalled_array_fails_the_tap_with_a_reason(monkeypatch) -> None:
    """The PCM can stay RUNNING while nothing arrives; the tap notices itself."""
    device = Device()
    tap = _audio(device, monkeypatch)
    tap.acquire()
    cursor = tap.cursor
    assert tap.stream(cursor, timeout=2.0) is not None
    device.broken.set()

    assert wait_until(lambda: tap.failed)
    assert "no audio" in (tap.error or "")
    assert tap.stream(tap.cursor, timeout=5.0) is None
    tap.shutdown()


@pytest.mark.parametrize(
    ("adapter", "check", "environment"),
    [
        (
            "realsense_adapter",
            "a.DEFAULT_EMITTER == 'on' and a.StreamConfig().emitter == 'on'",
            {"RRR_EMITTER": "off"},
        ),
        (
            "respeaker_adapter",
            "a.DEVICE_NAME == 'ReSpeaker' and a.BLOCK_SIZE == 256",
            {"RRR_AUDIO_DEVICE": "elsewhere", "RRR_AUDIO_BLOCK_SIZE": "1"},
        ),
    ],
)
def test_an_adapter_imports_nothing_from_rrr_and_ignores_the_environment(
    adapter: str, check: str, environment: dict[str, str]
) -> None:
    """A device stays usable without the recorder; rrr passes settings in."""
    code = (
        f"import sys, {adapter} as a;"
        "assert not [m for m in sys.modules if m == 'rrr' or m.startswith('rrr.')];"
        f"assert {check}"
    )
    subprocess.run(
        [sys.executable, "-c", code], check=True, env={**os.environ, **environment}
    )
