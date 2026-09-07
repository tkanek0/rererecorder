"""The tap's time stamping, driven by fake PortAudio callbacks.

No device is opened. The callback is what PortAudio would call, so calling it
directly exercises the whole stamping path - which is the part this repository
changed and the part a recording's time axis depends on.

Importing this does load PortAudio itself, because :mod:`audio.capture` does. A
missing libportaudio2 is an environment problem and says so at import; a missing
array is not, and nothing here needs one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from rrr.audio.capture import AudioTap

RATE = 16_000
BLOCK = 256
CHANNELS = 6
#: Where the fake ADC clock starts. Near a real monotonic reading so that a
#: comparison against time.monotonic() inside the tap behaves as it would live.
START = 1_322_228.0


@dataclass
class FakeTimeInfo:
    """What PortAudio hands a callback."""

    inputBufferAdcTime: float  # noqa: N815 - PortAudio's own spelling
    currentTime: float = 0.0  # noqa: N815


@dataclass
class FakeStatus:
    """PortAudio's callback flags."""

    input_overflow: bool = False


@pytest.fixture
def tap() -> AudioTap:
    """A tap that has never opened a device."""
    return AudioTap(rate=RATE, channels=CHANNELS, block_size=BLOCK, window_s=1.0)


def _feed(
    tap: AudioTap,
    blocks: int,
    *,
    first_block: int = 0,
    adc_start: float = START,
    gap_after: int | None = None,
    gap_blocks: int = 0,
    value: float | None = None,
) -> None:
    """Push blocks through the callback as PortAudio would.

    Args:
        tap: The tap to feed.
        blocks: How many blocks to deliver.
        first_block: Block number the ADC times start counting from.
        adc_start: ADC time of block zero.
        gap_after: Deliver a gap after this many blocks, or None for none.
        gap_blocks: How many blocks' worth of audio the gap swallows. The
            callback is simply not called for them - which is what a driver
            overflow looks like - so the sample count stays continuous while the
            ADC time jumps.
        value: Constant sample value, or None for a per-block ramp that makes
            the blocks distinguishable.
    """
    skipped = 0
    for n in range(blocks):
        if gap_after is not None and n == gap_after:
            skipped = gap_blocks
        index = first_block + n + skipped
        samples = np.full(
            (BLOCK, CHANNELS), n if value is None else value * 32768.0, dtype=np.int16
        )
        tap._callback(
            samples,
            BLOCK,
            FakeTimeInfo(inputBufferAdcTime=adc_start + index * BLOCK / RATE),
            FakeStatus(),
        )


# -- the stamps ---------------------------------------------------------------


def test_a_chunk_carries_a_stamp_per_block(tap: AudioTap) -> None:
    _feed(tap, 4)
    chunk = tap.stream(0, timeout=0.0)

    assert chunk is not None
    assert len(chunk.stamps) == 4
    assert [stamp.sample for stamp in chunk.stamps] == [0, 256, 512, 768]
    assert [stamp.frames for stamp in chunk.stamps] == [BLOCK] * 4


def test_stamps_hold_the_adc_time_not_the_callback_time(tap: AudioTap) -> None:
    """The whole change: a block is timed by when its audio was converted.

    The callback runs one block late in reality, and by whatever the scheduler
    adds on top. Using ``time.monotonic()`` here - as respeaker-playground does -
    would report START + a real elapsed time, not START.
    """
    _feed(tap, 3)
    chunk = tap.stream(0, timeout=0.0)

    for n, stamp in enumerate(chunk.stamps):
        assert stamp.monotonic == pytest.approx(START + n * BLOCK / RATE, abs=1e-9)


def test_captured_at_is_the_newest_samples_time(tap: AudioTap) -> None:
    """Not the block's first sample, and not when the callback ran."""
    _feed(tap, 4)
    chunk = tap.stream(0, timeout=0.0)

    assert chunk.captured_at == pytest.approx(START + 4 * BLOCK / RATE, abs=1e-9)


def test_first_sample_locates_the_chunk_in_the_stream(tap: AudioTap) -> None:
    _feed(tap, 4)
    first = tap.stream(0, timeout=0.0)
    _feed(tap, 2, first_block=4)
    second = tap.stream(first.cursor, timeout=0.0)

    assert first.first_sample == 0
    assert second.first_sample == 4 * BLOCK
    assert second.stamps[0].sample == 4 * BLOCK


def test_a_stamp_predicts_where_the_next_block_starts(tap: AudioTap) -> None:
    """end_monotonic is what a gap is measured against."""
    _feed(tap, 2)
    chunk = tap.stream(0, timeout=0.0)

    assert chunk.stamps[0].end_monotonic(RATE) == pytest.approx(
        chunk.stamps[1].monotonic, abs=1e-9
    )
    assert chunk.stamps[0].end_sample == chunk.stamps[1].sample


# -- the two ways audio goes missing ------------------------------------------


def test_a_driver_gap_shows_as_a_jump_in_time_and_not_in_samples(
    tap: AudioTap,
) -> None:
    """An input overflow: the callback is not called for what was lost.

    So the sample count stays continuous - the tap never saw those samples - and
    only the ADC time reveals the hole. A recorder that watched sample counts
    alone would write a file 128 ms shorter than the time it covers, with no
    trace of why.
    """
    _feed(tap, 6, gap_after=3, gap_blocks=8)  # 8 blocks = 128 ms
    chunk = tap.stream(0, timeout=0.0)

    stamps = chunk.stamps
    assert [stamp.sample for stamp in stamps] == [n * BLOCK for n in range(6)]
    assert stamps[3].sample == stamps[2].end_sample, "samples are continuous"

    lost = stamps[3].monotonic - stamps[2].end_monotonic(RATE)
    assert lost == pytest.approx(8 * BLOCK / RATE, abs=1e-9)
    assert lost == pytest.approx(0.128, abs=1e-6)


def test_a_slow_reader_shows_as_a_jump_in_samples(tap: AudioTap) -> None:
    """Overwritten audio: the tap captured it, the reader never collected it.

    Reported as ``dropped`` and visible as a gap between the cursor asked for
    and the first sample delivered - a different failure from the one above, and
    it has to stay a different one.
    """
    _feed(tap, 8)  # a 1 s window at 16 kHz holds 62.5 blocks, so nothing is lost
    tap._capacity = 4 * BLOCK  # shrink the window instead of feeding 60 blocks
    tap._ring = np.zeros((tap._capacity, CHANNELS), dtype=np.float32)
    tap._position = 0
    _feed(tap, 8, first_block=8)

    chunk = tap.stream(0, timeout=0.0)
    assert chunk.dropped > 0
    assert chunk.first_sample > 0


def test_stamps_cover_the_samples_a_chunk_actually_holds(tap: AudioTap) -> None:
    """Every sample delivered must have a stamp that covers it.

    Otherwise a recorder cannot time the start of the chunk, which is exactly
    the case that appears when the ring has wrapped.
    """
    _feed(tap, 100)  # more than the 62.5 blocks a 1 s window holds
    chunk = tap.stream(0, timeout=0.0)

    assert chunk.stamps, "a wrapped ring must still deliver stamps"
    assert chunk.stamps[0].sample <= chunk.first_sample
    assert chunk.stamps[-1].end_sample >= chunk.cursor


# -- when PortAudio does not fill the field in -------------------------------


def test_a_missing_adc_time_falls_back_to_the_callback_clock(
    tap: AudioTap, caplog
) -> None:
    """Some host APIs report zero. The recording should degrade, not lie."""
    import time

    before = time.monotonic()
    tap._callback(
        np.zeros((BLOCK, CHANNELS), dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=0.0),
        FakeStatus(),
    )
    after = time.monotonic()

    chunk = tap.stream(0, timeout=0.0)
    stamp = chunk.stamps[0]
    # One block before the callback ran, which is where the audio started.
    assert before - BLOCK / RATE <= stamp.monotonic <= after
    assert "inputBufferAdcTime" in caplog.text


def test_a_clock_with_the_wrong_origin_is_reported(tap: AudioTap, caplog) -> None:
    """A reported time from another clock must not pass silently.

    This is the failure that would put every audio sample somewhere else
    entirely, and nothing downstream could detect it - the times would look
    perfectly self-consistent.
    """
    tap._callback(
        np.zeros((BLOCK, CHANNELS), dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=1.0),  # as if since stream start
        FakeStatus(),
    )
    assert "not the same" in caplog.text


def test_a_plausible_adc_time_is_accepted_quietly(tap: AudioTap, caplog) -> None:
    import time

    tap._callback(
        np.zeros((BLOCK, CHANNELS), dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=time.monotonic() - BLOCK / RATE),
        FakeStatus(),
    )
    assert "not the same" not in caplog.text


def test_the_clock_check_runs_once(tap: AudioTap, caplog) -> None:
    """It compares against time.monotonic(), which drifts from a fake ADC clock.

    Checking every block would therefore warn constantly on a recording that is
    fine. Once is enough: the two clocks either share an origin or they do not.
    """
    import time

    now = time.monotonic()
    _feed(tap, 50, adc_start=now - BLOCK / RATE)
    assert caplog.text.count("not the same") == 0


# -- overflow accounting ------------------------------------------------------


def test_an_overflow_flag_is_counted(tap: AudioTap) -> None:
    tap._callback(
        np.zeros((BLOCK, CHANNELS), dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=START),
        FakeStatus(input_overflow=True),
    )
    assert tap.overruns == 1


# -- the samples themselves ---------------------------------------------------


def test_samples_come_back_scaled_and_in_order(tap: AudioTap) -> None:
    _feed(tap, 3)
    chunk = tap.stream(0, timeout=0.0)

    assert chunk.samples.shape == (3 * BLOCK, CHANNELS)
    assert chunk.samples.dtype == np.float32
    # Block n was filled with the int16 value n, and int16 is scaled by 1/32768.
    for n in range(3):
        assert chunk.samples[n * BLOCK, 0] == pytest.approx(n / 32768.0, abs=1e-9)
