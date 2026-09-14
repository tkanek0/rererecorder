"""The video writer, driven by a hub that hands over prepared frames.

No camera. The hub is replaced by one that publishes on demand, which is enough
to exercise the part that matters: every set the hub delivers must reach the
archive, and the counters must survive being asked for after the recording has
stopped.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
import pytest

from rrr.recorder.video_writer import VideoWriter
from rrr.video import ArchiveSource, Calibration, FrameSet, StreamConfig

from .conftest import HEIGHT, WIDTH

# The frames are full size, matching the conftest calibration. They have to be:
# a zlib depth blob carries no dimensions, so the reader takes the shape from
# the recording's calibration and a smaller array would not read back.


class FakeHub:
    """A FrameHub-shaped source of frames published by the test.

    Only what :class:`VideoWriter` uses is implemented.
    """

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
    """The counters live in the archive, which stop() closes.

    An earlier version dropped its reference to the writer before copying them
    out, so anything reading the statistics afterwards - which is exactly what
    the manifest does - saw whatever the last poll happened to have caught.
    Measured 210 of 240 frames on a real recording.
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
    """Counted asynchronously: append queues, and the archive's thread writes.

    So this waits for the queue rather than reading straight after publishing -
    which is also how the manifest sees it, one second at a time.
    """
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
    """A recording is a consumer of the camera in its own right.

    On the preview's reference count instead, closing the last browser tab
    would stop a recording.
    """
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
