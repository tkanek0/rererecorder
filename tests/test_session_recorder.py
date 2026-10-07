"""Recording a session end to end, with fakes standing in for the devices.

What a recording leaves on disk is checked the way a consumer would check it:
by reading the files back through ``rrr.inspection``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import numpy as np
import pytest
from realsense_adapter import (
    Calibration,
    Extrinsics,
    FrameSet,
    Intrinsics,
    StreamConfig,
)
from rrr.devices import FrameHub
from rrr.devices import audio as audio_module
from rrr.inspection import inspect_session
from rrr.recorder import SessionRecorder
from rrr.timeline import read_events

from .conftest import Device, FakeCapture, FakeSource, FakeTuning

WIDTH, HEIGHT = 32, 24


def _frame_set(calibration: Calibration) -> Callable[[int], FrameSet]:
    def make(n: int) -> FrameSet:
        color = np.full((HEIGHT, WIDTH, 3), n % 255, np.uint8)
        return FrameSet(
            index=n,
            received_monotonic=time.monotonic(),
            color=color,
            depth=None,
            calibration=calibration,
            timestamp_domain="global_time",
        )

    return make


@pytest.fixture
def record(tmp_path, monkeypatch) -> Iterator[Callable[..., tuple]]:
    """Record a short session with the given fake devices, marking it once."""
    intrinsics = Intrinsics(
        width=WIDTH,
        height=HEIGHT,
        fx=16.0,
        fy=16.0,
        ppx=16.0,
        ppy=12.0,
        model="brown_conrady",
        coeffs=(0.0,) * 5,
    )
    calibration = Calibration(
        color=intrinsics,
        depth=intrinsics,
        depth_scale=0.001,
        depth_to_color=Extrinsics.identity(),
        aligned=False,
    )
    opened: list[tuple[SessionRecorder, FrameHub]] = []

    def run(camera: Device, array: Device) -> tuple:
        FakeCapture.device = array
        monkeypatch.setattr(audio_module, "Capture", FakeCapture)
        monkeypatch.setattr("rrr.devices.doa.find_tuning", lambda: FakeTuning(array))
        hub = FrameHub(lambda: FakeSource(camera, _frame_set(calibration)), 0.0)
        recorder = SessionRecorder(
            str(tmp_path),
            streams=StreamConfig(depth=None, infrared=False, motion=False),
            hub=hub,
        )
        opened.append((recorder, hub))
        paths = recorder.start("s")
        time.sleep(1.0)
        recorder.mark("clap", {"n": 1})
        time.sleep(0.5)
        return paths, recorder.stop()

    yield run
    for recorder, hub in opened:
        recorder.close()
        hub.shutdown()


def test_a_session_holds_both_devices_the_direction_and_its_marks(record) -> None:
    paths, manifest = record(Device(), Device())

    assert manifest.errors == []
    assert manifest.video is not None and manifest.video.frames > 10
    assert manifest.audio is not None and manifest.audio.seconds > 1.0
    assert manifest.doa
    assert [event.label for event in read_events(paths.events)] == ["clap"]
    found = inspect_session(paths, manifest)
    assert found.problems == []
    assert found.doa is not None and found.doa["readings"] > 10
    assert found.marks is not None and found.marks["marks"] == 1


def test_a_device_that_fails_is_named_and_the_other_still_records(record) -> None:
    paths, manifest = record(Device(), Device(fail_open=True))

    assert manifest.video is not None and manifest.video.frames > 10
    assert any(error.startswith("audio:") for error in manifest.errors)
    assert "the recorder reported" in " ".join(
        inspect_session(paths, manifest).problems
    )
