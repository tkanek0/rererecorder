"""The adapter's capture primitive and device lookup, with no device attached.

Calling the callback directly exercises scaling and time stamping. Importing
still loads PortAudio, so a missing libportaudio2 fails at import.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pytest
from respeaker_adapter.capture import (
    _DOMAIN_CALIBRATION_BLOCKS,
    AdcClock,
    Capture,
    DeviceNotFound,
    _resolve_device,
)

RATE = 16_000
BLOCK = 256
CHANNELS = 6
LAG = BLOCK / RATE


@dataclass
class FakeTimeInfo:
    """What PortAudio hands a callback."""

    inputBufferAdcTime: float  # noqa: N815 - PortAudio's own spelling


@dataclass
class FakeStatus:
    """PortAudio's callback flags."""

    input_overflow: bool = False


def test_a_block_arrives_scaled_and_stamped_with_its_adc_time() -> None:
    received: list[tuple[np.ndarray, float]] = []
    capture = Capture(lambda block, at: received.append((block, at)), rate=RATE)
    capture._clock._decided = True  # skip calibration: the clock agrees

    before = time.monotonic()
    capture._callback(
        np.full((BLOCK, CHANNELS), 3, dtype=np.int16),
        BLOCK,
        FakeTimeInfo(inputBufferAdcTime=123.0),
        FakeStatus(input_overflow=True),
    )

    block, at = received[0]
    assert block.dtype == np.float32
    assert block[0, 0] == pytest.approx(3 / 32768.0)
    assert at == 123.0, "the converter's time, not the callback's"
    assert capture.overruns == 1
    assert capture.last_block_at >= before


# -- the ADC clock: agrees, a fixed offset, or not one clock at all ----------
#
# See docs/decisions.md 20 and 26.

BLOCK_SAMPLES = np.zeros((BLOCK, CHANNELS), dtype=np.float32)


def _calibrate(clock: AdcClock, offsets: list[float]) -> list[float]:
    """Report one block per offset at ``now - offset``; return the times released."""
    released = []
    for offset in offsets:
        released += [
            at for _, at in clock.add(BLOCK_SAMPLES, time.monotonic() - offset)
        ]
    return released


@pytest.mark.parametrize(
    ("offsets", "offset", "incoherent", "logged"),
    [
        # The ordinary case: the callback runs a block after the ADC.
        ([LAG] * _DOMAIN_CALIBRATION_BLOCKS, 0.0, False, ""),
        # A stable clock on another epoch, as WASAPI's ~3.9 s, is corrected.
        ([3.9] * _DOMAIN_CALIBRATION_BLOCKS, 3.9 - LAG, False, "fixed offset"),
        # Not one clock at all, as MME's was: the callback clock is used.
        (
            [-3.9 if n % 2 else -9.9 for n in range(_DOMAIN_CALIBRATION_BLOCKS)],
            0.0,
            True,
            "not readable as one clock",
        ),
    ],
)
def test_the_adc_clock_is_judged_once_and_then_applied(
    offsets, offset, incoherent, logged, caplog
) -> None:
    clock = AdcClock(RATE)
    released = _calibrate(clock, offsets)
    assert len(released) == _DOMAIN_CALIBRATION_BLOCKS, "held, then released together"
    _calibrate(clock, [LAG] * 30)  # decided once; nothing re-fitted or re-warned

    assert clock.offset == pytest.approx(offset, abs=2e-3)
    assert clock.incoherent is incoherent
    assert logged in caplog.text
    assert caplog.text.count("WARNING") <= 1

    now = time.monotonic()
    [(_, at)] = clock.add(BLOCK_SAMPLES, now - offsets[0])
    assert at == pytest.approx(now - LAG, abs=2e-3)


def test_blocks_delivered_in_bursts_are_stamped_without_gaps() -> None:
    """Callbacks arrive two blocks at a time; the converter's times are still even.

    Timing these from the callback would read as a hole every other block, which
    the writer then fills with silence that was never lost.
    """
    clock = AdcClock(RATE)
    start = time.monotonic() - 1.0
    released = []
    for n in range(_DOMAIN_CALIBRATION_BLOCKS + 4):
        released += [at for _, at in clock.add(BLOCK_SAMPLES, start + n * LAG)]
    assert np.diff(released) == pytest.approx(LAG, abs=1e-9)


def test_a_capture_closed_before_the_decision_still_delivers_what_it_held() -> None:
    received: list[float] = []
    capture = Capture(lambda block, at: received.append(at), rate=RATE)
    for _ in range(3):
        capture._callback(
            np.zeros((BLOCK, CHANNELS), dtype=np.int16),
            BLOCK,
            FakeTimeInfo(inputBufferAdcTime=time.monotonic()),
            FakeStatus(),
        )
    assert received == []
    capture.__exit__(None, None, None)
    assert len(received) == 3


def test_a_missing_adc_time_falls_back_to_the_callback_clock(caplog) -> None:
    """Some host APIs report zero; the block started a block before the callback."""
    before = time.monotonic()
    [(_, at)] = AdcClock(RATE).add(BLOCK_SAMPLES, 0.0)
    assert before - LAG <= at <= time.monotonic()
    assert "inputBufferAdcTime" in caplog.text


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
    monkeypatch.setattr("respeaker_adapter.capture.sd.query_devices", lambda: devices)
    monkeypatch.setattr("respeaker_adapter.capture.sd.query_hostapis", lambda: hostapis)


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


def test_the_first_match_wins_when_nothing_reports_the_requested_rate(
    monkeypatch,
) -> None:
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
            _device(
                "ReSpeaker 4 Mic Array (UAC1.0)", hostapi=0, rate=44100.0, channels=1
            ),
            _device(
                "ReSpeaker 4 Mic Array (UAC1.0) (WASAPI)",
                hostapi=1,
                rate=float(RATE),
                channels=6,
            ),
        ],
        [{"name": "MME"}, {"name": "Windows WASAPI"}],
    )
    assert _resolve_device("ReSpeaker", 6, RATE) == 1


def test_every_match_short_on_channels_names_the_firmware_problem(monkeypatch) -> None:
    _patch_devices(
        monkeypatch,
        [
            _device(
                "ReSpeaker 4 Mic Array (UAC1.0)", hostapi=0, rate=44100.0, channels=1
            )
        ],
        [{"name": "MME"}],
    )
    with pytest.raises(DeviceNotFound, match="1-channel firmware"):
        _resolve_device("ReSpeaker", 6, RATE)


def test_no_name_match_is_reported_plainly(monkeypatch) -> None:
    _patch_devices(monkeypatch, [], [])
    with pytest.raises(DeviceNotFound, match="no capture device"):
        _resolve_device("ReSpeaker", 6, RATE)
