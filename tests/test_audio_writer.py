"""Whether a recorded WAV's sample positions still mean capture times.

Audio goes missing in two opposite ways, and the writer has to repair both
without repairing either twice. Each case here is built by hand, written to a
real WAV and a real sidecar, and then read back through
``timeline.AudioTimeline`` - the same path a consumer of the recording takes. A
test that inspected the writer's own counters instead would agree with itself
whatever reached the disk.

No device is involved: the tap is replaced by one that hands over prepared
chunks.
"""

from __future__ import annotations

import threading
import time
import wave
from collections import deque
from dataclasses import dataclass

import numpy as np
import pytest

from rrr.audio.types import BlockStamp, Chunk
from rrr.recorder.audio_writer import AudioWriter
from rrr.timeline import AudioTimeline

RATE = 16_000
BLOCK = 256
CHANNELS = 6
START = 1_322_228.0
BLOCK_S = BLOCK / RATE  # 16 ms


@dataclass
class FakeBlock:
    """One capture block, as a scenario describes it.

    Attributes:
        sample: Absolute index of its first sample in what the tap captured.
        monotonic: ADC time of that sample.
        value: Constant int16 value filling the block, so that blocks can be
            told apart in the written file.
    """

    sample: int
    monotonic: float
    value: int


class FakeTap:
    """An AudioTap-shaped source of prepared chunks.

    Only what :class:`~recorder.audio_writer.AudioWriter` uses is implemented.
    """

    def __init__(self) -> None:
        self.rate = RATE
        self.channels = CHANNELS
        self.block_size = BLOCK
        self.error: str | None = None
        self.overruns = 0
        self.cursor = 0
        self._pending: deque[Chunk] = deque()
        self._exhausted = threading.Event()
        self.acquired = 0

    def acquire(self) -> None:
        self.acquired += 1

    def release(self) -> None:
        self.acquired -= 1

    def queue(self, chunk: Chunk) -> None:
        """Add a chunk for the writer to collect."""
        self._pending.append(chunk)

    def stream(self, cursor: int, timeout: float) -> Chunk | None:
        """Hand over the next prepared chunk, or nothing once they run out."""
        if self._pending:
            return self._pending.popleft()
        self._exhausted.set()
        time.sleep(0.005)
        return None

    def wait_until_drained(self, timeout: float = 5.0) -> None:
        """Block until the writer has asked for a chunk and found none left."""
        assert self._exhausted.wait(timeout), "the writer never drained the tap"


def _chunk(blocks: list[FakeBlock], *, dropped: int = 0) -> Chunk:
    """Build one chunk out of consecutive blocks.

    Args:
        blocks: The blocks it delivers, in order.
        dropped: Samples the ring overwrote before this chunk was collected.

    Returns:
        The chunk, laid out as the real tap would lay it out.
    """
    samples = np.concatenate(
        [
            np.full((BLOCK, CHANNELS), block.value / 32768.0, dtype=np.float32)
            for block in blocks
        ]
    )
    stamps = tuple(
        BlockStamp(sample=block.sample, monotonic=block.monotonic, frames=BLOCK)
        for block in blocks
    )
    return Chunk(
        samples=samples,
        rate=RATE,
        cursor=blocks[-1].sample + BLOCK,
        dropped=dropped,
        captured_at=blocks[-1].monotonic + BLOCK_S,
        stamps=stamps,
    )


def _run(tap: FakeTap, tmp_path) -> tuple[str, str, object]:
    """Run a writer over whatever the tap has queued, and close it.

    Returns:
        The WAV path, the sidecar path, and the final statistics.
    """
    wav = str(tmp_path / "audio.wav")
    clock = str(tmp_path / "audio.clock.jsonl")
    writer = AudioWriter(tap, wav_path=wav, clock_path=clock, clock_interval_s=0.0)
    writer.start()
    tap.wait_until_drained()
    stats = writer.stop(timeout=5.0)
    return wav, clock, stats


def _wav_frames(path: str) -> int:
    """How many frames the written WAV claims to hold."""
    with wave.open(path, "rb") as handle:
        assert handle.getnchannels() == CHANNELS
        assert handle.getframerate() == RATE
        return handle.getnframes()


def _wav_samples(path: str) -> np.ndarray:
    """The written audio as ``(n, channels)`` int16."""
    with wave.open(path, "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").reshape(-1, CHANNELS)


# -- the ordinary case --------------------------------------------------------


def test_continuous_audio_is_written_whole(tmp_path) -> None:
    tap = FakeTap()
    blocks = [FakeBlock(n * BLOCK, START + n * BLOCK_S, n + 1) for n in range(20)]
    for n in range(0, 20, 4):
        tap.queue(_chunk(blocks[n : n + 4]))

    wav, clock, stats = _run(tap, tmp_path)

    assert _wav_frames(wav) == 20 * BLOCK
    assert stats.filled == 0
    assert stats.gaps == 0
    assert stats.first_monotonic == pytest.approx(START, abs=1e-9)

    report = AudioTimeline.read(clock, RATE).report()
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-3)
    assert report.rate_error_ppm == pytest.approx(0.0, abs=1.0)
    assert report.filled == 0


def test_the_written_axis_can_be_queried_by_time(tmp_path) -> None:
    """The point of all of it: an instant maps to a position in the file."""
    tap = FakeTap()
    blocks = [FakeBlock(n * BLOCK, START + n * BLOCK_S, 1) for n in range(60)]
    tap.queue(_chunk(blocks))

    wav, clock, _ = _run(tap, tmp_path)
    timeline = AudioTimeline.read(clock, RATE)

    # Half a second in: 8000 samples, and the file is long enough to hold them.
    assert timeline.sample_at(START + 0.5) == pytest.approx(8_000, abs=1.0)
    assert timeline.monotonic_at(8_000) == pytest.approx(START + 0.5, abs=1e-6)
    assert _wav_frames(wav) > 8_000


def test_the_tap_is_held_only_while_recording(tmp_path) -> None:
    tap = FakeTap()
    tap.queue(_chunk([FakeBlock(0, START, 1)]))
    _run(tap, tmp_path)
    assert tap.acquired == 0


# -- the driver dropped input -------------------------------------------------


def test_a_driver_gap_is_filled_with_exactly_what_was_lost(tmp_path) -> None:
    """The callback is not called for lost audio: time jumps, samples do not.

    Unfilled, every sample after the hole would sit 128 ms early in the file.
    """
    lost_blocks = 8  # 128 ms
    tap = FakeTap()
    blocks = []
    for n in range(20):
        skipped = lost_blocks if n >= 10 else 0
        blocks.append(
            FakeBlock(n * BLOCK, START + (n + skipped) * BLOCK_S, n + 1)
        )
    for n in range(0, 20, 5):
        tap.queue(_chunk(blocks[n : n + 5]))

    wav, clock, stats = _run(tap, tmp_path)

    assert stats.gaps == 1
    assert stats.filled == lost_blocks * BLOCK
    # The file now covers the time it spans, not just the samples that arrived.
    assert _wav_frames(wav) == (20 + lost_blocks) * BLOCK

    report = AudioTimeline.read(clock, RATE).report()
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-3)
    assert report.filled == lost_blocks * BLOCK


def test_the_filled_region_is_silence_and_the_audio_is_not_shifted(
    tmp_path,
) -> None:
    """The repair must be silence, and must go where the hole was."""
    tap = FakeTap()
    blocks = [
        FakeBlock(n * BLOCK, START + (n + (4 if n >= 2 else 0)) * BLOCK_S, n + 1)
        for n in range(4)
    ]
    tap.queue(_chunk(blocks))

    wav, _, _ = _run(tap, tmp_path)
    samples = _wav_samples(wav)

    # Blocks 1 and 2, then four blocks of silence, then blocks 3 and 4.
    assert samples[0, 0] == 1
    assert samples[BLOCK, 0] == 2
    assert np.all(samples[2 * BLOCK : 6 * BLOCK] == 0)
    assert samples[6 * BLOCK, 0] == 3
    assert samples[7 * BLOCK, 0] == 4


def test_jitter_is_not_mistaken_for_a_gap(tmp_path) -> None:
    """0.35 ms of ADC jitter must not put silence in the middle of the audio."""
    rng = np.random.default_rng(20260901)
    tap = FakeTap()
    blocks = [
        FakeBlock(
            n * BLOCK,
            START + n * BLOCK_S + float(rng.normal(0.0, 0.00035)),
            n + 1,
        )
        for n in range(60)
    ]
    tap.queue(_chunk(blocks))

    wav, _, stats = _run(tap, tmp_path)

    assert stats.filled == 0, "jitter is not a hole"
    assert stats.gaps == 0
    assert _wav_frames(wav) == 60 * BLOCK


# -- the reader fell behind ---------------------------------------------------


def test_overwritten_audio_is_filled_by_its_sample_count(tmp_path) -> None:
    """The ring overwrote it: samples jump, capture time was continuous.

    The count is exact here - the tap knows how many samples it captured - so
    the fill must come from the sample numbers and not from the clock.
    """
    tap = FakeTap()
    blocks = [FakeBlock(n * BLOCK, START + n * BLOCK_S, n + 1) for n in range(20)]
    tap.queue(_chunk(blocks[:5]))
    # Blocks 5-9 were captured and overwritten before this reader got to them.
    tap.queue(_chunk(blocks[10:15], dropped=5 * BLOCK))
    tap.queue(_chunk(blocks[15:20]))

    wav, clock, stats = _run(tap, tmp_path)

    assert stats.filled == 5 * BLOCK
    assert stats.dropped_by_reader == 5 * BLOCK
    assert _wav_frames(wav) == 20 * BLOCK, "the file covers the whole span"

    report = AudioTimeline.read(clock, RATE).report()
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-3)


# -- both at once -------------------------------------------------------------


def test_the_two_losses_are_not_filled_twice(tmp_path) -> None:
    """The case the arithmetic exists for.

    A reader that lost 5 blocks also lost the 80 ms they occupied. Counting the
    missing samples *and* the elapsed time would insert 10 blocks of silence for
    5 blocks of loss - and the recording would then be longer than the time it
    covers, which is just as wrong as being shorter.

    Here 5 blocks are overwritten and, separately, the driver dropped 3 more.
    The correct fill is 8 blocks, not 11 and not 16.
    """
    overwritten, undelivered = 5, 3
    tap = FakeTap()
    blocks = []
    for n in range(20):
        # The driver's loss lands after block 14, moving capture time on for
        # everything from block 15.
        skipped = undelivered if n >= 15 else 0
        blocks.append(FakeBlock(n * BLOCK, START + (n + skipped) * BLOCK_S, n + 1))

    tap.queue(_chunk(blocks[:10]))
    tap.queue(_chunk(blocks[15:20], dropped=overwritten * BLOCK))

    wav, clock, stats = _run(tap, tmp_path)

    assert stats.filled == (overwritten + undelivered) * BLOCK
    assert _wav_frames(wav) == (15 + undelivered + 5) * BLOCK

    report = AudioTimeline.read(clock, RATE).report()
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-3), (
        "if the fill were wrong, the sidecar's own points would not lie on a line"
    )


def test_an_unrepaired_recording_would_have_failed_these_checks() -> None:
    """A control: the same scenario without the repair must not pass.

    Without this, a writer that filled nothing could pass the tests above if
    they were only checking self-consistency. Here the file position of every
    block after the hole is what an unfilled writer would produce, and the
    residual is asked to notice.
    """
    lost = 8 * BLOCK
    points = []
    position = 0
    for n in range(20):
        skipped = lost / RATE if n >= 10 else 0.0
        points.append((position, START + n * BLOCK_S + skipped))
        position += BLOCK

    from rrr.timeline.audio_clock import AudioClockPoint

    report = AudioTimeline(
        [AudioClockPoint(sample=s, monotonic=t) for s, t in points], RATE
    ).report()
    assert report.residual_max_ms > 50.0, "an unfilled hole must be visible"


# -- accounting ---------------------------------------------------------------


def test_a_tap_error_is_reported_not_raised(tmp_path) -> None:
    """A device that goes away must not take the process with it."""
    tap = FakeTap()
    tap.queue(_chunk([FakeBlock(0, START, 1)]))
    tap.error = "device disconnected"

    wav, _, stats = _run(tap, tmp_path)

    assert stats.error == "device disconnected"
    # And what had arrived before the failure is still on disk.
    assert _wav_frames(wav) == BLOCK
