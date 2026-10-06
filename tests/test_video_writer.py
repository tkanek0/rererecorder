"""The video writer, driven by a hub that hands over prepared frames.

Every set the hub delivers must reach the archive, and the counters must
survive stop(). The hub is faked; no camera.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
import pytest
from realsense_adapter import Calibration, FrameSet, StreamConfig
from rrr.recorder.video_writer import VideoWriter
from rrr.video import ArchiveSource

from .conftest import HEIGHT, WIDTH

# Frames are full size: zlib depth takes its shape from the calibration, so a
# smaller array would not read back.


class FakeHub:
    """A FrameHub-shaped source of frames published by the test."""

    def __init__(self, calibration: Calibration) -> None:
        self.calibration = calibration
        self.device = None
        self.error: str | None = None
        self.source = None
        self.acquired = 0
        self._listeners: list[Callable[[FrameSet], None]] = []
        self._latest: FrameSet | None = None
        #: Set to make latest() report nothing, as an unplugged camera does.
        self.deliver_first = True

    def acquire(self) -> None:
        self.acquired += 1

    def release(self) -> None:
        self.acquired -= 1

    def latest(self, timeout: float = 5.0, after: int = 0) -> FrameSet | None:
        return self._latest if self.deliver_first else None

    def add_listener(self, listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def publish(self, frames: FrameSet) -> None:
        """Deliver one set, as the hub's reader thread would."""
        self._latest = frames
        for listener in list(self._listeners):
            listener(frames)

    def seed(self, frames: FrameSet) -> None:
        """Make a set available to latest() without calling any listener."""
        self._latest = frames


@pytest.fixture
def sets(make_frames: Callable[..., FrameSet]) -> Callable[[int], list[FrameSet]]:
    """Small frame sets, cheap enough to write a few dozen of."""

    def build(count: int) -> list[FrameSet]:
        rng = np.random.default_rng(20260902)
        return [
            make_frames(
                index=i + 1,
                depth=rng.integers(0, 4096, (HEIGHT, WIDTH), dtype=np.uint16),
            )
            for i in range(count)
        ]

    return build


@pytest.fixture
def hub(calibration: Calibration, sets) -> FakeHub:
    fake = FakeHub(calibration)
    fake.seed(sets(1)[0])
    return fake


# -- nothing is lost ----------------------------------------------------------


def test_every_published_set_is_written(tmp_path, hub, sets) -> None:
    """A listener sees every frame; that is why recording uses one."""
    path = str(tmp_path / "video.rrdb")
    writer = VideoWriter(hub, path, config=StreamConfig())
    writer.start(timeout=1.0)
    originals = sets(12)
    for frames in originals:
        hub.publish(frames)
    writer.stop(timeout=10.0)

    with ArchiveSource(path) as archive:
        restored = list(archive.frames())
    assert len(restored) == 12
    for original, back in zip(originals, restored, strict=True):
        assert np.array_equal(back.depth, original.depth)


def test_the_frame_count_survives_being_asked_after_stop(tmp_path, hub, sets) -> None:
    """The counters live in the archive, which stop() closes, and must be copied first.

    A regression once left the manifest with 210 of 240 frames.
    """
    path = str(tmp_path / "video.rrdb")
    writer = VideoWriter(hub, path, config=StreamConfig())
    writer.start(timeout=1.0)
    for frames in sets(12):
        hub.publish(frames)

    returned = writer.stop(timeout=10.0)

    assert returned.frames == 12
    assert writer.stats.frames == 12, "and still 12 when asked again later"
    assert writer.stats.dropped == 0


def test_stats_track_the_recording_while_it_runs(tmp_path, hub, sets) -> None:
    """Counted asynchronously, so this waits for the archive's queue to drain."""
    path = str(tmp_path / "video.rrdb")
    writer = VideoWriter(hub, path, config=StreamConfig())
    writer.start(timeout=1.0)
    for frames in sets(10):
        hub.publish(frames)

    deadline = time.monotonic() + 5.0
    while writer.stats.frames < 10 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert writer.stats.frames == 10, "the queue should have drained by now"
    writer.stop(timeout=10.0)


# -- the time axis ------------------------------------------------------------


def test_the_span_and_rate_come_from_received_monotonic(tmp_path, hub, sets) -> None:
    """Not the mean of one-over-interval, which jitter biases high."""
    from .conftest import FPS

    path = str(tmp_path / "video.rrdb")
    writer = VideoWriter(hub, path, config=StreamConfig())
    writer.start(timeout=1.0)
    published = sets(16)
    for frames in published:
        hub.publish(frames)
    stats = writer.stop(timeout=10.0)

    assert stats.first_monotonic == pytest.approx(published[0].received_monotonic)
    assert stats.last_monotonic == pytest.approx(published[-1].received_monotonic)
    assert stats.span_s == pytest.approx(15 / FPS, abs=1e-6)
    assert stats.fps == pytest.approx(FPS, abs=1e-6)


# -- holding the camera -------------------------------------------------------


def test_the_hub_is_held_for_exactly_as_long_as_the_archive(tmp_path, hub, sets) -> None:
    """A recording holds the camera, so closing the last preview cannot stop it."""
    path = str(tmp_path / "video.rrdb")
    writer = VideoWriter(hub, path, config=StreamConfig())
    assert hub.acquired == 0
    writer.start(timeout=1.0)
    assert hub.acquired == 1
    writer.stop(timeout=10.0)
    assert hub.acquired == 0


def test_a_camera_that_never_delivers_releases_the_hub(tmp_path, hub) -> None:
    """Refusing to start must not leave the camera held by nobody."""
    hub.deliver_first = False
    writer = VideoWriter(hub, str(tmp_path / "video.rrdb"), config=StreamConfig())

    with pytest.raises(RuntimeError, match="no frames"):
        writer.start(timeout=0.01)
    assert hub.acquired == 0
    assert writer.running is False


def test_stopping_detaches_the_listener(tmp_path, hub, sets) -> None:
    """Frames published after a recording ends must not reach a closed archive."""
    path = str(tmp_path / "video.rrdb")
    writer = VideoWriter(hub, path, config=StreamConfig())
    writer.start(timeout=1.0)
    for frames in sets(5):
        hub.publish(frames)
    stats = writer.stop(timeout=10.0)

    for frames in sets(5):
        hub.publish(frames)  # must be harmless
    assert writer.stats.frames == stats.frames


def test_starting_twice_is_refused(tmp_path, hub) -> None:
    writer = VideoWriter(hub, str(tmp_path / "video.rrdb"), config=StreamConfig())
    writer.start(timeout=1.0)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            writer.start(timeout=1.0)
    finally:
        writer.stop(timeout=10.0)


def test_only_inertial_samples_from_the_recording_are_written(
    tmp_path, hub, sets
) -> None:
    """The sensor buffers seconds of samples; those from before the start are not this session's."""
    from realsense_adapter.types import MotionSample

    class Source:
        def __init__(self) -> None:
            self.buffered = [MotionSample("accel", 1.0, 0.0, 9.8, 0.0)]

        def drain_motion(self) -> list[MotionSample]:
            taken, self.buffered = self.buffered, []
            return taken

    hub.source = Source()
    path = str(tmp_path / "video.rrdb")
    writer = VideoWriter(hub, path, config=StreamConfig(color=None))
    writer.start()
    hub.source.buffered.append(MotionSample("accel", 2.0, 0.0, 9.8, 0.0))
    hub.publish(sets(1)[0])
    stats = writer.stop()

    assert stats.motion == 1
    with ArchiveSource(path) as archive:
        assert [s.timestamp_ms for s in archive.motion_samples()] == [2.0]
