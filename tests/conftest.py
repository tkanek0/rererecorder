"""Synthetic frames and audio, so that nothing here needs a device attached.

Values are measured on the real D455, not round figures: epoch-ms timestamps as
``global_time`` reports them, and frames arriving 8 ms after their timestamp.
"""

from __future__ import annotations

import threading
import time
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Self

import numpy as np
import pytest
from realsense_adapter import (
    Calibration,
    Extrinsics,
    FrameSet,
    Intrinsics,
    StreamConfig,
)
from realsense_adapter.types import MotionSample
from rrr.timeline import (
    AudioClockPoint,
    AudioTrack,
    ClockPair,
    Event,
    JsonlWriter,
    SessionManifest,
    SessionPaths,
    VideoTrack,
    write_manifest,
)
from rrr.video import ArchiveWriter

#: The D455 at its native depth resolution, measured on the device.
WIDTH, HEIGHT = 848, 480
FX, FY = 426.6, 426.2
DEPTH_SCALE = 0.001

#: A real pair of host clock readings. Their large offset makes a forgotten
#: conversion obvious.
MONO = 1_322_228.023434
REAL = 1_788_250_182.059000
OFFSET = REAL - MONO

#: How long after its own timestamp a frame reached the process. Measured 2-11
#: ms on a D455; 8 ms is in the middle of that.
ARRIVAL_LAG_S = 0.008

FPS = 30.0


@pytest.fixture
def intrinsics() -> Intrinsics:
    """Intrinsics of a D455 depth stream at 848x480."""
    return Intrinsics(
        width=WIDTH,
        height=HEIGHT,
        fx=FX,
        fy=FY,
        ppx=WIDTH / 2,
        ppy=HEIGHT / 2,
        model="brown_conrady",
        coeffs=(0.0,) * 5,
    )


@pytest.fixture
def calibration(intrinsics: Intrinsics) -> Calibration:
    """Calibration with depth aligned to color, as a recording would hold it."""
    return Calibration(
        color=intrinsics,
        depth=intrinsics,
        depth_scale=DEPTH_SCALE,
        depth_to_color=Extrinsics.identity(),
        aligned=True,
    )


@pytest.fixture
def make_frames(calibration: Calibration) -> Callable[..., FrameSet]:
    """Return a factory that wraps arrays in a FrameSet with realistic times."""

    def build(
        depth: np.ndarray | None = None,
        color: np.ndarray | None = None,
        index: int = 1,
        timestamp_domain: str = "global_time",
        color_format: str = "rgb8",
        infrared: tuple[np.ndarray, np.ndarray] | None = None,
    ) -> FrameSet:
        """Build a frame set carrying whatever was passed.

        Args:
            depth: Raw uint16 depth, or None.
            color: Color image in ``color_format``, or None.
            index: Frame counter. Also sets the frame's place in time, at 30 fps.
            timestamp_domain: What the timestamp is supposed to mean.
            color_format: ``"rgb8"`` or ``"yuyv"``.
            infrared: The left and right raw images, or None.

        Returns:
            The frame set, with ``received_monotonic`` exactly
            ``MONO + index / FPS + ARRIVAL_LAG_S`` and the same color and
            depth timestamp (no skew).
        """
        capture = index / FPS
        sdk_timestamp_ms = (REAL + capture) * 1000.0
        return FrameSet(
            index=index,
            color_timestamp_ms=sdk_timestamp_ms if color is not None else None,
            depth_timestamp_ms=sdk_timestamp_ms if depth is not None else None,
            received_monotonic=MONO + capture + ARRIVAL_LAG_S,
            color=color,
            depth=depth,
            calibration=calibration,
            timestamp_domain=timestamp_domain,
            color_format=color_format,
            infrared=infrared,
        )

    return build


def wait_until(ready: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``ready`` until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.01)
    return ready()


def write_session(
    paths: SessionPaths,
    *,
    calibration: Calibration,
    config: StreamConfig,
    frames: list[FrameSet],
    audio: np.ndarray | None = None,
    rate: int = 16_000,
    clock_every: int | None = None,
    motion: list[MotionSample] | None = None,
    doa: list[dict[str, object]] | None = None,
    events: list[Event] | None = None,
    clock_anchor: ClockPair | None = None,
    **manifest: object,
) -> SessionPaths:
    """Write a whole session the way the recorder lays one out.

    Args:
        paths: Where, as ``SessionPaths.create`` made it.
        calibration: Stored with the archive.
        config: Stored with the archive.
        frames: Every frame set, in order.
        audio: ``(n, channels)`` int16 for the WAV, or None for no audio.
        rate: The WAV's rate.
        clock_every: Samples between clock points; None for one at each end.
        motion: Inertial samples for the archive.
        doa: Direction readings for ``doa.jsonl``.
        events: Marks for ``events.jsonl``.
        clock_anchor: The archive's anchor; None reads the host's clocks.
        **manifest: Manifest fields beyond the tracks this derives.
    """
    with ArchiveWriter(
        paths.video, calibration=calibration, config=config, clock_anchor=clock_anchor
    ) as writer:
        if motion:
            assert writer.append_motion(motion)
        for frame_set in frames:
            assert writer.append(frame_set, timeout=30.0)
        assert writer.drain()
    first, last = frames[0].received_monotonic, frames[-1].received_monotonic
    tracks: dict[str, object] = {
        "video": VideoTrack(
            frames=len(frames),
            first_monotonic=first,
            last_monotonic=last,
            timestamp_domain="global_time",
            fps=(len(frames) - 1) / (last - first) if last > first else None,
        )
    }
    if audio is not None:
        with wave.open(paths.audio, "wb") as out:
            out.setnchannels(audio.shape[1])
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(audio.astype("<i2").tobytes())
        step = clock_every or len(audio)
        with JsonlWriter(paths.audio_clock) as clock:
            for sample in [*range(0, len(audio), step), len(audio)]:
                clock.append(AudioClockPoint(sample, first + sample / rate).as_dict())
        tracks["audio"] = AudioTrack(
            rate=rate,
            channels=audio.shape[1],
            samples=len(audio),
            first_monotonic=first,
        )
    for path, entries in ((paths.doa, doa), (paths.events, events)):
        if entries:
            with JsonlWriter(path) as sidecar:
                for entry in entries:
                    sidecar.append(
                        entry if isinstance(entry, dict) else entry.as_dict()
                    )
    write_manifest(
        paths,
        SessionManifest(
            session_id=paths.session_id, doa=bool(doa), **{**tracks, **manifest}
        ),
    )
    return paths


#: The small session several converters are tried on: 4 color frames at
#: 10 fps, 8 kHz audio whose processed channel is a ramp, one direction.
SMALL_FRAMES = 4
SMALL_FPS = 10.0
SMALL_RATE = 8_000
SMALL_START = 1_000.0


@pytest.fixture
def small_session(tmp_path) -> SessionPaths:
    intrinsics = Intrinsics(
        width=32,
        height=24,
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
    frames = []
    for n in range(SMALL_FRAMES):
        image = np.zeros((24, 32, 3), dtype=np.uint8)
        image[:, :, n % 3] = 64 + n * 32
        at = SMALL_START + n / SMALL_FPS
        frames.append(
            FrameSet(
                index=n,
                color_timestamp_ms=at * 1_000,
                received_monotonic=at,
                color=image,
                depth=None,
                calibration=calibration,
                timestamp_domain="global_time",
            )
        )
    count = round(SMALL_FRAMES / SMALL_FPS * SMALL_RATE)
    audio = np.zeros((count, 6), dtype="<i2")
    audio[:, 0] = np.arange(count) % 1_000
    return write_session(
        SessionPaths.create(str(tmp_path / "sessions"), "whole"),
        calibration=calibration,
        config=StreamConfig(depth=None, infrared=False),
        frames=frames,
        audio=audio,
        rate=SMALL_RATE,
        doa=[{"t": SMALL_START, "angle": 90, "voice": True}],
    )


# -- fake devices, for what shares and records them ----------------------------

FAKE_RATE = 16_000
FAKE_BLOCK = 256


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


@dataclass(frozen=True)
class FakeFrames:
    index: int


class FakeSource:
    """Delivers a set every 10 ms until closed, or fails as a stalled camera."""

    def __init__(
        self, device: Device, frame: Callable[[int], object] = lambda n: FakeFrames(n)
    ) -> None:
        self._device = device
        self._frame = frame
        self._closed = threading.Event()

    def __enter__(self) -> Self:
        self._device.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self._closed.set()

    def frames(self) -> Iterator[object]:
        n = 0
        while not self._closed.is_set():
            if self._device.broken.wait(0.01):
                raise RuntimeError("no frames for 5.0s")
            n += 1
            yield self._frame(n)


class FakeCapture:
    """Delivers a block per block's worth of time, with contiguous ADC times.

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
        start, n = time.monotonic(), 0
        while not self._stop.wait(FAKE_BLOCK / FAKE_RATE):
            if self.device.broken.is_set():
                continue
            self.last_block_at = time.monotonic()
            block = np.full((FAKE_BLOCK, 6), 0.01 * (n % 7), np.float32)
            self._on_block(block, start + n * FAKE_BLOCK / FAKE_RATE)
            n += 1


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
