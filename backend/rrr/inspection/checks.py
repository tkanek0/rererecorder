"""Check what a recorded session actually says about its own timing.

Cross-checks the files against each other rather than summarising the
manifest; see docs/features.md "The command line". ``scripts/inspect_session.py``
prints the result.
"""

from __future__ import annotations

import json
import sqlite3
import wave
from dataclasses import dataclass

import numpy as np
from realsense_adapter import StreamError

from rrr.timeline import (
    AudioTimeline,
    SessionManifest,
    SessionPaths,
    read_events,
)
from rrr.video import ArchiveSource

#: How far the header's and the clock points' audio lengths may differ, in
#: milliseconds. One 16 ms block; past that they describe different recordings.
LENGTH_TOLERANCE_MS = 16.0

#: Rate error past which a recording will visibly drift against the video.
#: 100 ppm is 0.36 s an hour.
RATE_WARN_PPM = 100.0

#: Residual past which the audio time axis is not a straight line, in
#: milliseconds. Measured jitter is 0.03 ms rms on Linux, so 1 ms is 30x that.
RESIDUAL_WARN_MS = 1.0

#: Shortest recording whose fitted sample rate is worth reporting, in seconds.
#: Measured: 4 s sessions fitted +51, +11 and +15 ppm and a 10 s one -14 ppm -
#: scatter from the short fit, not the crystal.
MIN_RATE_SPAN_S = 30.0


class Check:
    """Accumulates findings so that every check runs before anything is judged."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        """Record something that makes the session unsynchronised."""
        self.problems.append(message)

    def note(self, message: str) -> None:
        """Record something worth knowing that is not a failure."""
        self.notes.append(message)


@dataclass(frozen=True)
class Inspection:
    """What a session's files say about their own timing, checked against each other.

    Attributes:
        session_id: The session inspected.
        audio: What the WAV and its clock points agree on, or None if no audio.
        video: Frame counts, rate and losses, or None if no video.
        imu: Inertial sample counts and rates, or None if none was recorded.
        overlap: How the audio and video spans line up, or None.
        doa: Direction readings, or None.
        marks: Marks made while recording, or None.
        problems: Disagreements that make the session unsynchronised.
        notes: Things worth knowing that are not failures.
    """

    session_id: str
    audio: dict | None
    video: dict | None
    imu: dict | None
    overlap: dict | None
    doa: dict | None
    marks: dict | None
    problems: list[str]
    notes: list[str]

    @property
    def agreed(self) -> bool:
        """Return whether every cross-check agreed."""
        return not self.problems

    def as_dict(self) -> dict:
        """Return a JSON-serialisable view of the findings."""
        return {
            "session_id": self.session_id,
            "audio": self.audio,
            "video": self.video,
            "imu": self.imu,
            "overlap": self.overlap,
            "doa": self.doa,
            "marks": self.marks,
            "problems": self.problems,
            "notes": self.notes,
        }


def inspect_session(paths: SessionPaths, manifest: SessionManifest) -> Inspection:
    """Run every cross-check on one session.

    Args:
        paths: Where the session lives.
        manifest: Its manifest, already read.

    Returns:
        The findings. Every check runs before anything is judged.
    """
    check = Check()
    _check_recorded(manifest, check)
    audio = _check_audio(paths, manifest, check)
    video = _check_video(paths, manifest, check)
    imu = _check_imu(paths, manifest, video, check)
    overlap = _check_overlap(audio, video, manifest, check)
    doa = _check_doa(paths, manifest, audio, check)
    marks = _check_events(paths, audio, video, check)
    return Inspection(
        session_id=manifest.session_id,
        audio=audio,
        video=video,
        imu=imu,
        overlap=overlap,
        doa=doa,
        marks=marks,
        problems=check.problems,
        notes=check.notes,
    )


def _check_recorded(manifest: SessionManifest, check: Check) -> None:
    """Fail a session whose recorder said something went wrong, or that is empty."""
    if manifest.video is None and manifest.audio is None:
        check.fail("neither device was recorded")
    for error in manifest.errors:
        check.fail(f"the recorder reported: {error}")


def _outside(times: list[float], first: float, last: float, slack: float) -> int:
    """How many of ``times`` fall more than ``slack`` outside ``[first, last]``."""
    return sum(1 for t in times if t < first - slack or t > last + slack)


# -- audio --------------------------------------------------------------------


def _check_audio(
    paths: SessionPaths, manifest: SessionManifest, check: Check
) -> dict[str, object] | None:
    """Read the WAV and its clock sidecar, and make them argue.

    Args:
        paths: Where the session lives.
        manifest: What the recorder said.
        check: Where findings go.

    Returns:
        What was measured, or None if there is no audio.
    """
    if manifest.audio is None:
        return None
    try:
        with wave.open(paths.audio, "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
    except (OSError, wave.Error) as error:
        check.fail(f"the WAV cannot be read: {error}")
        return None

    if frames == 0:
        check.fail("the WAV holds no samples")
        return None

    if frames != manifest.audio.samples:
        check.fail(
            f"the manifest says {manifest.audio.samples} samples and the WAV "
            f"holds {frames}"
        )

    try:
        timeline = AudioTimeline.read(paths.audio_clock, rate)
    except (OSError, ValueError) as error:
        check.fail(f"the audio clock sidecar cannot be read: {error}")
        return {"frames": frames, "rate": rate, "channels": channels}

    report = timeline.report()

    # Two independent lengths: frames / nominal rate, and the fitted clock.
    by_header = frames / rate
    by_clock = timeline.monotonic_at(frames) - timeline.monotonic_at(0)
    disagreement_ms = abs(by_header - by_clock) * 1000.0
    if disagreement_ms > LENGTH_TOLERANCE_MS:
        check.fail(
            f"the WAV says it is {by_header:.3f} s long and its clock points "
            f"say {by_clock:.3f} s - {disagreement_ms:.0f} ms apart"
        )

    if report.residual_max_ms is not None and report.residual_max_ms > RESIDUAL_WARN_MS:
        check.fail(
            f"the audio time axis is not a straight line: {report.residual_max_ms:.1f} "
            f"ms worst departure. A hole was missed, or the fill was wrong"
        )
    if report.filled:
        check.note(
            f"{report.filled} samples ({report.filled / rate * 1000:.0f} ms) of "
            f"silence replace audio that was lost"
        )

    if report.rate_error_ppm is not None:
        if report.span_s < MIN_RATE_SPAN_S:
            check.note(
                f"the fitted sample rate ({report.rate_error_ppm:+.0f} ppm) is from "
                f"only {report.span_s:.1f} s and is not reliable; "
                f"{MIN_RATE_SPAN_S:.0f} s or more is needed to mean anything"
            )
        elif abs(report.rate_error_ppm) > RATE_WARN_PPM:
            check.note(
                f"the converter ran {report.rate_error_ppm:+.0f} ppm off nominal, "
                f"which is {abs(report.rate_error_ppm) * 3.6:.1f} ms of drift an hour"
            )

    return {
        "frames": frames,
        "rate": rate,
        "channels": channels,
        "seconds_by_header": by_header,
        "seconds_by_clock": by_clock,
        "disagreement_ms": disagreement_ms,
        "first_monotonic": timeline.monotonic_at(0),
        "last_monotonic": timeline.monotonic_at(frames),
        "report": report.as_dict(),
    }


# -- video --------------------------------------------------------------------


def _check_video(
    paths: SessionPaths, manifest: SessionManifest, check: Check
) -> dict[str, object] | None:
    """Read the archive's timestamps straight out of SQLite and check them.

    Args:
        paths: Where the session lives.
        manifest: What the recorder said.
        check: Where findings go.

    Returns:
        What was measured, or None if there is no video.

    Plain SQL rather than ``ArchiveSource``, so no image is decoded.
    """
    if manifest.video is None:
        return None
    try:
        with sqlite3.connect(f"file:{paths.video}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT idx, received_monotonic, metadata FROM frames ORDER BY idx"
            ).fetchall()
    except sqlite3.Error as error:
        check.fail(f"the archive cannot be read: {error}")
        return None

    if not rows:
        check.fail("the archive holds no frames")
        return None

    if len(rows) != manifest.video.frames:
        check.fail(
            f"the manifest says {manifest.video.frames} frames and the archive "
            f"holds {len(rows)}"
        )

    domain = manifest.video.timestamp_domain
    if domain != "global_time":
        # Not a failure: coarser but usable, and the per-stream timestamps
        # stay in the data. See docs/windows-native.md #3.
        check.note(
            f"frame timestamps are in domain {domain!r}, not global_time: "
            f"colour and depth were stamped independently"
        )

    monotonic = np.array([row[1] for row in rows], dtype=np.float64)
    if np.any(np.isnan(monotonic)):
        check.fail("some frames have no capture time")
        return None

    gaps = np.diff(monotonic)
    if np.any(gaps <= 0):
        check.fail("frame capture times are not increasing")

    counters: dict[str, list[int]] = {"color": [], "depth": []}
    for row in rows:
        if not row[2]:
            continue
        for stream, fields in json.loads(row[2]).items():
            if stream in counters and "frame_counter" in fields:
                counters[stream].append(int(fields["frame_counter"]))
    missing: dict[str, dict[str, int]] = {}
    for stream, numbers in counters.items():
        if not numbers:
            continue
        count = count_missing(numbers)
        missing[stream] = count
        if count["missing"]:
            check.fail(
                f"{count['missing']} of the {count['span']} {stream} frames the "
                f"camera numbered never reached the recorder"
            )
        if count["restarts"]:
            check.fail(
                f"the {stream} frame counter restarted {count['restarts']} times: "
                f"the stream restarted mid-recording, losing an uncounted number "
                f"of frames at each"
            )
    if not missing:
        # Without UVC metadata the SDK's counter is the host's own, and gapless
        # by construction: docs/windows-native.md, "The actual fix".
        check.note(
            "no device frame counters, so frames lost before the recorder are "
            "not counted"
        )

    span = float(monotonic[-1] - monotonic[0])
    fps = (len(rows) - 1) / span if span > 0 else None
    return {
        "missing": missing,
        "frames": len(rows),
        "span_s": span,
        "fps": fps,
        "first_monotonic": float(monotonic[0]),
        "last_monotonic": float(monotonic[-1]),
        "gap_ms": {
            "median": float(np.median(gaps)) * 1000.0,
            "min": float(np.min(gaps)) * 1000.0,
            "max": float(np.max(gaps)) * 1000.0,
        },
    }


def count_missing(numbers: list[int]) -> dict[str, int]:
    """Count the frames a camera numbered but never delivered.

    Args:
        numbers: One stream's ``frame_counter`` per recorded set, in order.
            A set may repeat the previous frame (decision 21), which is not a
            loss.

    Returns:
        ``span`` (frames numbered), ``delivered`` (distinct frames),
        ``missing`` and ``restarts``. A counter going backwards is a restarted
        stream; the runs between restarts are counted separately, and what was
        lost across a restart is not knowable from the counter.
    """
    runs: list[list[int]] = [[numbers[0]]]
    for number in numbers[1:]:
        if number < runs[-1][-1]:
            runs.append([])
        runs[-1].append(number)
    span = sum(run[-1] - run[0] + 1 for run in runs)
    delivered = sum(len(set(run)) for run in runs)
    return {
        "span": span,
        "delivered": delivered,
        "missing": span - delivered,
        "restarts": len(runs) - 1,
    }


# -- the inertial sensor ------------------------------------------------------

#: How far the magnitude of a still accelerometer may sit from gravity, in
#: m/s^2, before it is worth remarking on. A moving camera fails it legitimately.
GRAVITY_TOLERANCE = 0.5


def _check_imu(
    paths: SessionPaths,
    manifest: SessionManifest,
    video: dict[str, object] | None,
    check: Check,
) -> dict[str, object] | None:
    """Read the inertial samples and see whether they describe this recording.

    Args:
        paths: Where the session lives.
        manifest: What the recorder said.
        video: What the video check measured, for the time range to compare to.
        check: Where findings go.

    Returns:
        What was measured, or None if there is no inertial data.
    """
    if manifest.video is None:
        return None
    try:
        with ArchiveSource(paths.video) as archive:
            rates = archive.motion_rate()
            samples = list(archive.motion_samples())
    except StreamError as error:
        check.fail(f"the inertial samples cannot be read: {error}")
        return None

    if not samples:
        if manifest.video.motion:
            check.fail(
                f"the manifest claims {manifest.video.motion} inertial samples "
                f"and the archive holds none"
            )
        return None

    if manifest.video.motion and len(samples) != manifest.video.motion:
        check.fail(
            f"the manifest says {manifest.video.motion} inertial samples and "
            f"the archive holds {len(samples)}"
        )

    result: dict[str, object] = {"samples": len(samples), "rates": rates}

    # Catches a timestamp domain mismatch, which is otherwise self-consistent.
    placed = [
        s.capture_monotonic for s in samples if s.capture_monotonic is not None
    ]
    if not placed:
        check.fail("inertial samples carry no clock, so they cannot be placed")
    elif video is not None and "first_monotonic" in video:
        first, last = min(placed), max(placed)
        result["first_monotonic"] = first
        result["last_monotonic"] = last
        if _outside(
            [first, last], video["first_monotonic"], video["last_monotonic"], 2.0
        ):
            check.fail(
                f"inertial samples span {last - first:.1f} s but sit outside "
                f"the video they are supposed to accompany"
            )

    accel = np.array(
        [[s.x, s.y, s.z] for s in samples if s.stream == "accel"], dtype=np.float64
    )
    if accel.size:
        magnitude = float(np.median(np.linalg.norm(accel, axis=1)))
        result["accel_magnitude"] = magnitude
        if abs(magnitude - 9.81) > GRAVITY_TOLERANCE:
            check.note(
                f"the accelerometer's median magnitude is {magnitude:.2f} m/s^2, "
                f"not 9.81 - expected if the camera was moving, wrong if it was "
                f"not"
            )

    # Only inside the video's span: the sensor's start-up gaps (12-70 ms,
    # measured) all fall before the first frame. See docs/decisions.md 12.
    window = (
        (video["first_monotonic"], video["last_monotonic"])
        if video is not None and "first_monotonic" in video
        else None
    )
    gaps: dict[str, float] = {}
    for stream in rates:
        times = np.array(
            [
                s.capture_monotonic
                for s in samples
                if s.stream == stream
                and s.capture_monotonic is not None
                and (
                    window is None
                    or window[0] <= s.capture_monotonic <= window[1]
                )
            ]
        )
        if times.size > 2:
            gaps[stream] = float(np.max(np.diff(np.sort(times)))) * 1000.0
    result["largest_gap_ms"] = gaps
    for stream, gap in gaps.items():
        expected = 1000.0 / max(rates.get(stream, 1.0), 1.0)
        # Ten intervals: past the odd scheduling hiccup.
        if gap > expected * 10:
            check.fail(
                f"the {stream} stream has a {gap:.0f} ms gap inside the "
                f"recording, against a {expected:.1f} ms interval - samples "
                f"were lost"
            )

    return result


# -- the two together ---------------------------------------------------------


def _check_overlap(
    audio: dict[str, object] | None,
    video: dict[str, object] | None,
    manifest: SessionManifest,
    check: Check,
) -> dict[str, object] | None:
    """Find the part of the session where both devices were recording.

    Args:
        audio: What the audio check measured.
        video: What the video check measured.
        manifest: The session, for its calibration.
        check: Where findings go.

    Returns:
        The overlap, or None if only one device recorded.
    """
    if audio is None or video is None:
        return None
    if "first_monotonic" not in audio:
        return None

    start = max(audio["first_monotonic"], video["first_monotonic"])
    end = min(audio["last_monotonic"], video["last_monotonic"])
    seconds = max(0.0, end - start)
    if seconds <= 0:
        check.fail("the two tracks do not overlap at all")

    return {
        "start_monotonic": start,
        "end_monotonic": end,
        "seconds": seconds,
        "audio_lead_s": video["first_monotonic"] - audio["first_monotonic"],
        "calibrated": manifest.calibration.measured,
        "offset_s": manifest.calibration.offset_s,
    }


def _check_doa(
    paths: SessionPaths,
    manifest: SessionManifest,
    audio: dict[str, object] | None,
    check: Check,
) -> dict[str, object] | None:
    """Check the direction was recorded, and that its times land inside the audio.

    Args:
        paths: Where the session lives.
        manifest: What the recorder said.
        audio: What the audio check measured.
        check: Where findings go.

    Returns:
        What was measured, or None if no direction was recorded.
    """
    if not manifest.doa:
        return None
    try:
        with open(paths.doa, encoding="utf-8") as handle:
            times = [json.loads(line)["t"] for line in handle if line.strip()]
    except OSError:
        times = []
    if not times:
        check.fail("the direction was recorded and holds no readings")
        return {"readings": 0}

    result: dict[str, object] = {
        "readings": len(times),
        "first_monotonic": times[0],
        "last_monotonic": times[-1],
        "hz": (
            (len(times) - 1) / (times[-1] - times[0]) if times[-1] > times[0] else None
        ),
    }
    # Outside the audio would mean the two are on different clocks.
    if audio is not None and "first_monotonic" in audio:
        if _outside(times, audio["first_monotonic"], audio["last_monotonic"], 1.0):
            check.fail(
                "direction readings fall outside the audio they are supposed to "
                "describe"
            )
    return result


def _check_events(
    paths: SessionPaths,
    audio: dict[str, object] | None,
    video: dict[str, object] | None,
    check: Check,
) -> dict[str, object] | None:
    """Check the marks fall inside the recording they describe.

    Args:
        paths: Where the session lives.
        audio: What the audio check measured.
        video: What the video check measured.
        check: Where findings go.

    Returns:
        What was measured, or None if nobody marked anything.
    """
    try:
        events = read_events(paths.events)
    except ValueError as error:
        check.fail(f"unreadable mark: {error}")
        return None
    if not events:
        return None

    times = [event.monotonic for event in events]
    result: dict[str, object] = {
        "marks": len(events),
        "first_monotonic": times[0],
        "last_monotonic": times[-1],
        "labels": [event.label for event in events],
    }

    bounds = [
        track
        for track in (audio, video)
        if track is not None and "first_monotonic" in track
    ]
    if bounds:
        first = min(float(track["first_monotonic"]) for track in bounds)
        last = max(float(track["last_monotonic"]) for track in bounds)
        outside = _outside(times, first, last, 0.0)
        if outside:
            check.fail(f"{outside} of {len(times)} marks fall outside the recording")
    return result
