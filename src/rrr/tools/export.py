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
        color/            index.csv + data/<sample>.png
        ir_left/          index.csv + data/<sample>.png
        ir_right/         index.csv + data/<sample>.png
        depth/            index.csv + data/<sample>.png   (16-bit, raw z16)
        frame_metadata/   index.jsonl
        imu_accel/        index.csv
        imu_gyro/         index.csv
        audio/            audio.wav + clock.csv + clock_fit.json
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
* **Every sampled stream has an explicit index.** Images use an ``index.csv``
  with stable sample and frame-set ids, both host and sensor timestamps, and a
  relative path into ``data/``. Variable firmware metadata uses JSONL because
  its fields differ by device and stream.
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
import math
import os
import shutil
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Self

import cv2
import numpy as np

from rrr.timeline import (
    AudioClockPoint,
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
FORMAT_VERSION = 2

#: What this format calls itself, so a directory found later says what it is.
FORMAT_NAME = "rrr-export"

#: Image streams, and where each comes from in a frame set.
IMAGE_STREAMS = ("color", "ir_left", "ir_right", "depth")


class ExportValidationError(OSError):
    """Raised when a completed export contradicts its own manifest."""


@dataclass(frozen=True)
class _TimeRange:
    """Half-open interval on the recording's monotonic clock."""

    start: float | None = None
    end: float | None = None

    @property
    def selected(self) -> bool:
        """Return whether either side of the recording was trimmed."""
        return self.start is not None or self.end is not None

    def contains(self, seconds: float) -> bool:
        """Return whether a timestamp belongs to this interval."""
        return (self.start is None or seconds >= self.start) and (
            self.end is None or seconds < self.end
        )

    def as_dict(self) -> dict[str, int | None]:
        """Return the interval in the export's integer time unit."""
        return {
            "start_t_ns": _ns(self.start) if self.start is not None else None,
            "end_t_ns": _ns(self.end) if self.end is not None else None,
        }


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
    if args.start < 0:
        parser.error("--start must not be negative")
    if args.end is not None and args.end <= args.start:
        parser.error("--end must be greater than --start")

    root, _, session_id = args.directory.rstrip("/").rpartition("/")
    try:
        paths = SessionPaths.resolve(root or ".", session_id)
        manifest = read_manifest(paths)
    except SessionError as error:
        print(f"cannot read the session: {error}", file=sys.stderr)
        return 1

    destination = os.path.join(args.out, manifest.session_id)
    if os.path.exists(destination) and not args.force:
        print(
            f"{destination} exists; pass --force to overwrite it",
            file=sys.stderr,
        )
        return 1

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
            overwrite=args.force,
        )
    except (SessionError, StreamError, OSError, ValueError) as error:
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
    overwrite: bool = False,
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
        overwrite: Replace an existing destination after the new export has
            been written and validated successfully.

    Returns:
        The manifest that was written.

    Raises:
        StreamError: If the archive cannot be read.
        OSError: If the destination cannot be written.
    """
    if start < 0:
        raise ValueError("start must not be negative")
    if end is not None and end <= start:
        raise ValueError("end must be greater than start")
    if stride < 1:
        raise ValueError("stride must be at least 1")
    if os.path.exists(destination) and not overwrite:
        raise FileExistsError(destination)

    parent = os.path.dirname(os.path.abspath(destination))
    os.makedirs(parent, exist_ok=True)
    temporary = tempfile.mkdtemp(
        prefix=f".{os.path.basename(destination)}.exporting-", dir=parent
    )
    try:
        written = _export_into(
            paths,
            manifest,
            temporary,
            start=start,
            end=end,
            stride=stride,
            color=color,
            jpeg_quality=jpeg_quality,
        )
        problems = validate_export(temporary)
        if problems:
            raise ExportValidationError("; ".join(problems))
        _publish(temporary, destination, overwrite=overwrite)
    except Exception:
        if os.path.exists(temporary):
            shutil.rmtree(temporary)
        raise
    return written


def _export_into(
    paths: SessionPaths,
    manifest: SessionManifest,
    destination: str,
    *,
    start: int,
    end: int | None,
    stride: int,
    color: str,
    jpeg_quality: int,
) -> dict[str, Any]:
    """Write an export into a new temporary directory."""
    os.makedirs(os.path.join(destination, "derived"), exist_ok=True)

    streams: dict[str, Any] = {}
    calibration: dict[str, Any] = {}
    notes: list[str] = []

    if manifest.video is None and (start != 0 or end is not None):
        raise ValueError("--start/--end need a video track to define their time range")

    time_range = _TimeRange()
    if manifest.video is not None:
        frame_streams, time_range = _write_frames(
            paths,
            destination,
            start=start,
            end=end,
            stride=stride,
            color=color,
            jpeg_quality=jpeg_quality,
            calibration=calibration,
        )
        streams |= frame_streams
    if manifest.audio is not None:
        streams |= _write_audio(paths, destination, time_range)
    streams |= _write_doa(paths, destination, time_range)
    streams |= _write_events(paths, destination, time_range)

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
        "started_at": (manifest.started_at.as_dict() if manifest.started_at else None),
        "stopped_at": (manifest.stopped_at.as_dict() if manifest.stopped_at else None),
        "duration_s": manifest.duration_s,
        "source": {
            "recorder": "rererecorder",
            "session": manifest.session_id,
            "exported_at": time.time(),
            "frames": {"start": start, "end": end, "stride": stride},
            "time_range": time_range.as_dict(),
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
) -> tuple[dict[str, Any], _TimeRange]:
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
        The stream entries for the manifest and the half-open time interval
        selected by ``start`` and ``end``.
    """
    suffix = ".png" if color == "png" else ".jpg"
    params = (
        [] if color == "png" else [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
    )

    with ArchiveSource(paths.video) as archive:
        _describe_sensors(archive, calibration)
        times = archive.frame_times()
        if start and start >= len(times):
            raise ValueError(
                f"start frame {start} is outside an archive of {len(times)} frames"
            )
        time_range = _TimeRange(
            start=times[start][1] if start and start < len(times) else None,
            end=times[end][1] if end is not None and end < len(times) else None,
        )
        writers = {}
        counts = {name: 0 for name in IMAGE_STREAMS}
        metadata_path = os.path.join(destination, "frame_metadata", "index.jsonl")
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
        metadata_count = 0

        with open(metadata_path, "w", encoding="utf-8") as metadata_out:
            for position, frames in enumerate(archive.frames()):
                if position < start:
                    continue
                if end is not None and position >= end:
                    break
                if (position - start) % stride:
                    continue

                nanoseconds = _ns(frames.received_monotonic)
                metadata_out.write(
                    json.dumps(
                        {
                            "group_id": frames.index,
                            "t_ns": nanoseconds,
                            "timestamp_domain": frames.timestamp_domain,
                            "streams": frames.metadata or {},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                metadata_count += 1

                planes = {
                    "color": (_rgb(frames), frames.color_timestamp_ms),
                    "ir_left": (
                        frames.infrared[0] if frames.infrared else None,
                        frames.depth_timestamp_ms,
                    ),
                    "ir_right": (
                        frames.infrared[1] if frames.infrared else None,
                        frames.depth_timestamp_ms,
                    ),
                    "depth": (frames.depth, frames.depth_timestamp_ms),
                }
                for name, (image, sensor_timestamp_ms) in planes.items():
                    if image is None:
                        continue
                    if name not in writers:
                        writers[name] = _IndexWriter(
                            destination,
                            name,
                            (
                                "sample_id",
                                "group_id",
                                "t_ns",
                                "sensor_timestamp_ms",
                                "timestamp_domain",
                                "file",
                            ),
                        )
                        os.makedirs(
                            os.path.join(destination, name, "data"), exist_ok=True
                        )
                    # Depth stays 16-bit and stays raw; colour and infrared are 8.
                    extension = (
                        ".png" if name in ("depth", "ir_left", "ir_right") else suffix
                    )
                    sample_id = counts[name]
                    filename = f"{sample_id:09d}{extension}"
                    wrote = cv2.imwrite(
                        os.path.join(destination, name, "data", filename),
                        (
                            image
                            if name != "color"
                            else cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                        ),
                        params if extension == ".jpg" else [],
                    )
                    if not wrote:
                        raise OSError(f"could not write {name} frame {frames.index}")
                    writers[name].row(
                        (
                            sample_id,
                            frames.index,
                            nanoseconds,
                            sensor_timestamp_ms,
                            frames.timestamp_domain,
                            f"data/{filename}",
                        )
                    )
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
        if metadata_count:
            streams["frame_metadata"] = {
                "kind": "metadata",
                "index": "frame_metadata/index.jsonl",
                "count": metadata_count,
                "key": "group_id",
            }
        else:
            os.remove(metadata_path)
            os.rmdir(os.path.dirname(metadata_path))
        streams |= _write_motion(archive, destination, time_range)

    return streams, time_range


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


def _write_motion(
    archive: ArchiveSource, destination: str, time_range: _TimeRange
) -> dict[str, Any]:
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
        if sample.capture_monotonic is None or not time_range.contains(
            sample.capture_monotonic
        ):
            continue
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
    paths: SessionPaths,
    destination: str,
    time_range: _TimeRange,
) -> dict[str, Any]:
    """Copy the WAV and write its measured time mapping beside it.

    Args:
        paths: Where the session lives.
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
    with wave.open(paths.audio, "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        total_samples = source.getnframes()
    entry: dict[str, Any] = {
        "kind": "audio",
        "file": "audio/audio.wav",
        "rate": rate,
        "channels": channels,
        "clock": "audio/clock.csv",
    }

    points = 0
    try:
        timeline = AudioTimeline.read(paths.audio_clock, rate)
    except (OSError, ValueError) as error:
        # A WAV with no time mapping can only be read as frames / nominal rate,
        # which is the assumption the sidecar exists to avoid. Exported anyway,
        # with the absence visible, rather than refusing the whole session.
        logger.warning("no usable audio clock: %s", error)
        timeline = None

    if timeline is None and time_range.selected:
        raise SessionError(
            "cannot make a time-aligned partial export without a usable audio clock"
        )

    start_sample = 0
    end_sample = total_samples
    if timeline is not None and time_range.start is not None:
        start_sample = max(0, math.ceil(timeline.sample_at(time_range.start)))
    if timeline is not None and time_range.end is not None:
        end_sample = min(total_samples, math.ceil(timeline.sample_at(time_range.end)))
    start_sample = min(start_sample, total_samples)
    end_sample = max(start_sample, end_sample)
    _write_wav_range(
        paths.audio,
        os.path.join(folder, "audio.wav"),
        start_sample,
        end_sample,
    )
    entry["samples"] = end_sample - start_sample
    entry["source_samples"] = {"start": start_sample, "end": end_sample}

    if timeline is not None:
        output_points = (
            _cropped_clock_points(timeline, start_sample, end_sample)
            if time_range.selected
            else [
                (point.sample, point.monotonic, point.filled)
                for point in timeline.points
            ]
        )
        with open(
            os.path.join(folder, "clock.csv"), "w", encoding="utf-8", newline=""
        ) as handle:
            rows = csv.writer(handle)
            rows.writerow(("sample", "t_ns", "filled"))
            for sample, monotonic, filled in output_points:
                rows.writerow((sample, _ns(monotonic), filled))
                points += 1
        output_timeline = AudioTimeline(
            [
                AudioClockPoint(sample, monotonic, filled)
                for sample, monotonic, filled in output_points
            ],
            rate,
        )
        _write_json(
            os.path.join(folder, "clock_fit.json"), output_timeline.report().as_dict()
        )
        entry["fit"] = "audio/clock_fit.json"

    entry["clock_points"] = points
    return {"audio": entry}


def _write_wav_range(
    source_path: str, destination_path: str, start: int, end: int
) -> None:
    """Copy a half-open sample range without re-encoding the PCM payload."""
    with (
        wave.open(source_path, "rb") as source,
        wave.open(destination_path, "wb") as out,
    ):
        out.setparams(source.getparams())
        source.setpos(start)
        out.writeframes(source.readframes(end - start))


def _cropped_clock_points(
    timeline: AudioTimeline, start: int, end: int
) -> list[tuple[int, float, int]]:
    """Rebase measured audio clock points onto a cropped WAV."""
    points: list[tuple[int, float, int]] = [(0, timeline.monotonic_at(start), 0)]
    points.extend(
        (point.sample - start, point.monotonic, point.filled)
        for point in timeline.points
        if start < point.sample < end
    )
    final = (end - start, timeline.monotonic_at(end), 0)
    if final[0] != points[-1][0]:
        points.append(final)
    return points


def _write_doa(
    paths: SessionPaths, destination: str, time_range: _TimeRange
) -> dict[str, Any]:
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
    with (
        _IndexWriter(destination, "doa", ("t_ns", "angle_deg", "voice")) as writer,
        open(paths.doa, encoding="utf-8") as handle,
    ):
        for line in handle:
            line = line.strip()
            if not line:
                continue
            reading = json.loads(line)
            if not time_range.contains(float(reading["t"])):
                continue
            writer.row((_ns(reading["t"]), reading["angle"], int(reading["voice"])))
            count += 1
    if not count:
        os.remove(writer.path)
        os.rmdir(os.path.dirname(writer.path))
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


def _write_events(
    paths: SessionPaths, destination: str, time_range: _TimeRange
) -> dict[str, Any]:
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
    events = [event for event in events if time_range.contains(event.monotonic)]
    if not events:
        return {}

    with _IndexWriter(destination, "events", ("t_ns", "label", "data")) as writer:
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


# -- validation and publication ----------------------------------------------


def validate_export(directory: str) -> list[str]:
    """Return contradictions found in a completed neutral export.

    Validation is deliberately filesystem-only: it uses no archive reader and
    therefore checks the same boundary an external consumer sees.
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
    if not isinstance(clock, str):
        problems.append(f"stream {name} has no audio clock")
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


def _publish(temporary: str, destination: str, *, overwrite: bool) -> None:
    """Atomically make a validated temporary export visible."""
    if not os.path.exists(destination):
        os.replace(temporary, destination)
        return
    if not overwrite:
        raise FileExistsError(destination)
    backup = f"{destination}.previous-{os.getpid()}-{time.time_ns()}"
    os.replace(destination, backup)
    try:
        os.replace(temporary, destination)
    except Exception:
        os.replace(backup, destination)
        raise
    else:
        shutil.rmtree(backup)


# -- plumbing -----------------------------------------------------------------


class _IndexWriter:
    """A stream's ``index.csv``, created with its directory."""

    def __init__(self, destination: str, name: str, columns: tuple[str, ...]) -> None:
        folder = os.path.join(destination, name)
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "index.csv")
        # Lifetime is managed by close()/__exit__, not this constructor scope.
        self._handle = open(  # noqa: SIM115
            self.path, "w", encoding="utf-8", newline=""
        )
        self._writer = csv.writer(self._handle)
        self._writer.writerow(columns)

    def __enter__(self) -> Self:
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
    return round(seconds * 1e9)


def _write_json(path: str, payload: Any) -> None:
    """Write a JSON file with a trailing newline."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False, default=float)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
