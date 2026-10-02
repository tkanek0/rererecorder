"""Measure the offset between the camera and the array, from a handclap.

    uv run python -m rrr.tools.calibrate data/sessions/2026-09-02_15-28-36
    uv run python -m rrr.tools.calibrate data/sessions/... --apply

Without ``--apply`` nothing is written and ``calibration.offset_s`` stays
null. The frame rate bounds the accuracy - see docs/decisions.md 14.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave

import numpy as np

from rrr.timeline import (
    AudioTimeline,
    SessionPaths,
    SyncCalibration,
    read_manifest,
    write_manifest,
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


def main(argv: list[str] | None = None) -> int:
    """Measure a session's audio-to-video offset.

    Args:
        argv: Command line arguments, or None to read them from the process.

    Returns:
        0 if an offset was measured, 1 if not.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="the session to calibrate")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the result into session.json",
    )
    parser.add_argument(
        "--stream",
        default="ir1",
        choices=("ir1", "ir2", "color", "depth"),
        help="which video stream to look for movement in (default: ir1)",
    )
    args = parser.parse_args(argv)

    root, _, session_id = args.directory.rstrip("/").rpartition("/")
    paths = SessionPaths.resolve(root or ".", session_id)
    manifest = read_manifest(paths)

    if manifest.audio is None or manifest.video is None:
        print(
            "this session has only one device; there is no offset to measure",
            file=sys.stderr,
        )
        return 1

    claps = _find_claps(paths)
    if not claps:
        print(
            "no impulse found in the audio. Clap once or twice, close to the "
            "array and in view of the camera, and record a few seconds",
            file=sys.stderr,
        )
        return 1

    print(f"session {manifest.session_id}")
    print(f"  impulses        {len(claps)} found in the audio")

    offsets = []
    with ArchiveSource(paths.video) as archive:
        times = archive.frame_times()
        if not times:
            print("  the archive stores no capture times", file=sys.stderr)
            return 1
        for n, audio_at in enumerate(claps, start=1):
            found = _find_movement(archive, times, audio_at, args.stream)
            if found is None:
                print(f"  clap {n}          audio {audio_at:.3f} s - no movement found")
                continue
            index, video_at, sharpness = found
            # SyncCalibration's sign: added to an audio time, gives video time.
            offset = video_at - audio_at
            offsets.append(offset)
            # Capped: a still scene has a zero median and an unbounded ratio.
            print(
                f"  clap {n}          audio {audio_at:.3f} s, video {video_at:.3f} s "
                f"(frame {index}) -> {offset * 1000:+.1f} ms"
                f"   [movement {min(sharpness, 999.0):.0f}x the median]"
            )

    if not offsets:
        print(
            "impulses were found but no matching movement was. Was the clap in "
            "shot?",
            file=sys.stderr,
        )
        return 1

    values = np.array(offsets)
    median = float(np.median(values))
    spread = float(np.max(values) - np.min(values)) if len(values) > 1 else None
    # One frame interval bounds a single clap; several claps average it down.
    interval = 1.0 / (manifest.video.fps or 30.0)
    uncertainty = interval / 2 / np.sqrt(len(values))

    print()
    print(f"  offset          {median * 1000:+.1f} ms")
    print(
        f"  uncertainty     +/- {uncertainty * 1000:.1f} ms "
        f"(half a frame over sqrt({len(values)}) claps)"
    )
    if spread is not None:
        print(f"  spread          {spread * 1000:.1f} ms across the claps")
        if spread > interval * 2:
            print(
                "  WARNING         the claps disagree by more than two frame "
                "intervals; something other than a clap may have been detected"
            )
    print(
        "\n  Add the offset to an audio time to reach the video time of the same "
        "instant;\n  negative means the audio's clock reads later."
    )

    if not args.apply:
        print("\n  Not written. Pass --apply to store it in session.json.")
        return 0

    manifest.calibration = SyncCalibration(
        offset_s=median,
        uncertainty_s=float(uncertainty),
        method="handclap",
        measured_at=time.time(),
        note=(
            f"{len(values)} clap(s), {args.stream} stream"
            + (f", {spread * 1000:.0f} ms spread" if spread is not None else "")
        ),
    )
    write_manifest(paths, manifest)
    print(f"\n  Written to {paths.manifest}")
    return 0


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
        with wave.open(paths.audio, "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            raw = handle.readframes(handle.getnframes())
    except (OSError, wave.Error):
        return []
    if not raw:
        return []

    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
    # Channel 0 is the processed one; 1-4 are the microphones on a ReSpeaker.
    mics = samples[:, 1:5] if channels >= 5 else samples
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


if __name__ == "__main__":
    raise SystemExit(main())
