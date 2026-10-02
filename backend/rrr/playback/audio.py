"""Where the array's samples sit on the monotonic clock, and moving them there."""

from __future__ import annotations

import math
import wave
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from rrr.timeline import AudioClockPoint, AudioTimeline, Rig

from .frames import TimeRange

#: Samples read from a WAV at a time when mixing it down whole.
_CHUNK_SAMPLES = 1 << 20


@dataclass(frozen=True)
class AudioSelection:
    """A session's WAV, and which of its channels to use.

    Attributes:
        path: The WAV.
        rate: Nominal sample rate from its header.
        samples: Frames in the file.
        channels: Channels per frame.
        selected: Zero-based channels to mix, in order.
        label: How the selection reads in a report.
    """

    path: str
    rate: int
    samples: int
    channels: int
    selected: tuple[int, ...]
    label: str


def select_audio(path: str, requested: str, rig: Rig) -> AudioSelection | None:
    """Check the WAV and resolve a channel selection.

    Args:
        path: The session's WAV.
        requested: ``processed``, ``mix``, or a zero-based WAV channel.
        rig: The session's rig, whose ``channels`` name the physical
            microphones for ``mix``.

    Returns:
        The selection, or None if there is no readable WAV.

    Raises:
        ValueError: If the WAV is not 16-bit PCM or the selection is invalid.
    """
    try:
        with wave.open(path, "rb") as handle:
            if handle.getsampwidth() != 2:
                raise ValueError("only 16-bit PCM ReSpeaker WAV files are supported")
            channels = handle.getnchannels()
            rate = handle.getframerate()
            samples = handle.getnframes()
    except (FileNotFoundError, wave.Error):
        return None

    if requested == "processed":
        selected: tuple[int, ...] = (0,)
        label = "processed channel 0"
    elif requested == "mix":
        if rig.channels:
            selected = tuple(rig.channels)
            label = "physical microphone mix from rig"
        elif channels >= 5:
            selected = tuple(range(1, 5))
            label = "nominal ReSpeaker microphone mix (channels 1-4)"
        else:
            selected = tuple(range(channels))
            label = "all-channel mix"
    else:
        try:
            selected = (int(requested),)
        except ValueError as error:
            raise ValueError(
                "--audio-channel must be processed, mix, or a channel number"
            ) from error
        label = f"channel {selected[0]}"
    if not selected or any(channel < 0 or channel >= channels for channel in selected):
        raise ValueError(
            f"audio channel selection {selected} is outside {channels}-ch WAV"
        )
    return AudioSelection(path, rate, samples, channels, selected, label)


def audio_timeline(
    clock_path: str,
    rate: int,
    first_monotonic: float | None,
    *,
    fallback_start: float,
) -> tuple[AudioTimeline, bool]:
    """Use the measured sidecar, or an honest nominal fallback.

    Args:
        clock_path: The session's ``audio.clock.jsonl``.
        rate: Nominal sample rate from the WAV header.
        first_monotonic: When the first sample arrived, if the manifest says.
        fallback_start: Where sample zero goes when nothing else is known.

    Returns:
        The timeline, and whether it came from the measured sidecar.
    """
    try:
        return AudioTimeline.read(clock_path, rate), True
    except (OSError, ValueError):
        start = first_monotonic if first_monotonic is not None else fallback_start
        return AudioTimeline([AudioClockPoint(sample=0, monotonic=start)], rate), False


def read_mono(audio: AudioSelection) -> np.ndarray:
    """Read the selected channels as one zero-mean mono signal.

    Args:
        audio: What to read.

    Returns:
        float32 samples, one per WAV frame.
    """
    selected = list(audio.selected)
    parts: list[np.ndarray] = []
    with wave.open(audio.path, "rb") as handle:
        while True:
            raw = handle.readframes(_CHUNK_SAMPLES)
            if not raw:
                break
            block = np.frombuffer(raw, dtype="<i2").reshape(-1, audio.channels)
            parts.append(block[:, selected].astype(np.float32).mean(axis=1))
    mono = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return mono - mono.mean() if len(mono) else mono


def resample_onto_video(
    audio: AudioSelection,
    timeline: AudioTimeline,
    video_start: float,
    duration: float,
    offset: float,
    block: int,
) -> Iterator[tuple[np.ndarray, int]]:
    """Yield the selected channels as mono, resampled onto the video clock.

    Output sample ``n`` is the instant ``video_start + n / rate`` on the video
    clock, read from the WAV through ``timeline`` after removing ``offset``.
    Instants the WAV does not cover are silence.

    Args:
        audio: What to read.
        timeline: Where each WAV sample sits on the monotonic clock.
        video_start: Video time of output sample zero.
        duration: Seconds to cover.
        offset: Seconds to add to an audio time to reach the video time of the
            same instant.
        block: Output samples per yielded block.

    Yields:
        ``(samples, first)``: little-endian int16 mono, and the output sample
        number of its first element.
    """
    rate = int(audio.rate)
    total = math.ceil(duration * rate)
    with wave.open(audio.path, "rb") as handle:
        for output_start in range(0, total, block):
            count = min(block, total - output_start)
            first_video_time = video_start + output_start / rate
            first_source = timeline.sample_at(first_video_time - offset)
            source_step = (
                timeline.sample_at(first_video_time + 1.0 / rate - offset)
                - first_source
            )
            source_positions = first_source + np.arange(count) * source_step
            rendered = np.zeros(count, dtype=np.float64)
            valid = (source_positions >= 0) & (source_positions < audio.samples)
            if np.any(valid):
                first = max(0, math.floor(float(source_positions[valid].min())))
                last = min(
                    int(audio.samples),
                    math.ceil(float(source_positions[valid].max())) + 2,
                )
                handle.setpos(first)
                raw = handle.readframes(last - first)
                source = np.frombuffer(raw, dtype="<i2").reshape(-1, audio.channels)
                mono = source[:, list(audio.selected)].astype(np.float64).mean(axis=1)
                rendered[valid] = np.interp(
                    source_positions[valid], np.arange(first, last), mono
                )
            yield np.clip(np.rint(rendered), -32768, 32767).astype("<i2"), output_start


def sample_range(
    timeline: AudioTimeline | None, time_range: TimeRange, total: int
) -> tuple[int, int]:
    """Return the half-open WAV sample range a time interval covers.

    Args:
        timeline: Where each sample sits, or None if nothing measured it.
        time_range: The interval on the monotonic clock.
        total: Samples in the WAV.

    Returns:
        ``(start, end)``, clamped to the file. The whole file when there is no
        timeline to place the interval with.
    """
    start = 0
    end = total
    if timeline is not None and time_range.start is not None:
        start = max(0, math.ceil(timeline.sample_at(time_range.start)))
    if timeline is not None and time_range.end is not None:
        end = min(total, math.ceil(timeline.sample_at(time_range.end)))
    start = min(start, total)
    return start, max(start, end)


def crop_clock_points(
    timeline: AudioTimeline, start: int, end: int
) -> list[AudioClockPoint]:
    """Rebase measured clock points onto samples ``start`` up to ``end``.

    Args:
        timeline: The measured timeline of the whole WAV.
        start: First sample kept.
        end: First sample dropped.

    Returns:
        Points numbered from the cropped file's sample zero, with both edges
        interpolated so the crop is covered end to end.
    """
    points = [AudioClockPoint(0, timeline.monotonic_at(start), 0)]
    points.extend(
        AudioClockPoint(point.sample - start, point.monotonic, point.filled)
        for point in timeline.points
        if start < point.sample < end
    )
    final = AudioClockPoint(end - start, timeline.monotonic_at(end), 0)
    if final.sample != points[-1].sample:
        points.append(final)
    return points
