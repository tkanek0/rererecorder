"""The clock arithmetic, against offsets whose right answer is known.

A chosen offset, unlike a real one, can tell a correct conversion from one that
returns its input.
"""

from __future__ import annotations

import time

import pytest

from rrr.timeline.clock import ClockPair, ClockTrack, read_clocks

#: Monotonic near an uptime, realtime near now: an offset too large for a
#: forgotten conversion to pass.
MONO = 1_322_228.023434
REAL = 1_788_250_182.059000
OFFSET = REAL - MONO


@pytest.fixture
def pair() -> ClockPair:
    """A pair with a known offset."""
    return ClockPair(monotonic=MONO, realtime=REAL, uncertainty=1e-6)


def test_offset_is_realtime_minus_monotonic(pair: ClockPair) -> None:
    assert pair.offset == pytest.approx(OFFSET, abs=1e-9)


def test_conversions_invert_each_other(pair: ClockPair) -> None:
    for value in (MONO, MONO - 3600.0, MONO + 12345.678):
        assert pair.to_monotonic(pair.to_realtime(value)) == pytest.approx(
            value, abs=1e-6
        )


def test_to_realtime_places_the_anchor_on_itself(pair: ClockPair) -> None:
    # 1e-6 s, not 0: a double's ulp at 1.79e9 s is 2.4e-7 s, far below the
    # devices' 10 ms.
    assert pair.to_realtime(MONO) == pytest.approx(REAL, abs=1e-6)
    assert pair.to_monotonic(REAL) == pytest.approx(MONO, abs=1e-6)


def test_epoch_ms_conversion_matches_the_seconds_one(pair: ClockPair) -> None:
    """A camera timestamp in epoch ms lands where the same instant in seconds does."""
    epoch_ms = REAL * 1000.0 + 250.0
    assert pair.epoch_ms_to_monotonic(epoch_ms) == pytest.approx(
        pair.to_monotonic(epoch_ms / 1000.0), abs=1e-9
    )
    # And independently: 250 ms past the anchor is 0.25 s past it on either axis.
    assert pair.epoch_ms_to_monotonic(epoch_ms) == pytest.approx(MONO + 0.25, abs=1e-6)


def test_a_frame_timestamp_keeps_its_interval_through_conversion(
    pair: ClockPair,
) -> None:
    """Durations survive moving from the camera's epoch ms to monotonic seconds."""
    first = REAL * 1000.0
    second = first + 33.333
    delta = pair.epoch_ms_to_monotonic(second) - pair.epoch_ms_to_monotonic(first)
    # Measured worst case 1.1e-7 s over 1-1000 ms intervals: the ulp of epoch
    # ms at ~1.79e12 (2.4e-4 ms), far finer than the timestamps' accuracy.
    assert delta == pytest.approx(0.033333, abs=1e-6)


def test_round_trip_through_json(pair: ClockPair) -> None:
    assert ClockPair.from_dict(pair.as_dict()) == pair


def test_from_dict_tolerates_a_missing_uncertainty() -> None:
    rebuilt = ClockPair.from_dict({"monotonic": MONO, "realtime": REAL})
    assert rebuilt.uncertainty == 0.0
    assert rebuilt.offset == pytest.approx(OFFSET, abs=1e-9)


# -- read_clocks --------------------------------------------------------------


def test_read_clocks_agrees_with_reading_the_clocks_separately() -> None:
    before_mono, before_real = time.monotonic(), time.time()
    pair = read_clocks()
    after_mono, after_real = time.monotonic(), time.time()

    assert before_mono <= pair.monotonic <= after_mono
    assert before_real <= pair.realtime <= after_real
    assert pair.uncertainty >= 0.0
    # Generous: catches only a non-simultaneous pairing, since a loaded
    # machine can deschedule between reads.
    assert pair.uncertainty < 0.1


def test_read_clocks_offset_is_stable_across_calls() -> None:
    """Back-to-back offsets agree; NTP slew is only parts per million."""
    first, second = read_clocks(), read_clocks()
    assert second.offset - first.offset == pytest.approx(0.0, abs=1e-3)


# -- ClockTrack ---------------------------------------------------------------


def _synthetic_track(offsets: list[tuple[float, float]]) -> ClockTrack:
    """Build a track directly from (monotonic, offset) pairs."""
    return ClockTrack.from_list(
        [
            {"monotonic": mono, "realtime": mono + offset, "uncertainty": 0.0}
            for mono, offset in offsets
        ]
    )


def test_track_keeps_the_first_sample_immediately() -> None:
    track = ClockTrack(interval_s=60.0)
    track.sample()
    assert len(track.samples) == 1


def test_track_thins_samples_by_interval() -> None:
    track = ClockTrack(interval_s=60.0)
    for _ in range(5):
        track.sample()
    assert len(track.samples) == 1, "an interval of a minute cannot keep five reads"


def test_track_force_keeps_regardless_of_interval() -> None:
    track = ClockTrack(interval_s=60.0)
    track.sample()
    track.sample(force=True)
    assert len(track.samples) == 2


def test_track_at_picks_the_sample_in_force() -> None:
    track = _synthetic_track([(100.0, 1.0), (200.0, 2.0), (300.0, 3.0)])
    assert track.at(250.0).offset == pytest.approx(2.0)
    assert track.at(200.0).offset == pytest.approx(2.0), "at a boundary, that sample"
    assert track.at(999.0).offset == pytest.approx(3.0)


def test_track_at_falls_back_to_the_earliest_sample() -> None:
    """A frame stamped just before the first clock reading still converts."""
    track = _synthetic_track([(100.0, 1.0), (200.0, 2.0)])
    assert track.at(50.0).offset == pytest.approx(1.0)


def test_track_at_is_none_when_empty() -> None:
    assert ClockTrack().at(0.0) is None


def test_track_does_not_interpolate_across_a_step() -> None:
    """Interpolating across an NTP step would invent offsets never in force."""
    track = _synthetic_track([(100.0, 1.0), (200.0, 6.0)])
    assert track.at(150.0).offset == pytest.approx(1.0), "the old offset still held"


def test_drift_ppm_matches_an_independent_calculation() -> None:
    # 20 microseconds of drift over 100 seconds is 0.2 ppm.
    track = _synthetic_track([(100.0, 1.0), (200.0, 1.000020)])
    assert track.drift_ppm == pytest.approx(0.2, rel=1e-6)


def test_drift_ppm_is_none_when_it_cannot_be_measured() -> None:
    assert ClockTrack().drift_ppm is None
    assert _synthetic_track([(100.0, 1.0)]).drift_ppm is None
    assert _synthetic_track([(100.0, 1.0), (100.0, 1.0)]).drift_ppm is None


def test_track_round_trips_through_json() -> None:
    track = _synthetic_track([(100.0, 1.0), (200.0, 2.0)])
    rebuilt = ClockTrack.from_list(track.as_list())
    assert [pair.offset for pair in rebuilt.samples] == [1.0, 2.0]
