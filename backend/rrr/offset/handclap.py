"""Measure the offset between the camera and the array, from a handclap.

Nothing here writes: ``scripts/calibrate.py`` reports the measurement and stores
it only when asked, so ``calibration.offset_s`` stays null until somebody has
looked at it. The frame rate bounds the accuracy - see docs/decisions.md 14.
"""

from __future__ import annotations

import time
import wave
from dataclasses import dataclass

import numpy as np
from respeaker_adapter.config import CHANNEL_MICS

from rrr.playback import read_wav
from rrr.timeline import (
    AudioTimeline,
    SessionManifest,
    SessionPaths,
    SyncCalibration,
)
from rrr.video import ArchiveSource

#: How much louder than the preceding second a block must be to be a clap.
#: A clap is 20-40 dB over the floor; speech rises over tens of ms, not one.
ONSET_RATIO = 8.0

#: Analysis block for the audio energy, in seconds (32 samples at 16 kHz).
ONSET_BLOCK_S = 0.002

#: Seconds of quiet required before an onset counts, so that one clap's
#: reverberation is not read as a second clap.
ONSET_GAP_S = 0.5

#: How far either side of an audio impulse to look for the movement in video.
#: The expected offset is tens of ms; wider would admit unrelated movement.
SEARCH_S = 0.35


class OffsetError(ValueError):
    """Raised when a session cannot yield an offset at all."""


@dataclass(frozen=True)
class Clap:
    """One impulse in the audio, and the movement found for it in the video.

    Attributes:
        audio_at: Monotonic time of the impulse onset in the audio.
        frame: Archive index of the frame with the most movement nearby, or
            None if no frame was found.
        video_at: That frame's monotonic time, or None.
        sharpness: How far that movement stands out from the median; near 1
            is noise.
    """

    audio_at: float
    frame: int | None = None
    video_at: float | None = None
    sharpness: float | None = None

    @property
    def offset(self) -> float | None:
        """Seconds to add to the audio time to reach the video time, or None."""
        return None if self.video_at is None else self.video_at - self.audio_at


@dataclass(frozen=True)
class OffsetMeasurement:
    """What a session's claps say about the audio-to-video offset.

    Attributes:
        claps: Every impulse found, matched or not, in time order.
        frame_interval_s: One video frame, which bounds a single clap.
        stream: The video stream movement was looked for in.
    """

    claps: list[Clap]
    frame_interval_s: float
    stream: str

    @property
    def offsets(self) -> list[float]:
        """Offsets from the claps that found a movement."""
        return [clap.offset for clap in self.claps if clap.offset is not None]

    @property
    def offset_s(self) -> float | None:
        """Median offset, or None if no clap was matched."""
        return float(np.median(self.offsets)) if self.offsets else None

    @property
    def uncertainty_s(self) -> float | None:
        """Half a frame interval, averaged down over the matched claps."""
        count = len(self.offsets)
        return self.frame_interval_s / 2 / float(np.sqrt(count)) if count else None

    @property
    def spread_s(self) -> float | None:
        """How far the matched claps disagree, or None with fewer than two."""
        values = self.offsets
        return float(max(values) - min(values)) if len(values) > 1 else None

    @property
    def disagrees(self) -> bool:
        """Whether the claps disagree by more than two frame intervals."""
        spread = self.spread_s
        return spread is not None and spread > self.frame_interval_s * 2

    def to_calibration(self) -> SyncCalibration:
        """Return the measurement in the form ``session.json`` stores.

        Raises:
            OffsetError: If no clap was matched, so there is nothing to store.
        """
        if self.offset_s is None or self.uncertainty_s is None:
            raise OffsetError("no clap was matched; there is no offset to store")
        spread = self.spread_s
        return SyncCalibration(
            offset_s=self.offset_s,
            uncertainty_s=self.uncertainty_s,
            method="handclap",
            measured_at=time.time(),
            note=(
                f"{len(self.offsets)} clap(s), {self.stream} stream"
                + (f", {spread * 1000:.0f} ms spread" if spread is not None else "")
            ),
        )


def measure_offset(
    paths: SessionPaths, manifest: SessionManifest, stream: str = "ir1"
) -> OffsetMeasurement:
    """Measure a session's audio-to-video offset from its handclaps.

    Writes nothing: storing the result is the caller's decision.

    Args:
        paths: Where the session lives.
        manifest: Its manifest, already read.
        stream: Which video stream to look for movement in - ``ir1``, ``ir2``,
            ``color`` or ``depth``.

    Returns:
        Every clap and what was found for it. Possibly with none matched.

    Raises:
        OffsetError: If the session has only one device, no impulse in its
            audio, or no capture times in its archive.
    """
    if manifest.audio is None or manifest.video is None:
        raise OffsetError(
            "this session has only one device; there is no offset to measure"
        )
    impulses = _find_claps(paths)
    if not impulses:
        raise OffsetError(
            "no impulse found in the audio. Clap once or twice, close to the "
            "array and in view of the camera, and record a few seconds"
        )
    claps = []
    with ArchiveSource(paths.video) as archive:
        times = archive.frame_times()
        if not times:
            raise OffsetError("the archive stores no capture times")
        for audio_at in impulses:
            found = _find_movement(archive, times, audio_at, stream)
            if found is None:
                claps.append(Clap(audio_at))
            else:
                index, video_at, sharpness = found
                claps.append(Clap(audio_at, index, video_at, sharpness))
    return OffsetMeasurement(
        claps=claps,
        frame_interval_s=1.0 / (manifest.video.fps or 30.0),
        stream=stream,
    )


# -- the audio side -----------------------------------------------------------


def _find_claps(paths: SessionPaths) -> list[float]:
    """Find impulse onsets in the recording, on the monotonic axis.

    Args:
        paths: Where the session lives.

    Returns:
        The onset times, in order. Empty if the audio cannot be read.

    Uses the raw microphones: the processed channel's beamforming and gain
    control can move an onset.
    """
    try:
        samples, rate = read_wav(paths.audio)
    except (OSError, wave.Error):
        return []
    if not len(samples):
        return []

    channels = samples.shape[1]
    mics = samples[:, list(CHANNEL_MICS)] if channels > max(CHANNEL_MICS) else samples
    signal = np.abs(mics.astype(np.float32)).mean(axis=1)

    block = max(1, int(ONSET_BLOCK_S * rate))
    usable = len(signal) - len(signal) % block
    if usable < block * 2:
        return []
    energy = signal[:usable].reshape(-1, block).mean(axis=1)

    # A running median, unlike a mean, is not raised by the impulse itself.
    history = max(1, int(1.0 / ONSET_BLOCK_S))
    onsets: list[int] = []
    floor = float(np.median(energy[:history])) if len(energy) > history else 0.0
    last = -len(energy)
    for i in range(history, len(energy)):
        window = energy[max(0, i - history) : i]
        floor = float(np.median(window)) or floor
        if energy[i] > floor * ONSET_RATIO and (i - last) * ONSET_BLOCK_S > ONSET_GAP_S:
            onsets.append(i)
            last = i

    if not onsets:
        return []

    try:
        timeline = AudioTimeline.read(paths.audio_clock, rate)
    except (OSError, ValueError):
        return []
    # The block's start is the closest estimate of when the sound arrived.
    return [timeline.monotonic_at(index * block) for index in onsets]


# -- the video side -----------------------------------------------------------


def _find_movement(
    archive: ArchiveSource,
    times: list[tuple[int, float]],
    around: float,
    stream: str,
) -> tuple[int, float, float] | None:
    """Find the frame with the most movement near an instant.

    Args:
        archive: The open archive.
        times: ``(index, received_monotonic)`` for every frame.
        around: The audio impulse's time, to search either side of.
        stream: Which stream to difference.

    Returns:
        ``(index, received_monotonic, sharpness)`` for the frame where
        successive images differ most, or None if there are too few frames near
        that instant. ``sharpness`` is the peak over the median difference;
        under about 2 means no distinct movement.

    Hands meeting is the fastest movement in a clap, so the peak lands on the
    clap's frame, to within one frame.
    """
    window = [
        (index, at) for index, at in times if abs(at - around) <= SEARCH_S
    ]
    if len(window) < 4:
        return None

    only = "infrared" if stream.startswith("ir") else stream
    images = []
    for index, at in window:
        frames = archive.frame_at(index, only=only)
        if frames is None:
            continue
        image = _pick(frames, stream)
        if image is None:
            return None
        images.append((index, at, image.astype(np.float32)))
    if len(images) < 4:
        return None

    diffs = np.array(
        [
            np.abs(images[i][2] - images[i - 1][2]).mean()
            for i in range(1, len(images))
        ]
    )
    peak = int(np.argmax(diffs))
    median = float(np.median(diffs)) or 1e-9
    # diffs[i] is the change from image i to i + 1.
    index, at, _ = images[peak + 1]
    return index, at, float(diffs[peak] / median)


def _pick(frames, stream: str) -> np.ndarray | None:
    """The single-channel image a stream contributes, for differencing."""
    if stream == "ir1":
        return frames.infrared[0] if frames.infrared else None
    if stream == "ir2":
        return frames.infrared[1] if frames.infrared else None
    if stream == "depth":
        return frames.depth
    if frames.color is None:
        return None
    # YUYV: the luma plane alone, with no colour conversion.
    if frames.color_format == "yuyv":
        height, width = frames.color.shape
        return frames.color.view(np.uint8).reshape(height, width, 2)[:, :, 0]
    return frames.color.mean(axis=2)
