"""Record a session from the terminal.

    uv run python scripts/record.py --seconds 20
    uv run python scripts/record.py --session kitchen-test --no-doa
    uv run python scripts/record.py --no-depth --no-infrared --color-codec raw
    uv run python scripts/record.py --seconds 600 --min-fps 29.5

The same :class:`~rrr.recorder.SessionRecorder` the server uses, with a progress
line instead of a browser, and no web stack needed.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from inspect_session import print_inspection
from realsense_adapter import StreamConfig
from rrr.inspection import inspect_session
from rrr.recorder import SessionRecorder, config
from rrr.timeline import SessionManifest


def main(argv: list[str] | None = None) -> int:
    """Record one session.

    Args:
        argv: Command line arguments, or None to read them from the process.

    Returns:
        A process exit status; see ``_status``.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="how long to record; 0 runs until interrupted",
    )
    parser.add_argument(
        "--session", default=None, help="session directory name (default: the time)"
    )
    parser.add_argument(
        "--root",
        default=config.SESSIONS_ROOT,
        help=f"where sessions are created (default: {config.SESSIONS_ROOT})",
    )
    parser.add_argument("--no-video", action="store_true", help="skip the camera")
    parser.add_argument("--no-audio", action="store_true", help="skip the array")
    parser.add_argument("--no-doa", action="store_true", help="skip the direction")
    parser.add_argument(
        "--no-color", action="store_true", help="do not capture the color stream"
    )
    parser.add_argument(
        "--no-depth", action="store_true", help="do not capture the depth stream"
    )
    parser.add_argument(
        "--no-infrared",
        action="store_true",
        help="do not capture the infrared pair",
    )
    parser.add_argument(
        "--color-codec",
        choices=("compressed", "raw"),
        default=None,
        help="how the color stream is stored (default: from the environment)",
    )
    parser.add_argument(
        "--depth-codec",
        choices=("compressed", "raw"),
        default=None,
        help="how the depth stream is stored (default: from the environment)",
    )
    parser.add_argument(
        "--infrared-codec",
        choices=("compressed", "raw"),
        default=None,
        help="how the infrared pair is stored (default: from the environment)",
    )
    parser.add_argument("--quiet", action="store_true", help="no progress line")
    parser.add_argument(
        "--min-fps",
        type=float,
        default=None,
        help="count a video rate below this as a loss (exit status 2)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every discarded frame set and why",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        streams = _build_streams(args)
    except ValueError as error:
        print(f"could not start recording: {error}", file=sys.stderr)
        return 1

    recorder = SessionRecorder(
        args.root,
        streams=streams,
        serial=config.SERIAL,
        record_video=config.RECORD_VIDEO and not args.no_video,
        record_audio=config.RECORD_AUDIO and not args.no_audio,
        record_doa=config.RECORD_DOA and not args.no_doa,
        codecs=_build_codecs(args),
    )

    try:
        paths = recorder.start(args.session)
    except Exception as error:  # noqa: BLE001 - a failed start is a message
        print(f"could not start recording: {error}", file=sys.stderr)
        return 1

    print(f"recording to {paths.directory}", file=sys.stderr)
    try:
        _wait(recorder, args.seconds, quiet=args.quiet)
        manifest = recorder.stop()
    finally:
        # An unclosed capture stream makes the array's next open fail.
        recorder.close()

    print_inspection(manifest, inspect_session(paths, manifest))
    return _status(manifest, args.min_fps)


def _build_streams(args: argparse.Namespace) -> StreamConfig:
    """Apply --no-color/--no-depth/--no-infrared to the configured defaults.

    Args:
        args: Parsed command line arguments.

    Returns:
        The stream configuration to record.

    Raises:
        ValueError: If ``StreamConfig`` refuses the combination, e.g. infrared
            without depth.

    Skipped when ``--no-video`` is given, so an unused configuration cannot
    fail the recording.
    """
    if args.no_video:
        return config.DEFAULT_STREAMS
    off = {
        name: False
        for name in ("color", "depth", "infrared")
        if getattr(args, f"no_{name}")
    }
    return config.with_streams(config.DEFAULT_STREAMS, off)


def _build_codecs(args: argparse.Namespace) -> dict[str, str]:
    """Apply --color-codec/--depth-codec/--infrared-codec to the defaults.

    Args:
        args: Parsed command line arguments.

    Returns:
        The codec per stream.
    """
    choices = {
        stream: getattr(args, f"{stream}_codec")
        for stream in ("color", "depth", "infrared")
        if getattr(args, f"{stream}_codec") is not None
    }
    return config.with_codecs(config.CODECS, choices)


def _wait(recorder: SessionRecorder, seconds: float, *, quiet: bool) -> None:
    """Block until the recording should stop, showing progress.

    Args:
        recorder: The running recorder.
        seconds: How long to record, or 0 to wait for a signal.
        quiet: Suppress the progress line.

    SIGINT and SIGTERM stop the recording cleanly instead of killing the
    process, so the archive and manifest are finished.
    """
    stopping = False

    def on_signal(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    deadline = time.monotonic() + seconds if seconds > 0 else None
    while not stopping:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not recorder.recording:
            break
        if not quiet:
            _progress(recorder)
        time.sleep(0.5)
    if not quiet:
        print(file=sys.stderr)


def _progress(recorder: SessionRecorder) -> None:
    """Write one progress line, in place."""
    state = recorder.state()
    parts = [f"{state['seconds']:6.1f}s"]
    video = state["video"]
    if video is not None:
        parts.append(
            f"video {video['frames']:5d} frames"
            + (f" @{video['fps']:.1f}" if video["fps"] else "")
            + (f" DROPPED {video['dropped']}" if video["dropped"] else "")
        )
    audio = state["audio"]
    if audio is not None:
        parts.append(
            f"audio {audio['seconds']:6.1f}s"
            + (f" FILLED {audio['filled']}" if audio["filled"] else "")
        )
    parts.append(f"{state['size_bytes'] / 1e6:7.1f} MB")
    print("  " + " | ".join(parts) + "   ", end="\r", file=sys.stderr, flush=True)


def _status(manifest: SessionManifest, min_fps: float | None = None) -> int:
    """Turn a finished session into a process exit status.

    Args:
        manifest: The finished session.
        min_fps: Video rate below which the session counts as lossy, or None.

    Returns:
        0 if both tracks recorded cleanly, 1 if nothing was recorded, 2 if
        something was lost.
    """
    if manifest.video is None and manifest.audio is None:
        return 1
    # An empty track is a failure even if nothing raised: it is what a device
    # left in a bad state produces.
    if manifest.video is not None and not manifest.video.frames:
        return 1
    if manifest.audio is not None and not manifest.audio.samples:
        return 1
    if manifest.video is not None and manifest.video.dropped:
        return 2
    if (
        min_fps is not None
        and manifest.video is not None
        and (manifest.video.fps or 0.0) < min_fps
    ):
        return 2
    if manifest.audio is not None and manifest.audio.filled:
        return 2
    if manifest.errors:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
