"""Record a session from the terminal.

    uv run python -m tools.record --seconds 20
    uv run python -m tools.record --session kitchen-test --no-doa

The same :class:`~recorder.SessionRecorder` the server uses, with a progress
line instead of a browser. Nothing here needs the web stack, which is the point:
a machine that only records does not need one installed.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from recorder import SessionRecorder, config
from timeline import SessionManifest


def main(argv: list[str] | None = None) -> int:
    """Record one session.

    Args:
        argv: Command line arguments, or None to read them from the process.

    Returns:
        A process exit status. Non-zero when nothing was recorded, or when a
        recording finished with holes in it - a caller scripting this should
        find out without parsing the output.
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
    parser.add_argument("--quiet", action="store_true", help="no progress line")
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

    recorder = SessionRecorder(
        args.root,
        streams=config.DEFAULT_STREAMS,
        serial=config.SERIAL,
        record_video=config.RECORD_VIDEO and not args.no_video,
        record_audio=config.RECORD_AUDIO and not args.no_audio,
        record_doa=config.RECORD_DOA and not args.no_doa,
        codecs=config.CODECS,
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
        # The array is left in a state where the next open fails if its capture
        # stream is not closed before the process exits.
        recorder.close()

    _report(manifest, paths.directory)
    return _status(manifest)


def _wait(recorder: SessionRecorder, seconds: float, *, quiet: bool) -> None:
    """Block until the recording should stop, showing progress.

    Args:
        recorder: The running recorder.
        seconds: How long to record, or 0 to wait for a signal.
        quiet: Suppress the progress line.

    Ctrl-C stops the recording rather than killing the process: the archive has
    to be closed and the manifest finished, and a session abandoned mid-write is
    the one case where the files are hard to interpret afterwards.
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


def _report(manifest: SessionManifest, directory: str) -> None:
    """Print what the session holds, and what is wrong with it if anything."""
    print(f"session {manifest.session_id} in {directory}")
    if manifest.duration_s is not None:
        print(f"  duration        {manifest.duration_s:.2f} s")

    video = manifest.video
    if video is not None:
        print(
            f"  video           {video.frames} frames"
            + (f" at {video.fps:.2f} fps" if video.fps else "")
        )
        print(f"  timestamps      {video.timestamp_domain}")
        if video.motion:
            rate = video.motion_hz
            print(
                f"  inertial        {video.motion} samples"
                + (f" = {rate:.0f} Hz across both streams" if rate else "")
            )
        elif config.DEFAULT_STREAMS.motion:
            print("  inertial NONE   the sensor was asked for and gave nothing")
        if video.motion_overrun:
            print(f"  inertial LOST   {video.motion_overrun} (the buffer overran)")
        if video.dropped:
            print(f"  video dropped   {video.dropped} (the disk could not keep up)")
        if video.skipped:
            print(
                f"  video skipped   {video.skipped} mid-stream"
                f" ({video.skipped_unpaired} mispaired,"
                f" {video.skipped_duplicate} repeated)"
            )
        if video.skipped_warmup:
            print(
                f"  startup         {video.skipped_warmup} sets discarded while "
                f"the streams settled (normal)"
            )

    audio = manifest.audio
    if audio is not None:
        print(f"  audio           {audio.seconds:.2f} s, {audio.channels} ch")
        if not audio.samples:
            print("  audio EMPTY     the array recorded nothing")
        if audio.filled:
            print(
                f"  audio filled    {audio.filled} samples "
                f"({audio.filled / audio.rate * 1000:.0f} ms of silence)"
            )
        if audio.overruns:
            print(f"  audio overruns  {audio.overruns}")
        report = audio.timeline or {}
        if report.get("measured_rate"):
            print(
                f"  audio clock     {report['measured_rate']:.1f} Hz measured "
                f"({report['rate_error_ppm']:+.0f} ppm), "
                f"residual {report['residual_rms_ms']:.2f} ms rms / "
                f"{report['residual_max_ms']:.2f} ms max"
            )

    overlap = _overlap(manifest)
    if overlap is not None:
        print(f"  overlap         {overlap:.2f} s of both tracks")

    if manifest.calibration.measured:
        print(f"  offset          {manifest.calibration.offset_s * 1000:+.1f} ms")
    elif video is not None and audio is not None:
        print("  offset          not measured - run tools.calibrate to align")

    for error in manifest.errors:
        print(f"  error           {error}")


def _overlap(manifest: SessionManifest) -> float | None:
    """Seconds during which both devices were recording.

    Args:
        manifest: The finished session.

    Returns:
        The overlap, or None if only one track exists. Computed on the monotonic
        axis, which is the whole reason both tracks carry one.
    """
    video, audio = manifest.video, manifest.audio
    if video is None or audio is None:
        return None
    if video.first_monotonic is None or audio.first_monotonic is None:
        return None
    audio_end = audio.first_monotonic + audio.seconds
    start = max(video.first_monotonic, audio.first_monotonic)
    end = min(video.last_monotonic or video.first_monotonic, audio_end)
    return max(0.0, end - start)


def _status(manifest: SessionManifest) -> int:
    """Turn a finished session into a process exit status.

    Args:
        manifest: The finished session.

    Returns:
        0 if both tracks recorded cleanly, 1 if nothing was recorded, 2 if
        something was lost. Scriptable without parsing the report.
    """
    if manifest.video is None and manifest.audio is None:
        return 1
    # A track that was asked for and recorded nothing is a failure even when
    # nothing raised: an empty WAV beside a full archive is the shape a device
    # left in a bad state produces.
    if manifest.video is not None and not manifest.video.frames:
        return 1
    if manifest.audio is not None and not manifest.audio.samples:
        return 1
    if manifest.video is not None and manifest.video.dropped:
        return 2
    if manifest.audio is not None and manifest.audio.filled:
        return 2
    if manifest.errors:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
