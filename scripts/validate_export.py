"""Check a neutral export without importing or opening the recording archive.

    uv run python scripts/validate_export.py data/sessions/<session>/export

Together with ``scripts/export.py``, which writes the layout, this is where the
export format is defined; docs/export-format.md describes it. Deliberately
imports nothing from ``rrr``: it checks what an outside consumer sees, and can
be copied to one as it is.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import wave
from itertools import pairwise
from typing import Any

import cv2
import numpy as np

#: Bumped when the layout changes in a way a reader must know about.
FORMAT_VERSION = 2

#: What this format calls itself, so a directory found later says what it is.
FORMAT_NAME = "rrr-export"


def main(argv: list[str] | None = None) -> int:
    """Validate one exported session and return a shell-friendly status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="exported session directory")
    args = parser.parse_args(argv)

    problems = validate_export(args.directory)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    print("OK")
    return 0


def validate_export(directory: str) -> list[str]:
    """Return contradictions found in a completed neutral export.

    Filesystem-only, so it checks what an external consumer sees.

    Args:
        directory: The exported session directory.

    Returns:
        One message per problem; empty if the export is consistent.
    """
    problems: list[str] = []
    manifest_path = os.path.join(directory, "manifest.json")
    calibration_path = os.path.join(directory, "calibration.json")
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        return [f"manifest.json is not readable: {error}"]
    if not isinstance(manifest, dict):
        return ["manifest.json does not contain an object"]
    try:
        with open(calibration_path, encoding="utf-8") as handle:
            calibration = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        problems.append(f"calibration.json is not readable: {error}")
        calibration = {}
    if not isinstance(calibration, dict):
        problems.append("calibration.json does not contain an object")
        calibration = {}

    if manifest.get("format") != FORMAT_NAME:
        problems.append(f"unexpected format {manifest.get('format')!r}")
    if manifest.get("format_version") != FORMAT_VERSION:
        problems.append(f"unexpected format_version {manifest.get('format_version')!r}")

    streams = manifest.get("streams")
    if not isinstance(streams, dict):
        return problems + ["manifest streams is not an object"]
    for name, raw_entry in streams.items():
        if not isinstance(raw_entry, dict):
            problems.append(f"stream {name} is not an object")
            continue
        entry: dict[str, Any] = raw_entry
        for key in ("index", "data", "file", "clock", "fit"):
            relative = entry.get(key)
            if relative is not None and not os.path.exists(
                os.path.join(directory, str(relative))
            ):
                problems.append(f"stream {name} names missing {key} {relative}")

        kind = entry.get("kind")
        if kind in ("image", "samples", "marks"):
            index = entry.get("index")
            if not isinstance(index, str) or not index.endswith(".csv"):
                problems.append(f"stream {name} has no CSV index")
                continue
            rows = _read_csv(os.path.join(directory, index), problems, name)
            if rows is None:
                continue
            if len(rows) != entry.get("count"):
                problems.append(
                    f"stream {name} says {entry.get('count')} rows and has {len(rows)}"
                )
            _validate_times(name, rows, problems)
            if kind == "image":
                _validate_images(directory, name, entry, rows, calibration, problems)
        elif kind == "metadata":
            index = entry.get("index")
            if isinstance(index, str):
                count = _jsonl_count(os.path.join(directory, index), problems, name)
                if count is not None and count != entry.get("count"):
                    problems.append(
                        f"stream {name} says {entry.get('count')} rows and has {count}"
                    )
        elif kind == "audio":
            _validate_audio(directory, name, entry, problems)
    return problems


def _read_csv(
    path: str, problems: list[str], stream: str
) -> list[dict[str, str]] | None:
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error) as error:
        problems.append(f"stream {stream} index is not readable: {error}")
        return None


def _validate_times(name: str, rows: list[dict[str, str]], problems: list[str]) -> None:
    try:
        times = [int(row["t_ns"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        problems.append(f"stream {name} has an invalid t_ns column")
        return
    if any(after < before for before, after in pairwise(times)):
        problems.append(f"stream {name} timestamps go backwards")


def _validate_images(
    directory: str,
    name: str,
    entry: dict[str, Any],
    rows: list[dict[str, str]],
    calibration: dict[str, Any],
    problems: list[str],
) -> None:
    files = [row.get("file", "") for row in rows]
    index = str(entry.get("index", ""))
    stream_directory = os.path.dirname(os.path.join(directory, index))
    try:
        sample_ids = [int(row["sample_id"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        problems.append(f"stream {name} has an invalid sample_id column")
    else:
        if sample_ids != list(range(len(rows))):
            problems.append(f"stream {name} sample ids are not contiguous from zero")
    if len(files) != len(set(files)):
        problems.append(f"stream {name} reuses an image filename")
    missing = [
        relative
        for relative in files
        if not os.path.isfile(os.path.join(stream_directory, relative))
    ]
    if missing:
        problems.append(f"stream {name} has {len(missing)} missing image files")
        return
    if not files:
        return
    image = cv2.imread(os.path.join(stream_directory, files[0]), cv2.IMREAD_UNCHANGED)
    if image is None:
        problems.append(f"stream {name} first image cannot be decoded")
        return
    expected_dtype = np.uint16 if entry.get("pixel") == "z16" else np.uint8
    if image.dtype != expected_dtype:
        problems.append(
            f"stream {name} image dtype is {image.dtype}, expected {expected_dtype}"
        )
    sensor = calibration.get("sensors", {}).get(name, {})
    intrinsics = sensor.get("intrinsics", {}) if isinstance(sensor, dict) else {}
    expected_shape = (intrinsics.get("height"), intrinsics.get("width"))
    if (
        all(isinstance(value, int) for value in expected_shape)
        and image.shape[:2] != expected_shape
    ):
        problems.append(
            f"stream {name} image shape is {image.shape[:2]}, expected {expected_shape}"
        )


def _jsonl_count(path: str, problems: list[str], stream: str) -> int | None:
    count = 0
    try:
        with open(path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as error:
                    problems.append(
                        f"stream {stream} line {line_number} is not JSON: {error}"
                    )
                count += 1
    except OSError as error:
        problems.append(f"stream {stream} index is not readable: {error}")
        return None
    return count


def _validate_audio(
    directory: str, name: str, entry: dict[str, Any], problems: list[str]
) -> None:
    relative = entry.get("file")
    if not isinstance(relative, str):
        problems.append(f"stream {name} has no WAV file")
        return
    try:
        with wave.open(os.path.join(directory, relative), "rb") as handle:
            actual = (
                handle.getframerate(),
                handle.getnchannels(),
                handle.getnframes(),
            )
    except (OSError, wave.Error) as error:
        problems.append(f"stream {name} WAV is not readable: {error}")
        return
    expected = (entry.get("rate"), entry.get("channels"), entry.get("samples"))
    if actual != expected:
        problems.append(f"stream {name} WAV says {actual}, manifest says {expected}")
    clock = entry.get("clock")
    if clock is None:
        # Exported without a clock, which the manifest's notes say; then it
        # cannot claim clock points either.
        if entry.get("clock_points"):
            problems.append(
                f"stream {name} has no audio clock but says "
                f"{entry.get('clock_points')} clock points"
            )
        return
    if not isinstance(clock, str):
        problems.append(f"stream {name} has an invalid audio clock entry")
        return
    rows = _read_csv(os.path.join(directory, clock), problems, f"{name} clock")
    if rows is None:
        return
    try:
        samples = [int(row["sample"]) for row in rows]
        times = [int(row["t_ns"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        problems.append(f"stream {name} audio clock has invalid columns")
        return
    if len(rows) != entry.get("clock_points"):
        problems.append(
            f"stream {name} says {entry.get('clock_points')} clock points "
            f"and has {len(rows)}"
        )
    if any(after <= before for before, after in pairwise(samples)):
        problems.append(f"stream {name} audio clock samples are not increasing")
    if any(after < before for before, after in pairwise(times)):
        problems.append(f"stream {name} audio clock timestamps go backwards")
    if samples and (samples[0] < 0 or samples[-1] > actual[2]):
        problems.append(f"stream {name} audio clock falls outside the WAV")


if __name__ == "__main__":
    raise SystemExit(main())
