"""Whether the measured points can catch a broken audio time axis.

Each case builds a recording with one of the two failures invisible in a WAV -
a converter off its nominal rate, or dropped audio - and asks the report to
name it.
"""

from __future__ import annotations

import numpy as np
import pytest

from rrr.timeline.audio_clock import (
    AudioClockPoint,
    AudioClockWriter,
    AudioTimeline,
)

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


def test_ideal_recording_measures_the_nominal_rate() -> None:
    report = AudioTimeline(_ideal(100), RATE).report()
    assert report.measured_rate == pytest.approx(RATE, rel=1e-9)
    assert report.rate_error_ppm == pytest.approx(0.0, abs=1e-3)
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-6)
    assert report.filled == 0


def test_sample_and_time_invert_each_other() -> None:
    timeline = AudioTimeline(_ideal(50), RATE)
    for sample in (0, 1024, 40_960, 50_000):
        # Measured worst case 1.3e-6 samples: a double's ulp at ~1.3e6 s
        # (2.3e-10 s) divided by 6.25e-5 s per sample.
        assert timeline.sample_at(timeline.monotonic_at(sample)) == pytest.approx(
            sample, abs=1e-3
        )


def test_monotonic_at_matches_the_nominal_arithmetic() -> None:
    """For a clean recording, the fit must agree with start + n/rate."""
    timeline = AudioTimeline(_ideal(50), RATE)
    for sample in (0, 8_000, 32_768):
        assert timeline.monotonic_at(sample) == pytest.approx(
            START + sample / RATE, abs=1e-9
        )


def test_a_single_point_falls_back_to_the_nominal_rate() -> None:
    """One block is not enough to fit a rate, and must not pretend otherwise."""
    timeline = AudioTimeline([AudioClockPoint(sample=0, monotonic=START)], RATE)
    report = timeline.report()
    assert report.measured_rate is None
    assert report.rate_error_ppm is None
    assert timeline.monotonic_at(RATE) == pytest.approx(START + 1.0, abs=1e-9)


# -- a converter running at the wrong rate ------------------------------------


@pytest.mark.parametrize("ppm", [-200.0, -50.0, 50.0, 200.0])
def test_a_rate_error_is_measured_back(ppm: float) -> None:
    """A crystal off by ``ppm`` is reported as such (50 ppm = 0.18 s/h)."""
    actual_rate = RATE * (1.0 + ppm / 1e6)
    report = AudioTimeline(_ideal(1000, rate=actual_rate), RATE).report()

    assert report.measured_rate == pytest.approx(actual_rate, rel=1e-9)
    assert report.rate_error_ppm == pytest.approx(ppm, rel=1e-6)
    # Cross-check the fitted rate against the ppm figure over an hour.
    drift_per_hour = 3600.0 * (report.measured_rate - RATE) / RATE
    assert drift_per_hour == pytest.approx(ppm / 1e6 * 3600.0, rel=1e-6)


def test_a_rate_error_leaves_no_residual() -> None:
    """A steady rate error is absorbed by the fit, so only the rate reveals it."""
    report = AudioTimeline(_ideal(500, rate=RATE * 1.0002), RATE).report()
    assert report.residual_max_ms == pytest.approx(0.0, abs=1e-6)
    assert report.rate_error_ppm == pytest.approx(200.0, rel=1e-6)


# -- dropped audio ------------------------------------------------------------


def _with_unfilled_drop(
    blocks: int, at_block: int, lost: int
) -> list[AudioClockPoint]:
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


def test_an_unfilled_drop_shows_up_as_a_step_in_the_residual() -> None:
    """The file contracted by the dropped samples, and the report must say so."""
    lost = 4 * BLOCK  # 4096 samples = 256 ms at 16 kHz
    report = AudioTimeline(_with_unfilled_drop(200, 100, lost), RATE).report()

    # A mid-recording step leaves about half the gap either side of the fit.
    assert report.residual_max_ms is not None
    assert report.residual_max_ms == pytest.approx(lost / RATE * 1000.0 / 2, rel=0.25)


def test_a_step_also_corrupts_the_fitted_rate() -> None:
    """A drop drags the slope too: 256 ms in 13 s reads as about -29,000 ppm.

    So a fitted rate means nothing until the residual is checked first.
    """
    lost = 4 * BLOCK
    report = AudioTimeline(_with_unfilled_drop(200, 100, lost), RATE).report()

    assert abs(report.rate_error_ppm) > 10_000.0, "the slope is dragged, not spared"
    assert report.residual_max_ms > 100.0, "and the residual is what says so"


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


def test_filled_and_unfilled_are_distinguishable() -> None:
    """The two must not look the same, or the sidecar buys nothing."""
    lost = 2 * BLOCK
    unfilled = AudioTimeline(_with_unfilled_drop(200, 100, lost), RATE).report()
    assert unfilled.filled == 0
    assert unfilled.residual_max_ms > 10.0


# -- timestamp jitter ---------------------------------------------------------


def test_jitter_appears_in_the_residual_and_not_in_the_rate() -> None:
    """Measured ADC jitter on a ReSpeaker is 0.35 ms; it must not skew the rate."""
    rng = np.random.default_rng(20260901)
    jitter = rng.normal(0.0, 0.00035, 2000)
    points = [
        AudioClockPoint(sample=n * BLOCK, monotonic=START + n * BLOCK / RATE + jitter[n])
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

    with AudioClockWriter(path) as writer:
        for point in original:
            writer.append(point)
        assert writer.count == 10

    timeline = AudioTimeline.read(path, RATE)
    assert timeline.points == original
    assert timeline.report().filled == 512


def test_writer_omits_the_common_zero_filled_case(tmp_path) -> None:
    """Most blocks fill nothing, and 56k entries an hour is worth keeping small."""
    path = str(tmp_path / "audio.clock.jsonl")
    with AudioClockWriter(path) as writer:
        writer.append(AudioClockPoint(sample=0, monotonic=START))
    assert "filled" not in open(path, encoding="utf-8").read()


def test_points_are_sorted_by_position() -> None:
    timeline = AudioTimeline(list(reversed(_ideal(10))), RATE)
    assert [point.sample for point in timeline.points] == [
        n * BLOCK for n in range(10)
    ]


def test_empty_and_invalid_inputs_are_refused() -> None:
    with pytest.raises(ValueError):
        AudioTimeline([], RATE)
    with pytest.raises(ValueError):
        AudioTimeline(_ideal(2), 0)
