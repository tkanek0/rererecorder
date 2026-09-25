"""Record for a fixed duration with a chosen configuration, then say whether it held.

A reusable version of the ad hoc checks behind decisions 21, 22 and 23: does a
given combination of streams and codecs hold close to the requested fps with
nothing dropped, for as long as it is asked to run? This is the same question
every one of those investigations answered by hand - this script just makes it
repeatable on a different machine, after a driver update, or after a future
change to `FrameHub`/`VideoWriter`.

Every flag `rrr.tools.record` accepts works here too - it is forwarded
verbatim, so this never drifts out of sync with what the CLI actually
supports:

    # the combination decision 23 settled on for this Windows machine
    uv run python tests/perf/soak_record.py --session soak-color-raw \\
        --seconds 600 --no-depth --no-infrared --color-codec raw

    # the full six-image set, compressed - decision 22's worst case
    uv run python tests/perf/soak_record.py --session soak-full-compressed \\
        --seconds 60

Needs a live device. Not part of `pytest`, which runs with none attached -
run this by hand, or wire it into a separate device-equipped CI runner if one
ever exists.
"""

from __future__ import annotations

import argparse
import sys

from rrr.recorder import config
from rrr.timeline import SessionError, SessionPaths, read_manifest
from rrr.tools import record
from rrr.video import ArchiveSource, StreamError


def main(argv: list[str] | None = None) -> int:
    """Record, then verify the session that resulted.

    Args:
        argv: Command line arguments, or None to read them from the process.
            Anything this script does not define itself is forwarded to
            :func:`rrr.tools.record.main` unchanged.

    Returns:
        0 if the recording held within the given tolerances, 1 otherwise -
        scriptable the same way `rrr.tools.record` itself is.
    """
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="every other flag is forwarded to `rrr.tools.record` unchanged "
        "- see `python -m rrr.tools.record --help`.",
    )
    parser.add_argument(
        "--session",
        required=True,
        help="session name, so this can find it again afterwards to verify it",
    )
    parser.add_argument("--root", default=config.SESSIONS_ROOT)
    parser.add_argument(
        "--expect-fps",
        type=float,
        default=30.0,
        help="the fps this combination should hold (default: 30, the D455's own rate)",
    )
    parser.add_argument(
        "--fps-tolerance",
        type=float,
        default=0.5,
        help="how far below --expect-fps still counts as holding",
    )
    parser.add_argument(
        "--max-drop-rate",
        type=float,
        default=0.0,
        help="fraction of frames allowed to be dropped before this fails (0 = none)",
    )
    known, forwarded = parser.parse_known_args(argv)

    status = record.main(
        ["--session", known.session, "--root", known.root, *forwarded]
    )
    if status == 1:
        print("recording did not produce anything to verify", file=sys.stderr)
        return 1

    try:
        paths = SessionPaths.resolve(known.root, known.session)
        manifest = read_manifest(paths)
    except SessionError as error:
        print(f"could not read back the session: {error}", file=sys.stderr)
        return 1

    _describe_archive(paths.video)
    return _verify(
        manifest,
        expect_fps=known.expect_fps,
        fps_tolerance=known.fps_tolerance,
        max_drop_rate=known.max_drop_rate,
    )


def _describe_archive(path: str) -> None:
    """Print what was actually captured, so a pass/fail is legible on its own."""
    try:
        with ArchiveSource(path) as archive:
            print(f"streams: {archive.meta.get('config')}")
            print(f"codecs:  {archive.meta.get('codecs')}")
    except StreamError as error:
        print(f"(could not describe the archive: {error})", file=sys.stderr)


def _verify(
    manifest, *, expect_fps: float, fps_tolerance: float, max_drop_rate: float
) -> int:
    """Judge a finished session against the tolerances asked for.

    Args:
        manifest: The session's manifest, as written.
        expect_fps: The rate this combination should hold.
        fps_tolerance: How far below that still counts as holding.
        max_drop_rate: Fraction of attempted frames allowed to be dropped.

    Returns:
        0 if every check passed, 1 otherwise. Reasons are printed either way.
    """
    video = manifest.video
    if video is None or video.frames == 0:
        print("FAIL: no video was recorded at all")
        return 1

    attempted = video.frames + video.dropped
    drop_rate = video.dropped / attempted if attempted else 0.0
    reasons = []
    if drop_rate > max_drop_rate:
        reasons.append(
            f"drop rate {drop_rate:.2%} exceeds the {max_drop_rate:.2%} allowed "
            f"({video.dropped} of {attempted} frames)"
        )
    if video.fps is not None and video.fps < expect_fps - fps_tolerance:
        reasons.append(
            f"{video.fps:.2f} fps is more than {fps_tolerance:.2f} below the "
            f"expected {expect_fps:.2f}"
        )

    print(
        f"{video.frames} frames, {video.fps:.2f} fps, "
        f"{drop_rate:.2%} dropped, timestamps {video.timestamp_domain}"
        if video.fps is not None
        else f"{video.frames} frames, {drop_rate:.2%} dropped"
    )
    if not reasons:
        print("PASS")
        return 0
    print("FAIL:")
    for reason in reasons:
        print(f"  - {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
