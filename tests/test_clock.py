"""The clock arithmetic, against offsets whose right answer is known.

A chosen offset, unlike a real one, can tell a correct conversion from one that
returns its input.
"""

from __future__ import annotations

import time

import pytest
from rrr.timeline.clock import ClockPair, ClockTrack, drift_ppm, read_clocks

#: Monotonic near an uptime, realtime near now: an offset too large for a
#: forgotten conversion to pass.
MONO = 1_322_228.023434
REAL = 1_788_250_182.059000
OFFSET = REAL - MONO


def _pair(monotonic: float = MONO, offset: float = OFFSET) -> ClockPair:
    return ClockPair(monotonic=monotonic, realtime=monotonic + offset, uncertainty=1e-6)


def test_conversions_land_on_the_right_axis_and_survive_json() -> None:
    pair = _pair()
    assert pair.offset == pytest.approx(OFFSET, abs=1e-9)
    # 1e-6 s, not 0: a double's ulp at 1.79e9 s is 2.4e-7 s.
    assert pair.to_monotonic(REAL) == pytest.approx(MONO, abs=1e-6)
    # A camera timestamp 250 ms past the anchor is 0.25 s past it on either axis.
    assert pair.epoch_ms_to_monotonic(REAL * 1000.0 + 250.0) == pytest.approx(
        MONO + 0.25, abs=1e-6
    )
    # Durations survive the epoch-ms ulp (2.4e-4 ms at ~1.79e12).
    first = REAL * 1000.0
    delta = pair.epoch_ms_to_monotonic(first + 33.333) - pair.epoch_ms_to_monotonic(
        first
    )
    assert delta == pytest.approx(0.033333, abs=1e-6)
    assert ClockPair.from_dict(pair.as_dict()) == pair


def test_read_clocks_pairs_two_simultaneous_readings() -> None:
    before_mono, before_real = time.monotonic(), time.time()
    pair = read_clocks()
    after_mono, after_real = time.monotonic(), time.time()

    assert before_mono <= pair.monotonic <= after_mono
    assert before_real <= pair.realtime <= after_real
    # Generous: catches only a non-simultaneous pairing on a loaded machine.
    assert 0.0 <= pair.uncertainty < 0.1
    assert read_clocks().offset - pair.offset == pytest.approx(0.0, abs=1e-3)


def test_track_thins_samples_by_interval_unless_forced() -> None:
    track = ClockTrack(interval_s=60.0)
    for _ in range(5):
        track.sample()
    assert len(track.samples) == 1, "an interval of a minute cannot keep five reads"
    track.sample(force=True)
    assert len(track.samples) == 2
    assert track.latest == track.samples[-1]


def test_drift_ppm_matches_an_independent_calculation() -> None:
    # 20 microseconds of drift over 100 seconds is 0.2 ppm.
    samples = [_pair(100.0, 1.0), _pair(200.0, 1.000020)]
    assert drift_ppm(samples) == pytest.approx(0.2, rel=1e-6)
    assert drift_ppm(samples[:1]) is None
    assert drift_ppm([samples[0], samples[0]]) is None
