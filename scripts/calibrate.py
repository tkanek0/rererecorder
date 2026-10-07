"""Measure the offset between the camera and the array, from a handclap.

    uv run python scripts/calibrate.py data/sessions/2026-09-02_15-28-36
    uv run python scripts/calibrate.py data/sessions/... --apply

Without ``--apply`` nothing is written and ``calibration.offset_s`` stays
null. The measurement is ``rrr.offset``; this reports it and, when asked,
stores it. The frame rate bounds the accuracy - see docs/decisions.md 14.
"""

from __future__ import annotations

import argparse
import sys

from rrr.offset import OffsetError, measure_offset
from rrr.timeline import SessionError, SessionPaths, read_manifest, write_manifest


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

    try:
        paths = SessionPaths.of_directory(args.directory)
        manifest = read_manifest(paths)
    except SessionError as error:
        print(f"cannot read the session: {error}", file=sys.stderr)
        return 1

    try:
        measurement = measure_offset(paths, manifest, args.stream)
    except OffsetError as error:
        print(error, file=sys.stderr)
        return 1

    print(f"session {manifest.session_id}")
    print(f"  impulses        {len(measurement.claps)} found in the audio")
    for n, clap in enumerate(measurement.claps, start=1):
        if clap.offset is None or clap.sharpness is None:
            print(
                f"  clap {n}          audio {clap.audio_at:.3f} s - no movement found"
            )
            continue
        # Capped: a still scene has a zero median and an unbounded ratio.
        print(
            f"  clap {n}          audio {clap.audio_at:.3f} s, "
            f"video {clap.video_at:.3f} s "
            f"(frame {clap.frame}) -> {clap.offset * 1000:+.1f} ms"
            f"   [movement {min(clap.sharpness, 999.0):.0f}x the median]"
        )

    offset, uncertainty = measurement.offset_s, measurement.uncertainty_s
    if offset is None or uncertainty is None:
        print(
            "impulses were found but no matching movement was. Was the clap in shot?",
            file=sys.stderr,
        )
        return 1

    count = len(measurement.offsets)
    print()
    print(f"  offset          {offset * 1000:+.1f} ms")
    print(
        f"  uncertainty     +/- {uncertainty * 1000:.1f} ms "
        f"(half a frame over sqrt({count}) claps)"
    )
    if measurement.spread_s is not None:
        spread_ms = measurement.spread_s * 1000
        print(f"  spread          {spread_ms:.1f} ms across the claps")
        if measurement.disagrees:
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

    manifest.calibration = measurement.to_calibration()
    write_manifest(paths, manifest)
    print(f"\n  Written to {paths.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
