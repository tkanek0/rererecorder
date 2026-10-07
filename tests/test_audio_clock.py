"""Whether the measured points can catch a broken audio time axis.

Each case builds a recording with one of the two failures invisible in a WAV -
a converter off its nominal rate, or dropped audio - and asks the report to
name it.
"""

from __future__ import annotations

import numpy as np
import pytest
from rrr.timeline import AudioClockPoint, AudioTimeline, JsonlWriter

RATE = 16_000
BLOCK = 1024
#: Monotonic time of the first sample. Arbitrary, and large enough that a
#: forgotten intercept shows up rather than hiding near zero.
START = 1_322_228.023434


def _ideal(blocks: int, rate: float = RATE) -> list[AudioClockPoint]:
    """Points from a recording with no drops and a converter running at ``rate``."""
    return [
        AudioClockPoint(sample=n * BLOCK, monotonic=START + n * BLOCK / rate)
        for n in range(blocks)
    ]


# -- the ideal case -----------------------------------------------------------


def test_an_ideal_recording_is_the_nominal_arithmetic_both_ways() -> None:
    timeline = AudioTimeline(_ideal(50), RATE)
    report = timeline.report()
    assert report.measured_rate == pytest.approx(RATE, rel=1e-9)
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-6)
    assert report.filled == 0
    for sample in (0, 8_000, 40_960):
        assert timeline.monotonic_at(sample) == pytest.approx(
            START + sample / RATE, abs=1e-9
        )
        # A double's ulp at ~1.3e6 s is 2.3e-10 s, far under a sample.
        assert timeline.sample_at(timeline.monotonic_at(sample)) == pytest.approx(
            sample, abs=1e-3
        )

    # One point is not enough to fit a rate, and must not pretend otherwise.
    single = AudioTimeline([AudioClockPoint(sample=0, monotonic=START)], RATE)
    assert single.report().measured_rate is None
    assert single.monotonic_at(RATE) == pytest.approx(START + 1.0, abs=1e-9)


# -- a converter running at the wrong rate ------------------------------------


@pytest.mark.parametrize("ppm", [-200.0, -50.0, 50.0, 200.0])
def test_a_rate_error_is_measured_back_and_leaves_no_residual(ppm: float) -> None:
    """A crystal off by ``ppm`` is absorbed by the fit, so only the rate reveals it."""
    actual_rate = RATE * (1.0 + ppm / 1e6)
    report = AudioTimeline(_ideal(1000, rate=actual_rate), RATE).report()

    assert report.measured_rate == pytest.approx(actual_rate, rel=1e-9)
    assert report.rate_error_ppm == pytest.approx(ppm, rel=1e-6)
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-6)


# -- dropped audio ------------------------------------------------------------


def _with_unfilled_drop(blocks: int, at_block: int, lost: int) -> list[AudioClockPoint]:
    """Points from a recording that dropped ``lost`` samples and did not fill them.

    Every block after the drop lags its file position by ``lost / rate`` seconds.
    """
    points = []
    position = 0
    lag = 0.0
    for n in range(blocks):
        if n == at_block:
            lag = lost / RATE
        points.append(
            AudioClockPoint(sample=position, monotonic=START + n * BLOCK / RATE + lag)
        )
        position += BLOCK
    return points


def test_an_unfilled_drop_shows_as_a_step_and_drags_the_rate() -> None:
    """The file contracted by the dropped samples, and the report must say so.

    The slope is dragged too (256 ms in 13 s reads as about -29,000 ppm), so a
    fitted rate means nothing until the residual is checked first.
    """
    lost = 4 * BLOCK
    report = AudioTimeline(_with_unfilled_drop(200, 100, lost), RATE).report()

    # A mid-recording step leaves about half the gap either side of the fit.
    assert report.residual_max_ms == pytest.approx(lost / RATE * 1000.0 / 2, rel=0.25)
    assert abs(report.rate_error_ppm) > 10_000.0
    assert report.filled == 0


def test_a_filled_drop_keeps_the_axis_intact() -> None:
    """Filling the hole with silence keeps sample index and capture time in step."""
    lost = 4 * BLOCK
    points = []
    position = 0
    lag = 0.0
    for n in range(200):
        filled = 0
        if n == 100:
            lag = lost / RATE
            position += lost
            filled = lost
        points.append(
            AudioClockPoint(
                sample=position, monotonic=START + n * BLOCK / RATE + lag, filled=filled
            )
        )
        position += BLOCK

    report = AudioTimeline(points, RATE).report()
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-3)
    assert report.filled == lost
    assert report.rate_error_ppm == pytest.approx(0.0, abs=1e-3)


# -- timestamp jitter ---------------------------------------------------------


def test_jitter_appears_in_the_residual_and_not_in_the_rate() -> None:
    """Measured ADC jitter on a ReSpeaker is 0.35 ms; it must not skew the rate."""
    rng = np.random.default_rng(20260901)
    jitter = rng.normal(0.0, 0.00035, 2000)
    points = [
        AudioClockPoint(
            sample=n * BLOCK, monotonic=START + n * BLOCK / RATE + jitter[n]
        )
        for n in range(2000)
    ]
    report = AudioTimeline(points, RATE).report()

    assert report.residual_rms_ms == pytest.approx(0.35, rel=0.15)
    # Over 2000 blocks (128 s), this jitter moves the rate by a few ppm at most.
    assert abs(report.rate_error_ppm) < 5.0


# -- the sidecar --------------------------------------------------------------


def test_writer_and_reader_round_trip(tmp_path) -> None:
    path = str(tmp_path / "audio.clock.jsonl")
    original = _ideal(10)
    original[5] = AudioClockPoint(
        sample=original[5].sample, monotonic=original[5].monotonic, filled=512
    )

    with JsonlWriter(path) as writer:
        for point in original:
            writer.append(point.as_dict())
        assert writer.count == 10

    timeline = AudioTimeline.read(path, RATE)
    assert timeline.points == original
    assert timeline.report().filled == 512


def test_points_are_sorted_by_position() -> None:
    timeline = AudioTimeline(list(reversed(_ideal(10))), RATE)
    assert [point.sample for point in timeline.points] == [n * BLOCK for n in range(10)]


def test_empty_and_invalid_inputs_are_refused() -> None:
    with pytest.raises(ValueError):
        AudioTimeline([], RATE)
    with pytest.raises(ValueError):
        AudioTimeline(_ideal(2), 0)
