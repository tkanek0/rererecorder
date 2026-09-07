"""Check what a recorded session actually says about its own timing.

    uv run python -m rrr.tools.inspect var/sessions/2026-09-01_17-30-00

Everything here is a cross-check rather than a summary. The manifest already
says what the recorder believed; this reads the files themselves and asks
whether they agree - with each other, and with the arithmetic. The interesting
answers are the disagreements:

* the WAV's own length against what the measured clock points predict,
* the archive's frame timestamps against their stored monotonic times,
* each track's span against the other's, which is the only part of a session
  where audio and video can be compared at all,
* the direction sidecar's times against the audio's.

A recording that passes all of these is one where a sample can be placed against
a frame. A recording that fails one of them is still worth having - the
measurements are real - but it is not synchronised, and this is where that gets
found out rather than three months later.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import wave

import numpy as np

from rrr.timeline import (
    AudioTimeline,
    SessionManifest,
    SessionPaths,
    read_events,
    read_manifest,
)
from rrr.video import ArchiveSource, StreamError

#: How far the two independent estimates of the audio's length may differ before
#: it is called a disagreement, in milliseconds.
#:
#: One block at 16 kHz is 16 ms, and the two estimates are built from the same
#: samples through different arithmetic - the header's nominal rate against the
#: fitted one - so anything past a block means the sidecar and the file describe
#: different recordings.
LENGTH_TOLERANCE_MS = 16.0

#: Rate error past which a recording will visibly drift against the video.
#:
#: 100 ppm is 0.36 s an hour. Below about 50 ppm nothing shorter than an hour is
#: affected; above a few hundred, something is wrong beyond a crystal.
RATE_WARN_PPM = 100.0

#: Residual past which the audio time axis is not a straight line, in
#: milliseconds. Measured jitter is 0.03 ms rms on a real recording, so 1 ms is
#: thirty times the noise and well below one block.
RESIDUAL_WARN_MS = 1.0

#: Shortest recording whose fitted sample rate is worth reporting, in seconds.
#:
#: Measured on four real recordings: 4 second sessions fitted +51, +11 and +15
#: ppm, while a 10 second one fitted -14 ppm. The scatter is the fit's, not the
#: crystal's - a short span gives the slope nothing to lean on - so a rate from
#: a few seconds of audio says less than it appears to.
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

    root, _, session_id = args.directory.rstrip("/").rpartition("/")
    paths = SessionPaths.resolve(root or ".", session_id)
    manifest = read_manifest(paths)
    check = Check()

    audio = _check_audio(paths, manifest, check)
    video = _check_video(paths, manifest, check)
    imu = _check_imu(paths, manifest, video, check)
    overlap = _check_overlap(audio, video, manifest, check)
    doa = _check_doa(paths, audio, check)
    marks = _check_events(paths, audio, video, check)

    if args.json:
        json.dump(
            {
                "session_id": manifest.session_id,
                "audio": audio,
                "video": video,
                "imu": imu,
                "overlap": overlap,
                "doa": doa,
                "marks": marks,
                "problems": check.problems,
                "notes": check.notes,
            },
            sys.stdout,
            indent=2,
            default=float,
        )
        print()
    else:
        _print(manifest, audio, video, imu, overlap, doa, marks, check)

    return 1 if check.problems else 0


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

    # Two independent estimates of how long the audio is. The header's is
    # frames / nominal rate; the sidecar's is the time between the first and
    # last measured points, extended to the ends of the file. They are built
    # from different numbers and must agree.
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

    Read with plain SQL rather than through ``ArchiveSource``: the question is
    about the file's timing, and decoding 300 PNG pairs to answer it would cost
    seconds and prove nothing extra.
    """
    if manifest.video is None:
        return None
    try:
        with sqlite3.connect(f"file:{paths.video}?mode=ro", uri=True) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(frames)")
            }
            if "capture_monotonic" not in columns:
                check.fail(
                    "the archive has no capture_monotonic column, so its frames "
                    "cannot be placed against the audio"
                )
                return None
            rows = connection.execute(
                "SELECT idx, timestamp_ms, received_at, capture_monotonic "
                "FROM frames ORDER BY idx"
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
        check.fail(
            f"frame timestamps are in domain {domain!r}, not global_time: they are "
            f"on the camera's own clock and cannot be compared with audio times"
        )

    epoch_ms = np.array([row[1] for row in rows], dtype=np.float64)
    received = np.array([row[2] for row in rows], dtype=np.float64)
    monotonic = np.array([row[3] for row in rows], dtype=np.float64)

    if np.any(np.isnan(monotonic)):
        check.fail("some frames have no capture time")
        return None

    # The stored conversion must be the one the clock samples describe. Checked
    # against the offset in force for each frame, so an NTP step during the
    # recording does not look like a broken conversion.
    track = manifest.clock_track
    if track.samples:
        expected = np.array(
            [
                (track.at(m) or track.samples[0]).epoch_ms_to_monotonic(ms)
                for ms, m in zip(epoch_ms, monotonic, strict=True)
            ]
        )
        worst_ms = float(np.max(np.abs(expected - monotonic))) * 1000.0
        if worst_ms > 10.0:
            check.fail(
                f"stored capture times disagree with the session's clock samples "
                f"by up to {worst_ms:.1f} ms"
            )
    else:
        worst_ms = None
        check.note("the session recorded no clock samples to check against")

    gaps = np.diff(monotonic)
    if np.any(gaps <= 0):
        check.fail("frame capture times are not increasing")

    # A frame arrives after the instant it describes, never before.
    lag_ms = (received - monotonic) * 1000.0
    if np.any(lag_ms < -1.0):
        check.fail(
            f"some frames claim to have arrived {abs(float(np.min(lag_ms))):.1f} ms "
            f"before they were taken"
        )

    span = float(monotonic[-1] - monotonic[0])
    fps = (len(rows) - 1) / span if span > 0 else None
    return {
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
        "arrival_lag_ms": {
            "median": float(np.median(lag_ms)),
            "min": float(np.min(lag_ms)),
            "max": float(np.max(lag_ms)),
        },
        "conversion_worst_ms": worst_ms,
    }


# -- the inertial sensor ------------------------------------------------------

#: Sample rate below which the recording clearly holds one sample per video
#: frame rather than the sensor's own output.
#:
#: A D455 runs its accelerometer at 482 Hz and its gyroscope at 478. Anything
#: near 30 is the old per-frame behaviour, which is a fourteenth of the data.
MIN_IMU_HZ = 100.0

#: How far the magnitude of a still accelerometer may sit from gravity, in
#: m/s^2, before it is worth remarking on.
#:
#: A stationary accelerometer measures specific force, which is 9.81 upwards.
#: This is the one check on the inertial data that comes from physics rather
#: than from the file agreeing with itself - so it is worth making, even though
#: a camera that was moving will fail it legitimately.
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

    for stream, rate in rates.items():
        if rate < MIN_IMU_HZ:
            check.note(
                f"the {stream} stream was recorded at {rate:.0f} Hz, which is "
                f"video frame rate rather than the sensor's own - this "
                f"recording holds a fraction of what the IMU measured"
            )

    # The samples have to lie on the same axis as the frames, or they cannot be
    # used with them. This is the check that would catch a timestamp domain
    # mismatch, which would otherwise look perfectly self-consistent.
    placed = [
        s.capture_monotonic for s in samples if s.capture_monotonic is not None
    ]
    if not placed:
        check.fail("inertial samples carry no clock, so they cannot be placed")
    elif video is not None and "first_monotonic" in video:
        slack = 2.0
        first, last = min(placed), max(placed)
        result["first_monotonic"] = first
        result["last_monotonic"] = last
        if (
            first < video["first_monotonic"] - slack
            or last > video["last_monotonic"] + slack
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

    # Gaps are looked for only inside the video's own span. The inertial sensor
    # starts up to 0.7 s before the first frame and settles during that time -
    # measured, four gaps of 12 to 70 ms, all of them before the video began and
    # none after. Counting those would report every healthy recording as lossy.
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
                if (s.stream == stream or stream == "both")
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
        # Ten intervals: a real drop shows up as a multiple, and the odd
        # scheduling hiccup does not.
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
    paths: SessionPaths, audio: dict[str, object] | None, check: Check
) -> dict[str, object] | None:
    """Check the direction sidecar's times land inside the audio.

    Args:
        paths: Where the session lives.
        audio: What the audio check measured.
        check: Where findings go.

    Returns:
        What was measured, or None if there is no direction track.
    """
    try:
        with open(paths.doa, encoding="utf-8") as handle:
            times = [json.loads(line)["t"] for line in handle if line.strip()]
    except OSError:
        return None
    if not times:
        return {"readings": 0}

    result: dict[str, object] = {
        "readings": len(times),
        "first_monotonic": times[0],
        "last_monotonic": times[-1],
        "hz": (
            (len(times) - 1) / (times[-1] - times[0]) if times[-1] > times[0] else None
        ),
    }
    if audio is not None and "first_monotonic" in audio:
        # A bearing outside the audio cannot be lined up against anything, which
        # would mean the two are on different clocks after all.
        slack = 1.0
        if (
            times[0] < audio["first_monotonic"] - slack
            or times[-1] > audio["last_monotonic"] + slack
        ):
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

    A mark outside both tracks means the sidecar belongs to a different session
    or the clocks disagree, and either way it cannot be used to say what a
    stretch of the recording was.
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
        outside = [t for t in times if t < first or t > last]
        if outside:
            check.fail(
                f"{len(outside)} of {len(times)} marks fall outside the recording"
            )
    return result


# -- output -------------------------------------------------------------------


def _print(manifest, audio, video, imu, overlap, doa, marks, check: Check) -> None:
    """Write the findings for a person to read."""
    print(f"session {manifest.session_id}")
    if manifest.started_at is not None:
        import time as _time

        stamp = _time.strftime(
            "%Y-%m-%d %H:%M:%S", _time.localtime(manifest.started_at.realtime)
        )
        print(f"  started         {stamp}")
    if manifest.duration_s is not None:
        print(f"  duration        {manifest.duration_s:.2f} s")

    track = manifest.clock_track
    drift = track.drift_ppm
    if drift is not None:
        print(
            f"  clock offset    {track.samples[0].offset:.6f} s, "
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
        lag = video["arrival_lag_ms"]
        print(
            f"  arrival lag     {lag['median']:.1f} ms median "
            f"({lag['min']:.1f} to {lag['max']:.1f})"
        )
        if video["conversion_worst_ms"] is not None:
            print(
                f"  conversion      agrees with the clock samples to "
                f"{video['conversion_worst_ms']:.3f} ms"
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
        # Distinct labels, and only a few of them: a session of an experiment
        # can carry one mark per run, and a line listing forty of them buries
        # the checks around it.
        distinct = list(dict.fromkeys(marks["labels"]))
        shown = ", ".join(distinct[:5])
        if len(distinct) > 5:
            shown += f", and {len(distinct) - 5} more"
        print(f"  marks           {marks['marks']} ({shown})")

    for note in check.notes:
        print(f"  note            {note}")
    for problem in check.problems:
        print(f"  PROBLEM         {problem}")
    if not check.problems:
        print("  every cross-check agreed")


if __name__ == "__main__":
    raise SystemExit(main())
