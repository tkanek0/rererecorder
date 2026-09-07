"""Turn a session into a neutral directory that needs none of this code.

    uv run python -m rrr.tools.export var/sessions/2026-09-02_15-28-36
    uv run python -m rrr.tools.export var/sessions/x -o /mnt/dataspace01/rrr
    uv run python -m rrr.tools.export var/sessions/x --stride 5 --end 600

``video.rrdb`` is shaped for recording: one SQLite row per frame, colour left in
the sensor's own YUYV, depth in raw z16. That is the right shape for writing 54
MB/s without dropping anything and the wrong shape for anything else to read.
This writes the same recording as plain files - PNG images, CSV tables, a WAV -
so that a consumer needs a filesystem and nothing else.

The layout is flat, one directory per stream, named for what the stream *is*:

    <session>/
        manifest.json     the index: every stream, what it holds, where it is
        calibration.json  every sensor's intrinsics, every transform between them
        color/            index.csv + data/<ns>.png
        ir_left/          index.csv + data/<ns>.png
        ir_right/         index.csv + data/<ns>.png
        depth/            index.csv + data/<ns>.png   (16-bit, raw z16)
        imu_accel/        index.csv
        imu_gyro/         index.csv
        audio/            audio.wav + clock.csv + clock_fit.json + index.csv
        doa/              index.csv
        events/           index.csv
        derived/          empty, for whatever is computed from this later

Four properties are deliberate, and each of them is a thing that would hurt
later if it were otherwise:

* **Names are roles, not numbers.** ``ir_left``, not ``cam0``. Dropping a camera
  from the rig renumbers every ``camN`` layout and silently changes what an old
  configuration file means; it leaves this one alone.
* **``manifest.json`` is the index.** What a session holds is answered by
  reading one file, not by walking directories and guessing from suffixes.
* **Every stream has the same shape.** ``index.csv`` whose first column is
  ``t_ns``, plus ``data/`` when the samples are images. A stream type nobody
  has thought of yet still reads like the others.
* **Anything variable-length is an array, not a layout.** Eight microphones
  instead of four is a longer list in ``calibration.json``; the directories do
  not move.

Times are integer nanoseconds on ``CLOCK_MONOTONIC``, the axis the recording
was made on. ``manifest.json`` carries the wall-clock anchors, so an absolute
time is recoverable without making it the axis.

**Nothing is converted that cannot be converted back**, with one exception that
is named in the manifest: colour is written as RGB, because a YUYV PNG is not a
thing any tool reads, and the packed original stays in the archive. Depth keeps
its raw z16 and carries its scale. The measured device offset is written down
but **not applied** - applying it would bake one alignment into the files, and
the recording exists to let a consumer decide.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import sys
import time
from typing import Any

import cv2
import numpy as np

from rrr.timeline import (
    AudioTimeline,
    SessionError,
    SessionManifest,
    SessionPaths,
    read_events,
    read_manifest,
)
from rrr.video import ArchiveSource, StreamError

logger = logging.getLogger(__name__)

#: Bumped when the layout changes in a way a reader must know about.
FORMAT_VERSION = 1

#: What this format calls itself, so a directory found later says what it is.
FORMAT_NAME = "rrr-export"

#: Image streams, and where each comes from in a frame set.
IMAGE_STREAMS = ("color", "ir_left", "ir_right", "depth")


def main(argv: list[str] | None = None) -> int:
    """Export one session.

    Args:
        argv: Command-line arguments, or None to read ``sys.argv``.

    Returns:
        Process exit status: 0 on success, 1 if the session could not be read.
    """
    parser = argparse.ArgumentParser(
        description="Write a session out as plain files, for anything to read.",
    )
    parser.add_argument("directory", help="session directory to export")
    parser.add_argument(
        "-o",
        "--out",
        default="export",
        help="where to write the exported session (default: export/)",
    )
    parser.add_argument(
        "--start", type=int, default=0, help="first frame index to write"
    )
    parser.add_argument(
        "--end", type=int, default=None, help="stop before this frame index"
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=1,
        help="write every Nth frame (default: 1, all of them)",
    )
    parser.add_argument(
        "--color",
        choices=("png", "jpeg"),
        default="png",
        help="colour encoding. png is lossless (default); jpeg is not",
    )
    parser.add_argument(
        "--jpeg-quality", type=int, default=95, help="quality when --color=jpeg"
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing export"
    )
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )
    if args.stride < 1:
        parser.error("--stride must be at least 1")

    root, _, session_id = args.directory.rstrip("/").rpartition("/")
    try:
        paths = SessionPaths.resolve(root or ".", session_id)
        manifest = read_manifest(paths)
    except SessionError as error:
        print(f"cannot read the session: {error}", file=sys.stderr)
        return 1

    destination = os.path.join(args.out, manifest.session_id)
    if os.path.exists(destination):
        if not args.force:
            print(
                f"{destination} exists; pass --force to overwrite it",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(destination)

    try:
        written = export(
            paths,
            manifest,
            destination,
            start=args.start,
            end=args.end,
            stride=args.stride,
            color=args.color,
            jpeg_quality=args.jpeg_quality,
        )
    except (SessionError, StreamError, OSError) as error:
        print(f"export failed: {error}", file=sys.stderr)
        return 1

    for note in written["notes"]:
        logger.warning("note: %s", note)
    logger.info("exported %s to %s", manifest.session_id, destination)
    for name, stream in written["streams"].items():
        logger.info("  %-10s %s", name, stream.get("count", "-"))
    return 0


def export(
    paths: SessionPaths,
    manifest: SessionManifest,
    destination: str,
    *,
    start: int = 0,
    end: int | None = None,
    stride: int = 1,
    color: str = "png",
    jpeg_quality: int = 95,
) -> dict[str, Any]:
    """Write a session out in the neutral layout.

    Args:
        paths: Where the session lives.
        manifest: Its manifest, already read.
        destination: Directory to create and fill.
        start: First frame index to write.
        end: Stop before this frame index, or None for all of them.
        stride: Write every Nth frame.
        color: ``"png"`` (lossless) or ``"jpeg"``.
        jpeg_quality: Quality when ``color`` is ``"jpeg"``.

    Returns:
        The manifest that was written.

    Raises:
        StreamError: If the archive cannot be read.
        OSError: If the destination cannot be written.
    """
    os.makedirs(destination, exist_ok=True)
    os.makedirs(os.path.join(destination, "derived"), exist_ok=True)

    streams: dict[str, Any] = {}
    calibration: dict[str, Any] = {}
    notes: list[str] = []

    if manifest.video is not None:
        streams |= _write_frames(
            paths,
            destination,
            start=start,
            end=end,
            stride=stride,
            color=color,
            jpeg_quality=jpeg_quality,
            calibration=calibration,
        )
    if manifest.audio is not None:
        streams |= _write_audio(paths, manifest, destination)
    streams |= _write_doa(paths, destination)
    streams |= _write_events(paths, destination)

    calibration["array"] = _array(manifest)
    calibration["time_offset_s"] = {
        "value": manifest.calibration.offset_s,
        "uncertainty_s": manifest.calibration.uncertainty_s,
        "method": manifest.calibration.method,
        "note": (
            "seconds to add to an audio time to reach the video time of the "
            "same instant. NOT applied to anything in this export"
        ),
    }

    if not manifest.calibration.measured:
        notes.append(
            "the offset between the array and the camera is unmeasured, so "
            "audio and video are only as aligned as the two clocks"
        )
    if manifest.rig.source == "unset":
        notes.append(
            "the mounting between the array and the camera is unset, so a "
            "direction from the array cannot be placed in the camera's frame"
        )

    written = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "session_id": manifest.session_id,
        "clock_reference": "CLOCK_MONOTONIC",
        "time_unit": "ns",
        "started_at": (
            manifest.started_at.as_dict() if manifest.started_at else None
        ),
        "stopped_at": (
            manifest.stopped_at.as_dict() if manifest.stopped_at else None
        ),
        "duration_s": manifest.duration_s,
        "source": {
            "recorder": "rererecorder",
            "session": manifest.session_id,
            "exported_at": time.time(),
            "frames": {"start": start, "end": end, "stride": stride},
            "color_encoding": color,
        },
        "streams": streams,
        "notes": notes,
    }
    _write_json(os.path.join(destination, "manifest.json"), written)
    _write_json(os.path.join(destination, "calibration.json"), calibration)
    return written


# -- frames -------------------------------------------------------------------


def _write_frames(
    paths: SessionPaths,
    destination: str,
    *,
    start: int,
    end: int | None,
    stride: int,
    color: str,
    jpeg_quality: int,
    calibration: dict[str, Any],
) -> dict[str, Any]:
    """Write the image streams and the inertial samples.

    Args:
        paths: Where the session lives.
        destination: Directory being filled.
        start: First frame index to write.
        end: Stop before this index, or None.
        stride: Write every Nth frame.
        color: Colour encoding.
        jpeg_quality: Quality when ``color`` is ``"jpeg"``.
        calibration: Filled in with the sensors this recording has.

    Returns:
        The stream entries for the manifest.
    """
    suffix = ".png" if color == "png" else ".jpg"
    params = (
        [] if color == "png" else [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    )

    with ArchiveSource(paths.video) as archive:
        _describe_sensors(archive, calibration)
        writers = {}
        counts = {name: 0 for name in IMAGE_STREAMS}

        for index, frames in enumerate(archive.frames()):
            if index < start:
                continue
            if end is not None and index >= end:
                break
            if (index - start) % stride:
                continue

            nanoseconds = _ns(frames.capture_monotonic)
            planes = {
                "color": _rgb(frames),
                "ir_left": frames.infrared[0] if frames.infrared else None,
                "ir_right": frames.infrared[1] if frames.infrared else None,
                "depth": frames.depth,
            }
            for name, image in planes.items():
                if image is None:
                    continue
                if name not in writers:
                    writers[name] = _IndexWriter(
                        destination, name, ("t_ns", "file", "frame")
                    )
                    os.makedirs(
                        os.path.join(destination, name, "data"), exist_ok=True
                    )
                # Depth stays 16-bit and stays raw; colour and infrared are 8.
                extension = ".png" if name in ("depth", "ir_left", "ir_right") else suffix
                filename = f"{nanoseconds}{extension}"
                cv2.imwrite(
                    os.path.join(destination, name, "data", filename),
                    image if name != "color" else cv2.cvtColor(image, cv2.COLOR_RGB2BGR),
                    params if extension == ".jpg" else [],
                )
                writers[name].row((nanoseconds, f"data/{filename}", index))
                counts[name] += 1

        for writer in writers.values():
            writer.close()

        streams: dict[str, Any] = {}
        for name in IMAGE_STREAMS:
            if not counts[name]:
                continue
            streams[name] = {
                "kind": "image",
                "index": f"{name}/index.csv",
                "data": f"{name}/data",
                "count": counts[name],
                "encoding": (
                    "png16"
                    if name == "depth"
                    else ("png" if name != "color" else color)
                ),
                "pixel": {
                    "depth": "z16",
                    "color": "rgb8",
                    "ir_left": "y8",
                    "ir_right": "y8",
                }[name],
            }
        streams |= _write_motion(archive, destination)

    return streams


def _rgb(frames: Any) -> np.ndarray | None:
    """Convert a frame set's colour image to RGB, whatever it arrived as.

    Args:
        frames: The set to read.

    Returns:
        ``(height, width, 3)`` uint8 RGB, or None if colour was not recorded.

    This is the one conversion the export performs. The archive keeps the
    sensor's packed YUYV, which nothing outside the SDK reads; the packed
    original is still in the archive, so nothing is lost by writing RGB here.
    """
    if frames.color is None:
        return None
    if frames.color_format != "yuyv":
        return frames.color
    height, width = frames.color.shape
    return cv2.cvtColor(
        frames.color.view(np.uint8).reshape(height, width, 2),
        cv2.COLOR_YUV2RGB_YUY2,
    )


def _write_motion(archive: ArchiveSource, destination: str) -> dict[str, Any]:
    """Write the inertial samples, one stream per sensor.

    Args:
        archive: The open archive.
        destination: Directory being filled.

    Returns:
        The stream entries for the manifest.

    The accelerometer and the gyroscope are kept apart rather than joined into
    one table. They run at different rates - measured 482 and 478 Hz on this
    D455 - so a joined table would need one of them resampled onto the other,
    and this repository does not resample anything it can hand over as measured.
    """
    columns = ("t_ns", "x", "y", "z")
    writers = {
        "imu_accel": _IndexWriter(destination, "imu_accel", columns),
        "imu_gyro": _IndexWriter(destination, "imu_gyro", columns),
    }
    counts = {name: 0 for name in writers}

    for sample in archive.motion_samples():
        name = f"imu_{sample.stream}"
        if name not in writers:
            continue
        writers[name].row((_ns(sample.capture_monotonic), sample.x, sample.y, sample.z))
        counts[name] += 1

    streams: dict[str, Any] = {}
    for name, writer in writers.items():
        writer.close()
        if not counts[name]:
            os.remove(writer.path)
            os.rmdir(os.path.dirname(writer.path))
            continue
        streams[name] = {
            "kind": "samples",
            "index": f"{name}/index.csv",
            "count": counts[name],
            "columns": list(columns),
            "units": "m/s^2" if name.endswith("accel") else "rad/s",
        }
    return streams


def _describe_sensors(archive: ArchiveSource, calibration: dict[str, Any]) -> None:
    """Fill in the calibration for whatever the recording holds.

    Args:
        archive: The open archive.
        calibration: Mapping to fill in.

    Transforms are listed rather than nested, each naming the two frames it
    relates, so that a rig with a different set of sensors produces the same
    shape of file with different rows.
    """
    have = archive.calibration
    sensors: dict[str, Any] = {}
    extrinsics: list[dict[str, Any]] = []

    if have.color is not None:
        sensors["color"] = {"intrinsics": have.color.as_dict()}
    if have.depth is not None:
        sensors["depth"] = {
            "intrinsics": have.depth.as_dict(),
            "scale_m": have.depth_scale,
        }
    for name, entry in zip(("ir_left", "ir_right"), have.infrared):
        if entry is not None:
            sensors[name] = {"intrinsics": entry.as_dict()}

    def relate(target: str, transform: Any) -> None:
        if transform is None:
            return
        extrinsics.append(
            {
                "from": "depth",
                "to": target,
                "rotation": list(transform.rotation),
                "translation": list(transform.translation),
            }
        )

    relate("color", have.depth_to_color)
    relate("ir_left", have.depth_to_infrared[0])
    relate("ir_right", have.depth_to_infrared[1])
    if have.motion is not None:
        if have.motion.accel is not None:
            sensors["imu_accel"] = {"motion_intrinsics": have.motion.accel.as_dict()}
        if have.motion.gyro is not None:
            sensors["imu_gyro"] = {"motion_intrinsics": have.motion.gyro.as_dict()}
        relate("imu_accel", have.motion.depth_to_accel)
        relate("imu_gyro", have.motion.depth_to_gyro)

    calibration["reference_frame"] = "depth"
    calibration["sensors"] = sensors
    calibration["extrinsics"] = extrinsics
    calibration["infrared_baseline_m"] = have.infrared_baseline_m
    calibration["aligned_to_color"] = have.aligned
    device = archive.device
    if device is not None:
        calibration["device"] = device.as_dict()


def _array(manifest: SessionManifest) -> dict[str, Any]:
    """Describe the microphone array, including that it may be unknown.

    Args:
        manifest: The session's manifest.

    Returns:
        The array's entry for ``calibration.json``. ``source`` is ``"unset"``
        when nobody has written the mounting down, and every value is then
        null - a reader must refuse to place a direction rather than assume the
        array sits at the camera's origin.
    """
    rig = manifest.rig
    return {
        "source": rig.source,
        "microphones": (
            [list(point) for point in rig.microphones] if rig.microphones else None
        ),
        "channels": list(rig.channels) if rig.channels else None,
        "to_depth": (
            {
                "from": "array",
                "to": "depth",
                "rotation": list(rig.rotation),
                "translation": list(rig.translation),
            }
            if rig.known
            else None
        ),
        "description": rig.description,
        "note": rig.note,
    }


# -- the array's own files ----------------------------------------------------


def _write_audio(
    paths: SessionPaths, manifest: SessionManifest, destination: str
) -> dict[str, Any]:
    """Copy the WAV and write its measured time mapping beside it.

    Args:
        paths: Where the session lives.
        manifest: The session's manifest.
        destination: Directory being filled.

    Returns:
        The stream entry for the manifest.

    The WAV is copied rather than re-encoded, every channel kept. The mapping
    from a sample position to a time is what makes it placeable at all, and it
    is written twice: the measured points as they were taken, and the line
    fitted through them with its residual, so a consumer can use the fit and
    still see how good it is.
    """
    folder = os.path.join(destination, "audio")
    os.makedirs(folder, exist_ok=True)
    shutil.copyfile(paths.audio, os.path.join(folder, "audio.wav"))

    track = manifest.audio
    entry: dict[str, Any] = {
        "kind": "audio",
        "file": "audio/audio.wav",
        "rate": track.rate if track else None,
        "channels": track.channels if track else None,
        "samples": track.samples if track else None,
        "clock": "audio/clock.csv",
    }

    points = 0
    try:
        timeline = AudioTimeline.read(paths.audio_clock, track.rate if track else 0)
    except (OSError, ValueError) as error:
        # A WAV with no time mapping can only be read as frames / nominal rate,
        # which is the assumption the sidecar exists to avoid. Exported anyway,
        # with the absence visible, rather than refusing the whole session.
        logger.warning("no usable audio clock: %s", error)
        timeline = None

    if timeline is not None:
        with open(
            os.path.join(folder, "clock.csv"), "w", encoding="utf-8", newline=""
        ) as handle:
            rows = csv.writer(handle)
            rows.writerow(("sample", "t_ns", "filled"))
            for point in timeline.points:
                rows.writerow((point.sample, _ns(point.monotonic), point.filled))
                points += 1
        _write_json(
            os.path.join(folder, "clock_fit.json"), timeline.report().as_dict()
        )
        entry["fit"] = "audio/clock_fit.json"

    entry["clock_points"] = points
    return {"audio": entry}


def _write_doa(paths: SessionPaths, destination: str) -> dict[str, Any]:
    """Write the array's own direction estimate as a table.

    Args:
        paths: Where the session lives.
        destination: Directory being filled.

    Returns:
        The stream entry, or nothing if the session has no direction track.

    Kept because it is a baseline worth comparing against, not because it is
    the answer: it comes out of the array's firmware, whose band, threshold and
    method are not documented and cannot be changed.
    """
    if not os.path.exists(paths.doa):
        return {}
    count = 0
    with _IndexWriter(
        destination, "doa", ("t_ns", "angle_deg", "voice")
    ) as writer, open(paths.doa, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            reading = json.loads(line)
            writer.row((_ns(reading["t"]), reading["angle"], int(reading["voice"])))
            count += 1
    if not count:
        return {}
    return {
        "doa": {
            "kind": "samples",
            "index": "doa/index.csv",
            "count": count,
            "columns": ["t_ns", "angle_deg", "voice"],
            "source": "the array's own firmware, not computed here",
        }
    }


def _write_events(paths: SessionPaths, destination: str) -> dict[str, Any]:
    """Write the marks somebody made while recording.

    Args:
        paths: Where the session lives.
        destination: Directory being filled.

    Returns:
        The stream entry, or nothing if nobody marked anything.
    """
    try:
        events = read_events(paths.events)
    except ValueError as error:
        logger.warning("skipping unreadable marks: %s", error)
        return {}
    if not events:
        return {}

    with _IndexWriter(
        destination, "events", ("t_ns", "label", "data")
    ) as writer:
        for event in events:
            writer.row(
                (
                    _ns(event.monotonic),
                    event.label,
                    json.dumps(event.data, sort_keys=True),
                )
            )
    return {
        "events": {
            "kind": "marks",
            "index": "events/index.csv",
            "count": len(events),
            "columns": ["t_ns", "label", "data"],
            "note": (
                "made by hand, so late by a person's reaction time. Says what a "
                "stretch of the recording was; not an instant to align against"
            ),
        }
    }


# -- plumbing -----------------------------------------------------------------


class _IndexWriter:
    """A stream's ``index.csv``, created with its directory."""

    def __init__(self, destination: str, name: str, columns: tuple[str, ...]) -> None:
        folder = os.path.join(destination, name)
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "index.csv")
        self._handle = open(self.path, "w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(columns)

    def __enter__(self) -> _IndexWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def row(self, values: tuple[Any, ...]) -> None:
        """Write one row."""
        self._writer.writerow(values)

    def close(self) -> None:
        """Close the file."""
        if not self._handle.closed:
            self._handle.close()


def _ns(seconds: float) -> int:
    """Convert a time in seconds to integer nanoseconds.

    Args:
        seconds: A ``CLOCK_MONOTONIC`` reading.

    Returns:
        The same instant in nanoseconds. Integer, because a float second near
        10^6 has about 200 ns of resolution left, and a filename has to be an
        exact key.
    """
    return int(round(seconds * 1e9))


def _write_json(path: str, payload: Any) -> None:
    """Write a JSON file with a trailing newline."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False, default=float)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
