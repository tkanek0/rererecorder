"""Make a reviewable MP4 from a recorded colour stream and ReSpeaker audio.

The archive and WAV remain the measurements; this is a presentation copy.  It
uses their recorded monotonic clocks, applies a measured audio/video offset when
one exists, and resamples the WAV onto the video's time axis.  With no clock or
calibration it still makes a useful movie by starting both tracks together.

Usage::

    uv run python -m rrr.tools.render_mp4 data/sessions/walk-01
    uv run python -m rrr.tools.render_mp4 data/sessions/walk-01 -o walk-01.mp4
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import tempfile
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np

from rrr.timeline import (
    AudioClockPoint,
    AudioTimeline,
    Rig,
    SessionError,
    SessionPaths,
    read_manifest,
)
from rrr.video import ArchiveSource, StreamError

VIDEO_TIME_BASE = Fraction(1, 90_000)
AUDIO_FRAME_SAMPLES = 1_024
DOA_STALE_S = 0.5


@dataclass(frozen=True)
class _Direction:
    """One direction reading on the video clock."""

    time: float
    angle: float
    voice: bool


@dataclass(frozen=True)
class RenderReport:
    """What was placed in a rendered movie."""

    output: str
    frames: int
    duration_s: float
    audio: bool
    audio_channel: str | None
    clock: str
    offset_s: float | None
    doa: str


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Combine a recorded RealSense colour stream and ReSpeaker audio.",
    )
    parser.add_argument("directory", help="recorded session directory")
    parser.add_argument("-o", "--output", help="MP4 path (default: <session>.mp4)")
    parser.add_argument(
        "--audio-channel",
        default="processed",
        help="processed (default), mix, or a zero-based WAV channel",
    )
    parser.add_argument("--no-doa", action="store_true", help="do not draw DOA")
    parser.add_argument("--crf", type=int, default=20, help="H.264 CRF (default: 20)")
    parser.add_argument("--force", action="store_true", help="replace an existing MP4")
    args = parser.parse_args(argv)

    directory = Path(args.directory)
    output = Path(args.output) if args.output else Path(f"{directory.name}.mp4")
    try:
        report = render(
            directory,
            output,
            audio_channel=args.audio_channel,
            draw_doa=not args.no_doa,
            crf=args.crf,
            overwrite=args.force,
        )
    except (OSError, ValueError, SessionError, StreamError, av.FFmpegError) as error:
        parser.exit(1, f"render failed: {error}\n")

    print(
        f"rendered {report.frames} frames / {report.duration_s:.3f} s "
        f"to {report.output}"
    )
    print(f"  audio: {report.audio_channel if report.audio else 'none'}")
    print(f"  clock: {report.clock}")
    print(
        "  offset: "
        + (f"{report.offset_s:+.6f} s" if report.offset_s is not None else "unmeasured")
    )
    print(f"  DOA: {report.doa}")
    return 0


def render(
    directory: str | Path,
    output: str | Path,
    *,
    audio_channel: str = "processed",
    draw_doa: bool = True,
    crf: int = 20,
    overwrite: bool = False,
) -> RenderReport:
    """Render one raw session as H.264 video with mono AAC audio.

    A temporary sibling file is replaced into place only after both encoders
    have closed successfully.  The source recording is never modified.
    """
    directory = Path(directory)
    root = str(directory.parent) if str(directory.parent) else "."
    paths = SessionPaths.resolve(root, directory.name)
    manifest = read_manifest(paths)
    if manifest.video is None:
        raise ValueError("the session has no video track")

    output = Path(output)
    if output.exists() and not overwrite:
        raise FileExistsError(f"{output} exists; pass --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".mp4", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    os.chmod(temporary, 0o644)

    try:
        with ArchiveSource(paths.video) as archive:
            times = archive.frame_times()
            if not times:
                fps = manifest.video.fps or 30.0
                times = [(index, n / fps) for n, index in enumerate(archive.indices())]
                clock_description = "nominal video rate; audio starts at frame zero"
            else:
                clock_description = "recorded monotonic video and audio clocks"
            if not times:
                raise ValueError("the video archive is empty")

            fps = _frame_rate(times, manifest.video.fps)
            start = times[0][1]
            duration = max(times[-1][1] - start + 1.0 / fps, 1.0 / fps)
            offset = manifest.calibration.offset_s
            directions, doa_mode = _directions(
                paths.doa,
                offset or 0.0,
                manifest.rig,
                archive.calibration.depth_to_color,
                enabled=draw_doa and manifest.doa_file is not None,
            )

            audio = _audio_description(paths.audio, audio_channel, manifest.rig)
            timeline = None
            if audio is not None:
                timeline, used_clock = _audio_timeline(
                    paths.audio_clock,
                    audio["rate"],
                    manifest.audio.first_monotonic if manifest.audio else None,
                    fallback_start=start,
                )
                if not used_clock:
                    clock_description = "nominal rates; tracks start together"

            _encode(
                temporary,
                archive,
                times,
                fps,
                start,
                duration,
                directions,
                doa_mode,
                audio,
                timeline,
                offset or 0.0,
                crf,
                manifest.session_id,
            )
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return RenderReport(
        output=str(output),
        frames=len(times),
        duration_s=duration,
        audio=audio is not None,
        audio_channel=audio["label"] if audio is not None else None,
        clock=clock_description,
        offset_s=offset,
        doa=doa_mode,
    )


def _frame_rate(times: list[tuple[int, float]], recorded: float | None) -> float:
    """Choose an encoder hint without replacing the recorded frame times."""
    if recorded is not None and recorded > 0:
        return recorded
    gaps = np.diff([stamp for _, stamp in times])
    positive = gaps[gaps > 0]
    return float(1.0 / np.median(positive)) if len(positive) else 30.0


def _audio_description(path: str, requested: str, rig: Rig) -> dict[str, Any] | None:
    """Check the WAV and resolve the channel selection."""
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
        selected = (0,)
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
        raise ValueError(f"audio channel selection {selected} is outside {channels}-ch WAV")
    return {
        "path": path,
        "rate": rate,
        "samples": samples,
        "channels": channels,
        "selected": selected,
        "label": label,
    }


def _audio_timeline(
    clock_path: str,
    rate: int,
    first_monotonic: float | None,
    *,
    fallback_start: float,
) -> tuple[AudioTimeline, bool]:
    """Use the measured sidecar, or an honest nominal fallback."""
    try:
        return AudioTimeline.read(clock_path, rate), True
    except (OSError, ValueError):
        start = first_monotonic if first_monotonic is not None else fallback_start
        return AudioTimeline([AudioClockPoint(sample=0, monotonic=start)], rate), False


def _encode(
    output: Path,
    archive: ArchiveSource,
    times: list[tuple[int, float]],
    fps: float,
    start: float,
    duration: float,
    directions: list[_Direction],
    doa_mode: str,
    audio: dict[str, Any] | None,
    timeline: AudioTimeline | None,
    offset: float,
    crf: int,
    session_id: str,
) -> None:
    """Encode and mux both tracks into one temporary MP4."""
    first = archive.frame_at(times[0][0], only="color")
    if first is None or first.color is None:
        raise ValueError("the archive has no colour frames")
    first_image = _bgr(first)
    height, width = first_image.shape[:2]
    if width % 2 or height % 2:
        raise ValueError("H.264 requires an even colour frame width and height")

    with av.open(str(output), "w", options={"movflags": "+faststart"}) as container:
        container.metadata["title"] = session_id
        container.metadata["comment"] = (
            "Review copy generated by rererecorder; source archive remains authoritative"
        )
        video_stream = container.add_stream("libx264", rate=Fraction(fps).limit_denominator(1001))
        video_stream.width = width
        video_stream.height = height
        video_stream.pix_fmt = "yuv420p"
        video_stream.time_base = VIDEO_TIME_BASE
        # The stream time base controls the MP4 track, while the codec context
        # controls how x264 quantises input PTS.  Leaving the latter at 1/fps
        # makes two real frames less than one nominal interval apart collapse
        # onto the same DTS; walk recordings contain ordinary 31/47 ms jitter
        # around 29.967 fps and MP4 correctly rejects that duplicate timestamp.
        video_stream.codec_context.time_base = VIDEO_TIME_BASE
        video_stream.options = {"crf": str(crf), "preset": "medium"}
        audio_stream = None
        if audio is not None and timeline is not None:
            audio_stream = container.add_stream("aac", rate=audio["rate"])
            audio_stream.layout = "mono"
            audio_stream.bit_rate = 128_000

        previous_pts = -1
        for position, (index, stamp) in enumerate(times):
            frames = first if position == 0 else archive.frame_at(index, only="color")
            if frames is None or frames.color is None:
                raise ValueError(f"frame {index} has no colour image")
            image = _bgr(frames)
            _draw_direction(image, stamp, directions, doa_mode)
            frame = av.VideoFrame.from_ndarray(image, format="bgr24")
            pts = max(previous_pts + 1, round((stamp - start) / float(VIDEO_TIME_BASE)))
            frame.pts = pts
            frame.time_base = VIDEO_TIME_BASE
            for packet in video_stream.encode(frame):
                container.mux(packet)
            previous_pts = pts
        for packet in video_stream.encode():
            container.mux(packet)

        if audio_stream is not None and audio is not None and timeline is not None:
            for samples, pts in _audio_frames(audio, timeline, start, duration, offset):
                frame = av.AudioFrame.from_ndarray(samples[np.newaxis, :], "s16", "mono")
                frame.sample_rate = audio["rate"]
                frame.pts = pts
                frame.time_base = Fraction(1, audio["rate"])
                for packet in audio_stream.encode(frame):
                    container.mux(packet)
            for packet in audio_stream.encode():
                container.mux(packet)


def _bgr(frames: Any) -> np.ndarray:
    """Convert either recorded colour representation into encoder-ready BGR."""
    if frames.color_format == "yuyv":
        height, width = frames.color.shape
        packed = frames.color.view(np.uint8).reshape(height, width, 2)
        return cv2.cvtColor(packed, cv2.COLOR_YUV2BGR_YUY2)
    return cv2.cvtColor(frames.color, cv2.COLOR_RGB2BGR)


def _audio_frames(
    audio: dict[str, Any],
    timeline: AudioTimeline,
    video_start: float,
    duration: float,
    offset: float,
):
    """Yield clock-corrected mono blocks covering the complete video."""
    rate = int(audio["rate"])
    total = math.ceil(duration * rate)
    with wave.open(audio["path"], "rb") as handle:
        for output_start in range(0, total, AUDIO_FRAME_SAMPLES):
            count = min(AUDIO_FRAME_SAMPLES, total - output_start)
            first_video_time = video_start + output_start / rate
            first_source = timeline.sample_at(first_video_time - offset)
            source_step = (
                timeline.sample_at(first_video_time + 1.0 / rate - offset)
                - first_source
            )
            source_positions = first_source + np.arange(count) * source_step
            rendered = np.zeros(count, dtype=np.float64)
            valid = (source_positions >= 0) & (source_positions < audio["samples"])
            if np.any(valid):
                first = max(0, math.floor(float(source_positions[valid].min())))
                last = min(
                    int(audio["samples"]),
                    math.ceil(float(source_positions[valid].max())) + 2,
                )
                handle.setpos(first)
                raw = handle.readframes(last - first)
                source = np.frombuffer(raw, dtype="<i2").reshape(-1, audio["channels"])
                mono = source[:, audio["selected"]].astype(np.float64).mean(axis=1)
                rendered[valid] = np.interp(
                    source_positions[valid], np.arange(first, last), mono
                )
            yield np.clip(np.rint(rendered), -32768, 32767).astype("<i2"), output_start


def _directions(
    path: str,
    offset: float,
    rig: Rig,
    depth_to_color: Any,
    *,
    enabled: bool,
) -> tuple[list[_Direction], str]:
    """Read DOA and, when fully described, rotate it into the colour camera."""
    if not enabled:
        return [], "off"
    readings: list[_Direction] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line)
                readings.append(
                    _Direction(
                        time=float(raw["t"]) + offset,
                        angle=float(raw["angle"]) % 360.0,
                        voice=bool(raw.get("voice", False)),
                    )
                )
    except FileNotFoundError:
        return [], "unavailable"
    readings.sort(key=lambda reading: reading.time)
    if not readings:
        return [], "unavailable"
    if not rig.known or depth_to_color is None:
        return readings, "array coordinates (rig unset)"

    rotation = np.asarray(depth_to_color.rotation).reshape(3, 3) @ np.asarray(
        rig.rotation
    ).reshape(3, 3)
    corrected = []
    for reading in readings:
        radians = math.radians(reading.angle)
        # Array convention used by this renderer: 0° is +Y and angles increase
        # clockwise toward +X.  A rig rotation defines those physical axes.
        ray = rotation @ np.array([math.sin(radians), math.cos(radians), 0.0])
        bearing = math.degrees(math.atan2(float(ray[0]), float(ray[2]))) % 360.0
        corrected.append(_Direction(reading.time, bearing, reading.voice))
    return corrected, f"colour-camera coordinates ({rig.source} rig)"


def _draw_direction(
    image: np.ndarray,
    stamp: float,
    readings: list[_Direction],
    mode: str,
) -> None:
    """Draw the freshest non-stale DOA reading as a top-down compass."""
    if not readings:
        return
    position = bisect.bisect_right(readings, stamp, key=lambda reading: reading.time) - 1
    if position < 0:
        return
    reading = readings[position]
    if stamp - reading.time > DOA_STALE_S:
        return
    radius = max(28, min(image.shape[:2]) // 14)
    centre = (image.shape[1] - radius - 18, radius + 18)
    colour = (40, 220, 40) if reading.voice else (160, 160, 160)
    cv2.circle(image, centre, radius, (240, 240, 240), 2, cv2.LINE_AA)
    radians = math.radians(reading.angle)
    tip = (
        round(centre[0] + radius * 0.82 * math.sin(radians)),
        round(centre[1] - radius * 0.82 * math.cos(radians)),
    )
    cv2.arrowedLine(image, centre, tip, colour, 3, cv2.LINE_AA, tipLength=0.25)
    label = f"DOA {reading.angle:.0f} deg {'camera' if mode.startswith('colour') else 'array'}"
    cv2.putText(
        image,
        label,
        (12, image.shape[0] - 16),
        cv2.FONT_HERSHEY_SIMPLEX,
        max(0.45, image.shape[1] / 1800.0),
        colour,
        2,
        cv2.LINE_AA,
    )


if __name__ == "__main__":
    raise SystemExit(main())
