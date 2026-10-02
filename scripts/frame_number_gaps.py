"""Reproduce decision 21's frame_number gap check.

Watches the SDK's own per-stream `frame_number` for skips (real,
device-reported loss) through `LiveSource`, as the recorder uses it. See
docs/decisions.md 21. Run with AE both on and off if colour/depth timing is in
question:

    uv run python scripts/frame_number_gaps.py --seconds 60
    uv run python scripts/frame_number_gaps.py --seconds 60 --auto-exposure off

Needs a live device. Not part of `pytest`.
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from itertools import pairwise

from realsense_adapter import LiveSource, StreamError
from rrr.recorder import config


def _apply_auto_exposure(source: LiveSource, mode: str) -> None:
    """Set the colour sensor's auto-exposure, if asked to.

    Args:
        source: An already-open source.
        mode: ``"on"``, ``"off"`` or ``"leave"`` (the default - untouched).
    """
    if mode == "leave":
        return
    source.set_option(
        "RGB Camera/enable_auto_exposure", 1.0 if mode == "on" else 0.0
    )


def _collect(source: LiveSource, seconds: float) -> dict[str, list[int]]:
    """Run the source for a fixed duration, recording every frame_number seen.

    Args:
        source: An already-open source.
        seconds: How long to run.

    Returns:
        Stream name to the sequence of frame_number values observed, in order.
    """
    numbers: dict[str, list[int]] = collections.defaultdict(list)
    deadline = time.monotonic() + seconds
    for _ in source.frames():
        for stream, number in source.frame_numbers.items():
            numbers[stream].append(number)
        if time.monotonic() >= deadline:
            break
    return numbers


def _report(name: str, sequence: list[int]) -> bool:
    """Print one stream's gap analysis.

    Returns:
        Whether the stream showed no gap, i.e. no jump other than 0
        (a duplicate) or 1.
    """
    if len(sequence) < 2:
        print(f"{name:>8}: only {len(sequence)} frame(s), nothing to check")
        return True
    gaps = [
        (a, b)
        for a, b in pairwise(sequence)
        if b - a not in (0, 1)
    ]
    print(
        f"{name:>8}: {len(sequence)} sets, frame_number {sequence[0]} to "
        f"{sequence[-1]}, {len(gaps)} gap(s)"
        + (f" - first at {gaps[0][0]} -> {gaps[0][1]}" if gaps else "")
    )
    return not gaps


def main(argv: list[str] | None = None) -> int:
    """Record nothing, just watch the device's own frame numbering for gaps.

    Args:
        argv: Command line arguments, or None to read them from the process.

    Returns:
        0 if no stream showed a gap, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument(
        "--auto-exposure",
        choices=("leave", "on", "off"),
        default="leave",
        help="set the colour sensor's AE before watching (default: leave it)",
    )
    args = parser.parse_args(argv)

    try:
        with LiveSource(config.DEFAULT_STREAMS, serial=config.SERIAL) as source:
            _apply_auto_exposure(source, args.auto_exposure)
            numbers = _collect(source, args.seconds)
    except StreamError as error:
        print(f"could not read the device: {error}", file=sys.stderr)
        return 1

    if not numbers:
        print("no frames were delivered at all", file=sys.stderr)
        return 1

    ok = True
    for name in sorted(numbers):
        ok = _report(name, numbers[name]) and ok
    print("PASS: no stream's frame_number skipped" if ok else "FAIL: see gap(s) above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
