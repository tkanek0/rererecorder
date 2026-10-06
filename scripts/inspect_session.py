"""Check what a recorded session actually says about its own timing.

    uv run python scripts/inspect_session.py data/sessions/2026-09-01_17-30-00

The cross-checks are ``rrr.inspection``; this prints them for a person, or as
JSON. Not named ``inspect.py``, which would shadow the standard library module
of that name for everything imported while it runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from rrr.inspection import Inspection, inspect_session
from rrr.timeline import (
    SessionError,
    SessionManifest,
    SessionPaths,
    drift_ppm,
    read_manifest,
)


def main(argv: list[str] | None = None) -> int:
    """Inspect one session.

    Args:
        argv: Command line arguments, or None to read them from the process.

    Returns:
        0 if every cross-check agreed, 1 if any disagreed.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="the session directory to inspect")
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output instead"
    )
    args = parser.parse_args(argv)

    try:
        paths = SessionPaths.of_directory(args.directory)
        manifest = read_manifest(paths)
    except SessionError as error:
        print(f"cannot read the session: {error}", file=sys.stderr)
        return 1
    found = inspect_session(paths, manifest)

    if args.json:
        json.dump(found.as_dict(), sys.stdout, indent=2, default=float)
        print()
    else:
        print_inspection(manifest, found)

    return 0 if found.agreed else 1


def print_inspection(manifest: SessionManifest, found: Inspection) -> None:
    """Write the findings for a person to read."""
    audio, video, imu = found.audio, found.video, found.imu
    overlap, doa, marks = found.overlap, found.doa, found.marks
    print(f"session {manifest.session_id}")
    if manifest.started_at is not None:
        stamp = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(manifest.started_at.realtime)
        )
        print(f"  started         {stamp}")
    if manifest.duration_s is not None:
        print(f"  duration        {manifest.duration_s:.2f} s")

    drift = drift_ppm(manifest.clock_samples)
    if drift is not None:
        print(
            f"  clock offset    {manifest.clock_samples[0].offset:.6f} s, "
            f"drifting {drift:+.2f} ppm over the session"
        )

    if video is not None:
        print(
            f"  video           {video['frames']} frames over {video['span_s']:.2f} s"
            + (f" = {video['fps']:.2f} fps" if video["fps"] else "")
        )
        gap = video["gap_ms"]
        print(
            f"  frame interval  {gap['median']:.1f} ms median, "
            f"{gap['min']:.1f} min, {gap['max']:.1f} max"
        )
        for stream, count in video["missing"].items():
            print(
                f"  {stream:<6}          {count['delivered']} of {count['span']} "
                f"numbered frames, MISSING {count['missing']}"
            )

    if imu is not None:
        rates = ", ".join(
            f"{stream} {rate:.0f} Hz" for stream, rate in sorted(imu["rates"].items())
        )
        print(f"  inertial        {imu['samples']} samples ({rates})")
        if "accel_magnitude" in imu:
            print(
                f"  gravity         {imu['accel_magnitude']:.2f} m/s^2 median "
                f"magnitude (9.81 if still)"
            )

    if audio is not None and "report" in audio:
        report = audio["report"]
        print(
            f"  audio           {audio['frames']} samples, "
            f"{audio['channels']} ch at {audio['rate']} Hz"
        )
        print(
            f"  length          {audio['seconds_by_header']:.3f} s by header, "
            f"{audio['seconds_by_clock']:.3f} s by clock points "
            f"({audio['disagreement_ms']:.1f} ms apart)"
        )
        if report["measured_rate"]:
            print(
                f"  audio clock     {report['measured_rate']:.2f} Hz fitted "
                f"({report['rate_error_ppm']:+.0f} ppm) from {report['points']} points"
            )
            print(
                f"  residual        {report['residual_rms_ms']:.3f} ms rms, "
                f"{report['residual_max_ms']:.3f} ms max"
            )

    if doa is not None:
        print(
            f"  direction       {doa['readings']} readings"
            + (f" at {doa['hz']:.1f} Hz" if doa.get("hz") else "")
        )

    if overlap is not None:
        print(f"  overlap         {overlap['seconds']:.2f} s of both tracks")
        print(
            f"  audio started   {overlap['audio_lead_s'] * 1000:+.0f} ms "
            f"before the video"
        )
        if overlap["calibrated"]:
            print(f"  offset          {overlap['offset_s'] * 1000:+.1f} ms (measured)")
        else:
            print(
                "  offset          NOT MEASURED - the tracks share a clock but "
                "their absolute alignment is unknown"
            )

    if marks:
        # A few distinct labels, so a mark per run does not bury the checks.
        distinct = list(dict.fromkeys(marks["labels"]))
        shown = ", ".join(distinct[:5])
        if len(distinct) > 5:
            shown += f", and {len(distinct) - 5} more"
        print(f"  marks           {marks['marks']} ({shown})")

    for note in found.notes:
        print(f"  note            {note}")
    for problem in found.problems:
        print(f"  PROBLEM         {problem}")
    if found.agreed:
        print("  every cross-check agreed")


if __name__ == "__main__":
    raise SystemExit(main())
