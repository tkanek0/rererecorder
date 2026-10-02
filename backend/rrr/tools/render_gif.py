"""Make a short GIF from a recorded colour stream, optionally showing its sound.

A presentation copy, like ``render_mp4``; see docs/features.md "MP4 review
copies".

Usage::

    uv run python -m rrr.tools.render_gif data/sessions/walk-01
    uv run python -m rrr.tools.render_gif data/sessions/walk-01 --volume
    uv run python -m rrr.tools.render_gif data/sessions/walk-01 --volume --waveform \\
        -o /tmp/walk-01.gif
"""

from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import av.filter
import cv2
import numpy as np

from rrr.playback import audio_timeline, frame_times, read_mono, select_audio
from rrr.timeline import AudioTimeline, SessionError, SessionPaths, read_manifest
from rrr.video import ArchiveSource, StreamError, color_to_bgr

DEFAULT_OUTPUT = "video.gif"
# GIF frame delays are stored in hundredths of a second.
GIF_TIME_BASE = Fraction(1, 100)
PAD = 4
VOLUME_HEIGHT = 30
WAVEFORM_HEIGHT = 70
BACKGROUND = (16, 16, 16)
# BGR.
PLAYED = (210, 180, 150)
UNPLAYED = (110, 90, 70)
PLAYHEAD = (60, 60, 255)
WAVE = (140, 220, 120)
AXIS = (50, 50, 50)
LABEL = (190, 190, 190)
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.35
# Amplitude that fills the waveform strip; a high percentile so that one click
# does not flatten the rest of the recording.
WAVEFORM_PERCENTILE = 99.95


@dataclass(frozen=True)
class GifReport:
    """What was placed in a rendered GIF."""

    output: str
    frames: int
    duration_s: float
    audio_channel: str | None
    clock: str
    offset_s: float | None


@dataclass(frozen=True)
class _Audio:
    """The selected channels, mixed to mono, and where they sit in time."""

    samples: np.ndarray
    rate: int
    timeline: AudioTimeline
    offset: float
    label: str
    channels: tuple[int, ...]
    loudness: np.ndarray
    scale: float

    def sample_at(self, stamp: float) -> int:
        """Return the sample recorded at a video-clock time."""
        return round(self.timeline.sample_at(stamp - self.offset))


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Make a short GIF from a recorded colour stream.",
    )
    parser.add_argument("directory", help="recorded session directory")
    parser.add_argument(
        "-o", "--output", default=DEFAULT_OUTPUT, help=f"GIF path (default: ./{DEFAULT_OUTPUT})"
    )
    parser.add_argument("--volume", action="store_true", help="add the loudness strip")
    parser.add_argument("--waveform", action="store_true", help="add the waveform strip")
    parser.add_argument(
        "--audio-channel",
        default="mix",
        help="mix (default), processed, or a zero-based WAV channel",
    )
    parser.add_argument("--stride", type=int, default=15, help="use every Nth frame (default: 15)")
    parser.add_argument("--width", type=int, default=360, help="GIF width in pixels (default: 360)")
    parser.add_argument(
        "--frame-ms", type=int, default=80, help="display time per frame, 10 ms steps (default: 80)"
    )
    parser.add_argument(
        "--window-s", type=float, default=0.5, help="waveform window in seconds (default: 0.5)"
    )
    parser.add_argument(
        "--colours", type=int, default=96, help="palette size per frame (default: 96)"
    )
    parser.add_argument("--force", action="store_true", help="replace an existing GIF")
    args = parser.parse_args(argv)

    try:
        report = render(
            args.directory,
            args.output,
            volume=args.volume,
            waveform=args.waveform,
            audio_channel=args.audio_channel,
            stride=args.stride,
            width=args.width,
            frame_ms=args.frame_ms,
            window_s=args.window_s,
            colours=args.colours,
            overwrite=args.force,
        )
    except (OSError, ValueError, SessionError, StreamError, av.FFmpegError) as error:
        parser.exit(1, f"render failed: {error}\n")

    print(f"rendered {report.frames} frames from {report.duration_s:.3f} s to {report.output}")
    print(f"  audio: {report.audio_channel or 'none'}")
    print(f"  clock: {report.clock}")
    print(
        "  offset: "
        + (f"{report.offset_s:+.6f} s" if report.offset_s is not None else "unmeasured")
    )
    return 0


def render(
    directory: str | Path,
    output: str | Path,
    *,
    volume: bool = False,
    waveform: bool = False,
    audio_channel: str = "mix",
    stride: int = 15,
    width: int = 360,
    frame_ms: int = 80,
    window_s: float = 0.5,
    colours: int = 96,
    overwrite: bool = False,
) -> GifReport:
    """Render one raw session as an animated GIF.

    Written to a temporary sibling and moved into place only on success.

    Args:
        directory: The recorded session.
        output: Where the GIF goes.
        volume: Add the loudness strip.
        waveform: Add the waveform strip.
        audio_channel: ``mix``, ``processed``, or a zero-based WAV channel.
        stride: Use every ``stride``-th colour frame.
        width: GIF width in pixels; height follows the camera's aspect.
        frame_ms: Display time per frame, rounded to GIF's 10 ms steps.
        window_s: Length of the waveform strip.
        colours: Palette size, chosen afresh for every frame.
        overwrite: Replace an existing ``output``.

    Returns:
        What was rendered.

    Raises:
        ValueError: If a parameter is out of range, the session has no colour
            video, or a strip was asked for without a WAV to draw it from.
        FileExistsError: If ``output`` exists and ``overwrite`` is false.
    """
    if stride < 1 or width < 1 or window_s <= 0 or not 2 <= colours <= 256:
        raise ValueError("stride, width and window_s must be positive; colours 2-256")
    delay = round(frame_ms / 10)
    if delay < 1:
        raise ValueError("frame_ms must be at least 10")

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

    with ArchiveSource(paths.video) as archive:
        times, measured = frame_times(archive, manifest.video.fps)
        clock_description = (
            "recorded monotonic video and audio clocks"
            if measured
            else "nominal video rate; audio starts at frame zero"
        )
        if not times:
            raise ValueError("the video archive is empty")
        start = times[0][1]
        offset = manifest.calibration.offset_s

        audio = None
        if volume or waveform:
            description = select_audio(paths.audio, audio_channel, manifest.rig)
            if description is None:
                raise ValueError("--volume and --waveform need the session's WAV")
            timeline, used_clock = audio_timeline(
                paths.audio_clock,
                description.rate,
                manifest.audio.first_monotonic if manifest.audio else None,
                fallback_start=start,
            )
            if not used_clock:
                clock_description = "nominal rates; tracks start together"
            samples = read_mono(description)
            audio = _Audio(
                samples=samples,
                rate=description.rate,
                timeline=timeline,
                offset=offset or 0.0,
                label=description.label,
                channels=description.selected,
                loudness=_column_rms(samples, width),
                scale=_waveform_scale(samples),
            )

        selected = times[::stride]
        images = (
            _compose(archive, index, stamp, start, width, audio, volume, waveform, window_s)
            for index, stamp in selected
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.stem}.", suffix=".gif", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o644)
        try:
            _encode(temporary, images, delay, colours)
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    return GifReport(
        output=str(output),
        frames=len(selected),
        duration_s=times[-1][1] - start,
        audio_channel=audio.label if audio is not None else None,
        clock=clock_description,
        offset_s=offset,
    )


def _compose(
    archive: ArchiveSource,
    index: int,
    stamp: float,
    start: float,
    width: int,
    audio: _Audio | None,
    volume: bool,
    waveform: bool,
    window_s: float,
) -> np.ndarray:
    """Build one GIF frame: the scaled colour image and the requested strips."""
    frames = archive.frame_at(index, only="color")
    image = color_to_bgr(frames) if frames is not None else None
    if image is None:
        raise ValueError(f"frame {index} has no colour image")
    height = max(round(image.shape[0] * width / image.shape[1]), 1)
    image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    if audio is None:
        return image

    strips: list[np.ndarray] = []
    sample = audio.sample_at(stamp)
    if volume:
        strips.append(_volume_strip(audio, width, sample))
    if waveform:
        strips.append(_waveform_strip(audio, width, sample, window_s, stamp - start))
    rows = [image]
    for strip in strips:
        rows.append(np.full((PAD, width, 3), BACKGROUND, dtype=np.uint8))
        rows.append(strip)
    rows.append(np.full((PAD, width, 3), BACKGROUND, dtype=np.uint8))
    return np.vstack(rows)


def _volume_strip(audio: _Audio, width: int, sample: int) -> np.ndarray:
    """Draw the whole recording's loudness with a playhead at ``sample``."""
    strip = np.full((VOLUME_HEIGHT, width, 3), BACKGROUND, dtype=np.uint8)
    head = int(np.clip(sample / max(len(audio.samples), 1), 0.0, 1.0) * (width - 1))
    for x, value in enumerate(audio.loudness):
        top = VOLUME_HEIGHT - 1 - round(value * (VOLUME_HEIGHT - 1))
        strip[top:, x] = PLAYED if x <= head else UNPLAYED
    cv2.line(strip, (head, 0), (head, VOLUME_HEIGHT - 1), PLAYHEAD, 2)
    return strip


def _column_rms(samples: np.ndarray, width: int) -> np.ndarray:
    """RMS of ``width`` equal slices, scaled so the loudest is 1."""
    rms = np.array(
        [
            float(np.sqrt(np.mean(np.square(part, dtype=np.float64)))) if len(part) else 0.0
            for part in np.array_split(samples, width)
        ]
    )
    peak = rms.max() if len(rms) else 0.0
    return rms / peak if peak > 0 else rms


def _waveform_strip(
    audio: _Audio, width: int, sample: int, window_s: float, elapsed: float
) -> np.ndarray:
    """Draw the ``window_s`` seconds ending at ``sample`` as a min/max envelope."""
    scale = audio.scale
    length = max(round(window_s * audio.rate), 1)
    window = np.zeros(length, dtype=np.float32)
    first = sample - length
    source = audio.samples[max(first, 0) : max(min(sample, len(audio.samples)), 0)]
    if len(source):
        window[max(-first, 0) : max(-first, 0) + len(source)] = source

    strip = np.full((WAVEFORM_HEIGHT, width, 3), BACKGROUND, dtype=np.uint8)
    middle = (WAVEFORM_HEIGHT - 1) / 2
    cv2.line(strip, (0, round(middle)), (width - 1, round(middle)), AXIS, 1)
    for x, part in enumerate(np.array_split(window, width)):
        if not len(part):
            continue
        high = float(np.clip(part.max() / scale, -1.0, 1.0))
        low = float(np.clip(part.min() / scale, -1.0, 1.0))
        cv2.line(
            strip,
            (x, round(middle - high * middle)),
            (x, round(middle - low * middle)),
            WAVE,
            1,
        )
    cv2.putText(
        strip,
        f"ch {'+'.join(map(str, audio.channels))}, last {window_s:g} s",
        (PAD, 11),
        FONT,
        FONT_SCALE,
        LABEL,
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        strip, f"t={elapsed:5.1f} s", (width - 62, 11), FONT, FONT_SCALE, LABEL, 1, cv2.LINE_AA
    )
    return strip


def _waveform_scale(samples: np.ndarray) -> float:
    """The amplitude that fills the waveform strip, shared by every frame."""
    value = float(np.percentile(np.abs(samples), WAVEFORM_PERCENTILE)) if len(samples) else 0.0
    return value if value > 0 else 1.0


def _encode(output: Path, images: Any, delay: int, colours: int) -> None:
    """Encode BGR images as a looping GIF with a palette chosen per frame.

    FFmpeg rather than OpenCV: OpenCV's global palette measured about 3x further
    from the source, and its local-palette mode fails inside the encoder.
    """
    iterator = iter(images)
    first = next(iterator, None)
    if first is None:
        raise ValueError("no frames to encode")
    height, width = first.shape[:2]

    with av.open(str(output), "w", format="gif") as container:
        stream = container.add_stream("gif", rate=Fraction(100, delay))
        stream.width = width
        stream.height = height
        stream.pix_fmt = "pal8"
        stream.codec_context.time_base = GIF_TIME_BASE

        graph = av.filter.Graph()
        source = graph.add_buffer(
            width=width, height=height, format="bgr24", time_base=GIF_TIME_BASE
        )
        split = graph.add("split")
        generate = graph.add("palettegen", f"stats_mode=single:max_colors={colours}")
        apply = graph.add("paletteuse", "new=1")
        sink = graph.add("buffersink")
        source.link_to(split)
        split.link_to(generate, 0, 0)
        split.link_to(apply, 1, 0)
        generate.link_to(apply, 0, 1)
        apply.link_to(sink)
        graph.configure()

        def drain() -> None:
            while True:
                try:
                    frame = sink.pull()
                except (av.BlockingIOError, av.EOFError):
                    return
                frame.time_base = GIF_TIME_BASE
                for packet in stream.encode(frame):
                    container.mux(packet)

        pts = 0
        image: np.ndarray | None = first
        while image is not None:
            if image.shape[:2] != (height, width):
                raise ValueError("every GIF frame must have the first frame's size")
            frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(image), format="bgr24")
            frame.pts = pts
            frame.time_base = GIF_TIME_BASE
            source.push(frame)
            drain()
            pts += delay
            image = next(iterator, None)
        source.push(None)
        drain()
        for packet in stream.encode():
            container.mux(packet)


if __name__ == "__main__":
    raise SystemExit(main())
