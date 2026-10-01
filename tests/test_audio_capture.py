"""The tap's time stamping, driven by fake PortAudio callbacks.

Calling the callback directly exercises the whole stamping path with no device.
Importing still loads PortAudio, so a missing libportaudio2 fails at import.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pytest

from rrr.audio.capture import (
    _DOMAIN_CALIBRATION_BLOCKS,
    AudioTap,
    DeviceNotFound,
    _resolve_device,
)

RATE = 16_000
BLOCK = 256
CHANNELS = 6


@dataclass
class FakeTimeInfo:
    """What PortAudio hands a callback."""

    inputBufferAdcTime: float  # noqa: N815 - PortAudio's own spelling
    currentTime: float = 0.0  # noqa: N815


@dataclass
class FakeStatus:
    """PortAudio's callback flags."""

    input_overflow: bool = False


def _fresh_tap() -> AudioTap:
    """A tap that has never opened a device, or calibrated its clock domain."""
    return AudioTap(rate=RATE, channels=CHANNELS, block_size=BLOCK, window_s=1.0)


@pytest.fixture
def tap() -> AudioTap:
    """A tap whose clock domain is already decided as "agrees"."""
    instance = _fresh_tap()
    instance._domain_calibrated = True
    return instance


def _feed(
    tap: AudioTap,
    blocks: int,
    *,
    first_block: int = 0,
    adc_start: float | None = None,
    gap_after: int | None = None,
    gap_blocks: int = 0,
    value: float | None = None,
) -> None:
    """Push blocks through the callback as PortAudio would.

    Args:
        tap: The tap to feed.
        blocks: How many blocks to deliver.
        first_block: Block number the ADC times start counting from.
        adc_start: ADC time of block zero. None takes ``time.monotonic()``,
            so an uncalibrated tap still calibrates to "agrees"; a constant
            would only agree on one machine's uptime.
        gap_after: Deliver a gap after this many blocks, or None for none.
        gap_blocks: Blocks the gap swallows. The callback is not called for
            them, as in a driver overflow: samples stay continuous, ADC time
            jumps.
        value: Constant sample value, or None for a per-block ramp that makes
            the blocks distinguishable.
    """
    if adc_start is None:
        adc_start = time.monotonic()
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
    """A block is timed by when its audio was converted, not when the callback ran."""
    start = time.monotonic()
    _feed(tap, 3, adc_start=start)
    chunk = tap.stream(0, timeout=0.0)

    for n, stamp in enumerate(chunk.stamps):
        assert stamp.monotonic == pytest.approx(start + n * BLOCK / RATE, abs=1e-9)


def test_captured_at_is_the_newest_samples_time(tap: AudioTap) -> None:
    """Not the block's first sample, and not when the callback ran."""
    start = time.monotonic()
    _feed(tap, 4, adc_start=start)
    chunk = tap.stream(0, timeout=0.0)

    assert chunk.captured_at == pytest.approx(start + 4 * BLOCK / RATE, abs=1e-9)


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
    """An input overflow: samples stay continuous, only the ADC time shows the hole."""
    _feed(tap, 6, gap_after=3, gap_blocks=8)  # 8 blocks = 128 ms
    chunk = tap.stream(0, timeout=0.0)

    stamps = chunk.stamps
    assert [stamp.sample for stamp in stamps] == [n * BLOCK for n in range(6)]
    assert stamps[3].sample == stamps[2].end_sample, "samples are continuous"

    lost = stamps[3].monotonic - stamps[2].end_monotonic(RATE)
    assert lost == pytest.approx(8 * BLOCK / RATE, abs=1e-9)
    assert lost == pytest.approx(0.128, abs=1e-6)


def test_a_slow_reader_shows_as_a_jump_in_samples(tap: AudioTap) -> None:
    """Overwritten audio is reported as ``dropped``, distinct from a driver gap."""
    _feed(tap, 8)  # a 1 s window at 16 kHz holds 62.5 blocks, so nothing is lost
    tap._capacity = 4 * BLOCK  # shrink the window instead of feeding 60 blocks
    tap._ring = np.zeros((tap._capacity, CHANNELS), dtype=np.float32)
    tap._position = 0
    _feed(tap, 8, first_block=8)

    chunk = tap.stream(0, timeout=0.0)
    assert chunk.dropped > 0
    assert chunk.first_sample > 0


def test_stamps_cover_the_samples_a_chunk_actually_holds(tap: AudioTap) -> None:
    """Every sample delivered has a stamp covering it, even after the ring wraps."""
    _feed(tap, 100)  # more than the 62.5 blocks a 1 s window holds
    chunk = tap.stream(0, timeout=0.0)

    assert chunk.stamps, "a wrapped ring must still deliver stamps"
    assert chunk.stamps[0].sample <= chunk.first_sample
    assert chunk.stamps[-1].end_sample >= chunk.cursor


# -- when PortAudio does not fill the field in -------------------------------


def test_a_missing_adc_time_falls_back_to_the_callback_clock(
    tap: AudioTap, caplog
) -> None:
    """Some host APIs report zero; that falls back to the callback clock."""
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


# -- the clock domain: agrees, a fixed offset, or not one clock at all -------
#
# These use _fresh_tap(), since the `tap` fixture skips calibration.
# See docs/decisions.md 20 and 26.


def _feed_domain(tap: AudioTap, offsets: list[float]) -> None:
    """Feed one block per offset, each reported at ``now - offsets[n]``."""
    for offset in offsets:
        now = time.monotonic()
        tap._callback(
            np.zeros((BLOCK, CHANNELS), dtype=np.int16),
            BLOCK,
            FakeTimeInfo(inputBufferAdcTime=now - offset),
            FakeStatus(),
        )


def test_an_agreeing_clock_is_accepted_quietly(caplog) -> None:
    """The ordinary case: calibration decides "agrees" and adjusts nothing."""
    fresh = _fresh_tap()
    _feed_domain(fresh, [BLOCK / RATE] * _DOMAIN_CALIBRATION_BLOCKS)

    assert fresh._adc_offset == 0.0
    assert not fresh._adc_bad_domain
    assert "correcting for the fixed offset" not in caplog.text
    assert "not readable as one clock" not in caplog.text


def test_a_stable_wrong_origin_is_corrected_for(caplog) -> None:
    """A stable clock on another epoch is corrected, not discarded.

    Modelled on WASAPI's ~3.9 s offset; see docs/decisions.md 26.
    """
    fresh = _fresh_tap()
    offset = 3.9  # seconds - the same order as the real measurement
    _feed_domain(fresh, [offset] * _DOMAIN_CALIBRATION_BLOCKS)

    assert not fresh._adc_bad_domain
    # Loose tolerance: allows for the Python-level delay between `_feed_domain`
    # reading `now` and the tap reading it, not sensor jitter.
    assert fresh._adc_offset == pytest.approx(offset - BLOCK / RATE, abs=2e-3)
    assert "correcting for the fixed offset" in caplog.text

    # And it is applied: the block lands where an agreeing clock's would.
    now = time.monotonic()
    fresh._callback(
        np.zeros((BLOCK, CHANNELS), dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=now - offset),
        FakeStatus(),
    )
    chunk = fresh.stream(0, timeout=0.0)
    assert chunk.stamps[-1].monotonic == pytest.approx(now - BLOCK / RATE, abs=2e-3)


def test_an_incoherent_clock_falls_back_for_the_rest_of_the_recording(caplog) -> None:
    """An incoherent clock (as MME's was) yields to the callback clock for good.

    See docs/decisions.md 20 and 26.
    """
    fresh = _fresh_tap()
    # Only the spread matters. Negative, so the reported time stays positive (a
    # valid reading) however recently the host booted.
    offsets = [
        -3.9 if n % 2 == 0 else -9.9 for n in range(_DOMAIN_CALIBRATION_BLOCKS)
    ]
    _feed_domain(fresh, offsets)

    assert fresh._adc_bad_domain
    assert "not readable as one clock" in caplog.text

    caplog.clear()
    before = time.monotonic()
    fresh._callback(
        np.zeros((BLOCK, CHANNELS), dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=1.0),
        FakeStatus(),
    )
    after = time.monotonic()
    chunk = fresh.stream(0, timeout=0.0)
    assert before - BLOCK / RATE <= chunk.stamps[-1].monotonic <= after
    assert caplog.text == "", "the decision is not re-warned every block"


def test_the_domain_decision_is_made_once(caplog) -> None:
    """Calibration stops after its window rather than re-fitting every block."""
    fresh = _fresh_tap()
    _feed_domain(fresh, [BLOCK / RATE] * (_DOMAIN_CALIBRATION_BLOCKS + 30))

    assert len(fresh._domain_lags) == _DOMAIN_CALIBRATION_BLOCKS
    assert "correcting for the fixed offset" not in caplog.text
    assert "not readable as one clock" not in caplog.text


# -- overflow accounting ------------------------------------------------------


def test_an_overflow_flag_is_counted(tap: AudioTap) -> None:
    tap._callback(
        np.zeros((BLOCK, CHANNELS), dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=time.monotonic()),
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


# -- device resolution ---------------------------------------------------------
#
# Windows exposes the same ReSpeaker once per host API; these fakes reproduce
# that shape. See docs/decisions.md 24.


def _device(name: str, hostapi: int, rate: float, channels: int = 6) -> dict:
    return {
        "name": name,
        "hostapi": hostapi,
        "max_input_channels": channels,
        "default_samplerate": rate,
    }


_WINDOWS_HOSTAPIS = [
    {"name": "MME"},
    {"name": "Windows DirectSound"},
    {"name": "Windows WASAPI"},
    {"name": "Windows WDM-KS"},
]

# MME and DirectSound come first, as on the real array, so picking WASAPI
# tests the ranking rather than the order.
_WINDOWS_DEVICES = [
    _device("ReSpeaker 4 Mic Array (UAC1.0)", hostapi=0, rate=44100.0),
    _device("ReSpeaker 4 Mic Array (UAC1.0) (DS)", hostapi=1, rate=44100.0),
    _device("ReSpeaker 4 Mic Array (UAC1.0) (WASAPI)", hostapi=2, rate=16000.0),
    _device("ReSpeaker 4 Mic Array (UAC1.0) (WDM-KS)", hostapi=3, rate=16000.0),
]


def _patch_devices(monkeypatch, devices: list[dict], hostapis: list[dict]) -> None:
    monkeypatch.setattr("rrr.audio.capture.sd.query_devices", lambda: devices)
    monkeypatch.setattr("rrr.audio.capture.sd.query_hostapis", lambda: hostapis)


def test_wasapi_is_preferred_when_it_reports_the_right_rate(monkeypatch) -> None:
    """Among several host-API entries for one device, WASAPI-at-the-right-rate wins."""
    _patch_devices(monkeypatch, _WINDOWS_DEVICES, _WINDOWS_HOSTAPIS)
    index = _resolve_device("ReSpeaker", 6, RATE)
    assert index == 2


def test_a_matching_rate_wins_without_wasapi_present(monkeypatch) -> None:
    """Linux's shape: one match, no WASAPI; the rate tier alone picks it."""
    _patch_devices(
        monkeypatch,
        [_device("ReSpeaker 4 Mic Array (UAC1.0)", hostapi=0, rate=float(RATE))],
        [{"name": "ALSA"}],
    )
    assert _resolve_device("ReSpeaker", 6, RATE) == 0


def test_the_first_match_wins_when_nothing_reports_the_requested_rate(monkeypatch) -> None:
    """With no ranked tier applying, the first match is used rather than raising."""
    _patch_devices(
        monkeypatch,
        [
            _device("ReSpeaker 4 Mic Array (UAC1.0)", hostapi=0, rate=48000.0),
            _device("ReSpeaker 4 Mic Array (UAC1.0) (2)", hostapi=0, rate=48000.0),
        ],
        [{"name": "MME"}],
    )
    assert _resolve_device("ReSpeaker", 6, RATE) == 0


def test_a_later_match_with_enough_channels_is_used_over_an_earlier_one_without(
    monkeypatch,
) -> None:
    """A name match short on channels does not end the search."""
    _patch_devices(
        monkeypatch,
        [
            _device("ReSpeaker 4 Mic Array (UAC1.0)", hostapi=0, rate=44100.0, channels=1),
            _device("ReSpeaker 4 Mic Array (UAC1.0) (WASAPI)", hostapi=1, rate=float(RATE), channels=6),
        ],
        [{"name": "MME"}, {"name": "Windows WASAPI"}],
    )
    assert _resolve_device("ReSpeaker", 6, RATE) == 1


def test_every_match_short_on_channels_names_the_firmware_problem(monkeypatch) -> None:
    _patch_devices(
        monkeypatch,
        [_device("ReSpeaker 4 Mic Array (UAC1.0)", hostapi=0, rate=44100.0, channels=1)],
        [{"name": "MME"}],
    )
    with pytest.raises(DeviceNotFound, match="1-channel firmware"):
        _resolve_device("ReSpeaker", 6, RATE)


def test_no_name_match_is_reported_plainly(monkeypatch) -> None:
    _patch_devices(monkeypatch, [], [])
    with pytest.raises(DeviceNotFound, match="no capture device"):
        _resolve_device("ReSpeaker", 6, RATE)


# -- failure handling ---------------------------------------------------------
#
# A failed open is never retried automatically. See docs/decisions.md 29.


def _wait_until(ready, timeout: float = 2.0) -> bool:
    """Poll ``ready`` until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.01)
    return ready()


class _FakeInputStream:
    """Stands in for ``sd.InputStream``: opens nothing, delivers nothing."""

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self) -> _FakeInputStream:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


@pytest.fixture
def failing_tap(monkeypatch):
    """A tap whose device cannot be found, and a count of the attempts."""
    attempts = []

    def resolve(*_: object) -> int:
        attempts.append(time.monotonic())
        raise DeviceNotFound("no capture device whose name contains 'ReSpeaker'")

    monkeypatch.setattr("rrr.audio.capture._resolve_device", resolve)
    monkeypatch.setattr("rrr.audio.capture.sd.InputStream", _FakeInputStream)
    made = _fresh_tap()
    yield made, attempts
    made.shutdown()


def test_a_failed_open_is_not_retried(failing_tap) -> None:
    tap, attempts = failing_tap
    tap.acquire()
    assert _wait_until(lambda: tap.failed)
    tap.acquire()
    time.sleep(0.2)
    assert len(attempts) == 1
    assert not tap.active
    assert "no capture device" in (tap.error or "")


def test_reads_do_not_wait_while_failed(failing_tap) -> None:
    tap, _ = failing_tap
    tap.acquire()
    assert _wait_until(lambda: tap.failed)
    began = time.monotonic()
    assert tap.stream(0, timeout=5.0) is None
    assert tap.latest(timeout=5.0) is None
    assert time.monotonic() - began < 0.5


def test_reconnect_opens_again(failing_tap, monkeypatch) -> None:
    tap, _ = failing_tap
    tap.acquire()
    assert _wait_until(lambda: tap.failed)

    monkeypatch.setattr("rrr.audio.capture._resolve_device", lambda *_: 0)
    tap.reconnect()
    assert not tap.failed
    assert tap.error is None
    assert _wait_until(lambda: tap.active)
