"""The manifest that makes a recorded session self-describing.

A session is a directory, not a file. Two devices with different natural formats
are being recorded at once, and forcing both into one container would mean the
audio could no longer be opened by anything that opens a WAV - which is most of
what anyone would want to do with it.

    var/sessions/2026-09-01_17-30-00/
        session.json        this manifest
        video.rsdb          frames, calibration, sensor options, inertial samples
        audio.wav           every channel, int16, gaps filled with silence
        audio.clock.jsonl   measured capture time per block
        doa.jsonl           the array's direction estimate

The manifest is what ties them together. Without it the directory is three
recordings that happen to share a folder: the WAV has no start time, and the
archive's frame timestamps are on a clock the WAV knows nothing about. With it,
any sample can be placed against any frame.

What it deliberately does **not** claim is that the two are aligned. The
absolute offset between the array's converter and the camera's shutter cannot be
derived from either device's documentation - each reports its own idea of when a
measurement happened, and the paths in between are not specified - so it is left
null until something measures it. ``rrr/tools/calibrate.py`` does that from a
handclap; until it has run, ``calibration.offset_s`` is None and every consumer
knows the alignment is only as good as the two clocks.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace

from .audio_clock import SUFFIX as AUDIO_CLOCK_SUFFIX
from .clock import ClockPair, ClockTrack

#: Bumped when the layout changes in a way a reader must know about.
FORMAT_VERSION = 1

#: File names inside a session directory. Fixed rather than recorded per session
#: so that a directory can be understood without reading the manifest first.
MANIFEST_NAME = "session.json"
VIDEO_NAME = "video.rrdb"
AUDIO_NAME = "audio.wav"
AUDIO_CLOCK_NAME = f"audio{AUDIO_CLOCK_SUFFIX}"
DOA_NAME = "doa.jsonl"

#: Session directory names this module will produce and accept back.
#:
#: A session id reaches the filesystem from an HTTP path, so it is rejected
#: rather than sanitised. No dots at all: a name with none cannot be a relative
#: path, which is a stronger statement than checking for "..".
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SessionError(Exception):
    """Raised for a session that cannot be named, found or read."""


@dataclass(frozen=True)
class VideoTrack:
    """What was recorded from the camera.

    Attributes:
        file: Archive name inside the session directory.
        frames: Frames written.
        dropped: Frames the encoder queue could not accept. Non-zero means the
            disk or the CPU could not keep up, and is reported rather than
            hidden.
        skipped_warmup: Sets discarded before the first one was written, while
            the SDK's syncer settled. Measured on a D455: three, every time,
            within the same millisecond as ``pipeline.start``. Not a loss, and
            kept apart from the counts below so that a recording which lost
            nothing does not report a number that looks like it did.
        skipped_duplicate: Sets discarded mid-stream because every frame in them
            had already been delivered.
        skipped_unpaired: Sets discarded because their streams disagreed about
            when they were taken by more than a few milliseconds. Measured on a
            D455: the first five sets after ``pipeline.start`` pair one stale
            depth frame with five successive colour frames, 294 to 432 ms apart,
            and a dropped depth frame later in the stream does the same thing
            once. Neither is a moment in time, so neither belongs in a recording
            that claims to be synchronised - but the count belongs in the
            manifest, because it says how hard the camera was working to keep
            the streams paired.
        first_monotonic: Capture time of the first frame written, on the
            monotonic axis, or None if nothing was written.
        last_monotonic: Capture time of the last frame written.
        motion: Inertial samples written. About 960 a second against 30
            frames, because the sensor is recorded at its own rate rather than
            sampled once per frame - measured 482 Hz accelerometer, 478 Hz
            gyroscope. Zero means inertial recording was off, or the sensor
            could not be opened.
        motion_overrun: Samples discarded because the writer did not drain the
            source's buffer in time. Should be zero.
        timestamp_domain: What the SDK said its timestamps mean, as
            ``frame.get_frame_timestamp_domain()`` reports it. Expected to be
            ``global_time``, which is epoch milliseconds fitted to the host
            clock. If it reads ``hardware_clock`` instead, the frame times are
            on the device's own clock and nothing here can place them against
            the audio - so it is recorded, not assumed.
        fps: Frames written divided by the span they cover.
    """

    file: str = VIDEO_NAME
    frames: int = 0
    dropped: int = 0
    motion: int = 0
    motion_overrun: int = 0
    skipped_warmup: int = 0
    skipped_duplicate: int = 0
    skipped_unpaired: int = 0
    first_monotonic: float | None = None
    last_monotonic: float | None = None
    timestamp_domain: str = "unknown"
    fps: float | None = None

    @property
    def skipped(self) -> int:
        """Sets discarded mid-stream, for either reason. Excludes startup."""
        return self.skipped_duplicate + self.skipped_unpaired

    @property
    def motion_hz(self) -> float | None:
        """Inertial samples per second across the recording, both streams.

        Returns:
            The rate, or None if there is nothing to divide. Approximate: the
            span is the video's, and the sensor runs a little before and after
            it, so the figure reads slightly high. It is precise enough for
            what it is for - telling roughly 800 Hz (the sensor's own rate)
            from roughly 60 (one sample of each per video frame). For the
            measured rate, ask the archive: ``ArchiveSource.motion_rate``.
        """
        first, last = self.first_monotonic, self.last_monotonic
        if not self.motion or first is None or last is None or last <= first:
            return None
        return self.motion / (last - first)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this track."""
        return {
            "file": self.file,
            "frames": self.frames,
            "dropped": self.dropped,
            "skipped": self.skipped,
            "motion": self.motion,
            "motion_overrun": self.motion_overrun,
            "skipped_warmup": self.skipped_warmup,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_unpaired": self.skipped_unpaired,
            "first_monotonic": self.first_monotonic,
            "last_monotonic": self.last_monotonic,
            "timestamp_domain": self.timestamp_domain,
            "fps": round(self.fps, 3) if self.fps is not None else None,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> VideoTrack:
        """Rebuild a track from its stored form.

        A manifest written before the skip counts were split carries only
        ``skipped``. Reading that as zero would report a session which
        discarded 45 sets as having discarded none, so the total is put where
        it came from: every such set observed on a real camera was a
        mispairing, never a repeat.
        """
        unpaired = raw.get("skipped_unpaired")
        if unpaired is None:
            unpaired = raw.get("skipped", 0)
        return VideoTrack(
            file=str(raw.get("file", VIDEO_NAME)),
            frames=int(raw.get("frames", 0)),
            dropped=int(raw.get("dropped", 0)),
            motion=int(raw.get("motion", 0)),
            motion_overrun=int(raw.get("motion_overrun", 0)),
            skipped_warmup=int(raw.get("skipped_warmup", 0)),
            skipped_duplicate=int(raw.get("skipped_duplicate", 0)),
            skipped_unpaired=int(unpaired),  # type: ignore[arg-type]
            first_monotonic=_optional_float(raw.get("first_monotonic")),
            last_monotonic=_optional_float(raw.get("last_monotonic")),
            timestamp_domain=str(raw.get("timestamp_domain", "unknown")),
            fps=_optional_float(raw.get("fps")),
        )


@dataclass(frozen=True)
class AudioTrack:
    """What was recorded from the array.

    Attributes:
        file: WAV name inside the session directory.
        clock_file: Sidecar of measured capture times.
        rate: Nominal sample rate from the WAV header.
        channels: Channels written. All of them, whatever was being listened
            to - the raw microphones cannot be recovered from the processed one.
        samples: Frames written to the WAV, counting inserted silence.
        filled: Samples of silence inserted to replace dropped audio, so that a
            file position keeps corresponding to a capture time.
        overruns: How often the driver reported an input overflow.
        first_monotonic: Capture time of sample zero, on the monotonic axis.
        timeline: What the measured points say about the time axis, as
            :meth:`~timeline.audio_clock.AudioTimeline.report` produced it.
            Read ``rate_error_ppm`` and ``residual_max_ms`` before trusting the
            recording against the video.
    """

    file: str = AUDIO_NAME
    clock_file: str = AUDIO_CLOCK_NAME
    rate: int = 0
    channels: int = 0
    samples: int = 0
    filled: int = 0
    overruns: int = 0
    first_monotonic: float | None = None
    timeline: dict[str, object] | None = None

    @property
    def seconds(self) -> float:
        """Length of the audio as the file's own header describes it."""
        return self.samples / self.rate if self.rate else 0.0

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this track."""
        return {
            "file": self.file,
            "clock_file": self.clock_file,
            "rate": self.rate,
            "channels": self.channels,
            "samples": self.samples,
            "seconds": round(self.seconds, 3),
            "filled": self.filled,
            "overruns": self.overruns,
            "first_monotonic": self.first_monotonic,
            "timeline": self.timeline,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> AudioTrack:
        """Rebuild a track from its stored form."""
        timeline = raw.get("timeline")
        return AudioTrack(
            file=str(raw.get("file", AUDIO_NAME)),
            clock_file=str(raw.get("clock_file", AUDIO_CLOCK_NAME)),
            rate=int(raw.get("rate", 0)),
            channels=int(raw.get("channels", 0)),
            samples=int(raw.get("samples", 0)),
            filled=int(raw.get("filled", 0)),
            overruns=int(raw.get("overruns", 0)),
            first_monotonic=_optional_float(raw.get("first_monotonic")),
            timeline=timeline if isinstance(timeline, dict) else None,
        )


@dataclass(frozen=True)
class SyncCalibration:
    """The measured offset between the two devices, or the absence of one.

    Both devices timestamp their own measurements, and both are believable to
    about ten milliseconds. What neither says is how much time passes between a
    sound reaching a microphone and the array's converter stamping it, or
    between light reaching the sensor and the camera's shutter timestamp - and
    the two are not the same. That residual is what this holds.

    Attributes:
        offset_s: Seconds to add to an audio time to reach the video time of the
            same physical instant. None means nobody has measured it, and a
            consumer should say "unaligned" rather than assume zero.
        uncertainty_s: How well it is known.
        method: How it was measured, e.g. ``"handclap"``.
        measured_at: Epoch seconds of the measurement.
        note: Anything worth knowing about the measurement.
    """

    offset_s: float | None = None
    uncertainty_s: float | None = None
    method: str | None = None
    measured_at: float | None = None
    note: str | None = None

    @property
    def measured(self) -> bool:
        """Whether an offset is actually known."""
        return self.offset_s is not None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this calibration."""
        return {
            "offset_s": self.offset_s,
            "uncertainty_s": self.uncertainty_s,
            "method": self.method,
            "measured_at": self.measured_at,
            "note": self.note,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> SyncCalibration:
        """Rebuild a calibration from its stored form."""
        return SyncCalibration(
            offset_s=_optional_float(raw.get("offset_s")),
            uncertainty_s=_optional_float(raw.get("uncertainty_s")),
            method=_optional_str(raw.get("method")),
            measured_at=_optional_float(raw.get("measured_at")),
            note=_optional_str(raw.get("note")),
        )


@dataclass
class SessionManifest:
    """Everything needed to read a session directory as one recording.

    Attributes:
        session_id: Directory name.
        format_version: Layout version.
        started_at: Both host clocks, read when recording began. The anchor
            that lets the monotonic axis be named in wall-clock terms.
        stopped_at: The same, read when it ended. None while recording.
        clock_samples: Pairs taken throughout, so that a camera timestamp can
            be converted with the offset that was in force at the time.
        video: What the camera contributed, or None if it was not recorded.
        audio: What the array contributed, or None.
        doa_file: Direction sidecar name, or None.
        calibration: The measured offset between the devices, if any.
        errors: What went wrong during the session. A device that failed does
            not stop the other one - a recording with one track and an
            explanation beats no recording - so failures are written down here
            rather than raised past the recorder.
    """

    session_id: str
    format_version: int = FORMAT_VERSION
    started_at: ClockPair | None = None
    stopped_at: ClockPair | None = None
    clock_samples: list[ClockPair] = field(default_factory=list)
    video: VideoTrack | None = None
    audio: AudioTrack | None = None
    doa_file: str | None = None
    calibration: SyncCalibration = field(default_factory=SyncCalibration)
    errors: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float | None:
        """Seconds between the start and stop anchors, or None while running."""
        if self.started_at is None or self.stopped_at is None:
            return None
        return self.stopped_at.monotonic - self.started_at.monotonic

    @property
    def clock_track(self) -> ClockTrack:
        """The clock samples as a track, for looking up an offset by instant."""
        return ClockTrack.from_list([pair.as_dict() for pair in self.clock_samples])

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this manifest."""
        return {
            "format_version": self.format_version,
            "session_id": self.session_id,
            "clock_reference": "CLOCK_MONOTONIC",
            "started_at": self.started_at.as_dict() if self.started_at else None,
            "stopped_at": self.stopped_at.as_dict() if self.stopped_at else None,
            "duration_s": (
                round(self.duration_s, 3) if self.duration_s is not None else None
            ),
            "clock_samples": [pair.as_dict() for pair in self.clock_samples],
            "video": self.video.as_dict() if self.video else None,
            "audio": self.audio.as_dict() if self.audio else None,
            "doa_file": self.doa_file,
            "calibration": self.calibration.as_dict(),
            "errors": list(self.errors),
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> SessionManifest:
        """Rebuild a manifest from its stored form.

        Args:
            raw: A mapping as :meth:`as_dict` produced.

        Returns:
            The manifest.

        Raises:
            SessionError: If the stored version is newer than this code knows.
        """
        version = int(raw.get("format_version", 0))
        if version > FORMAT_VERSION:
            raise SessionError(
                f"session format {version} is newer than this reader "
                f"understands ({FORMAT_VERSION})"
            )
        video = raw.get("video")
        audio = raw.get("audio")
        calibration = raw.get("calibration")
        samples = raw.get("clock_samples") or []
        return SessionManifest(
            session_id=str(raw["session_id"]),
            format_version=version,
            started_at=_optional_pair(raw.get("started_at")),
            stopped_at=_optional_pair(raw.get("stopped_at")),
            clock_samples=[
                ClockPair.from_dict(entry)
                for entry in samples
                if isinstance(entry, dict)
            ],
            video=VideoTrack.from_dict(video) if isinstance(video, dict) else None,
            audio=AudioTrack.from_dict(audio) if isinstance(audio, dict) else None,
            doa_file=_optional_str(raw.get("doa_file")),
            calibration=(
                SyncCalibration.from_dict(calibration)
                if isinstance(calibration, dict)
                else SyncCalibration()
            ),
            errors=[str(entry) for entry in raw.get("errors") or []],
        )

    def with_calibration(self, calibration: SyncCalibration) -> SessionManifest:
        """Return a copy carrying a different calibration.

        Args:
            calibration: The measurement to record.

        Returns:
            A new manifest. The rest of the session is untouched, which is what
            lets a calibration be measured long after the recording.
        """
        return replace(self, calibration=calibration)


@dataclass(frozen=True)
class SessionPaths:
    """Where each part of a session lives on disk.

    Attributes:
        directory: The session directory.
        session_id: Its name.
    """

    directory: str
    session_id: str

    @staticmethod
    def create(root: str, session_id: str) -> SessionPaths:
        """Make a session directory.

        Args:
            root: Where sessions are kept.
            session_id: Name for this one.

        Returns:
            The paths.

        Raises:
            SessionError: If the id is not a usable directory name, or a
                session of that name already exists. Refusing rather than
                overwriting: a recording is not reproducible, so clobbering one
                is not a recoverable mistake.
        """
        if not ID_PATTERN.match(session_id):
            raise SessionError(f"{session_id!r} is not a usable session id")
        directory = os.path.join(root, session_id)
        if os.path.exists(directory):
            raise SessionError(f"session {session_id!r} already exists")
        os.makedirs(directory)
        return SessionPaths(directory=directory, session_id=session_id)

    @staticmethod
    def resolve(root: str, session_id: str) -> SessionPaths:
        """Turn a session id into paths, refusing anything else.

        Args:
            root: Where sessions are kept.
            session_id: Name as it appears in a listing.

        Returns:
            The paths.

        Raises:
            SessionError: If the id is not a plain session name, or no such
                session exists. This is the only thing standing between an HTTP
                path parameter and the filesystem.
        """
        if not ID_PATTERN.match(session_id):
            raise SessionError(f"{session_id!r} is not a session id")
        directory = os.path.abspath(os.path.join(root, session_id))
        if os.path.dirname(directory) != os.path.abspath(root):
            raise SessionError(f"{session_id!r} is not a session id")
        if not os.path.isdir(directory):
            raise SessionError(f"no session {session_id!r}")
        return SessionPaths(directory=directory, session_id=session_id)

    def _path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    @property
    def manifest(self) -> str:
        """Path to the manifest."""
        return self._path(MANIFEST_NAME)

    @property
    def video(self) -> str:
        """Path to the video archive."""
        return self._path(VIDEO_NAME)

    @property
    def audio(self) -> str:
        """Path to the WAV."""
        return self._path(AUDIO_NAME)

    @property
    def audio_clock(self) -> str:
        """Path to the audio clock sidecar."""
        return self._path(AUDIO_CLOCK_NAME)

    @property
    def doa(self) -> str:
        """Path to the direction sidecar."""
        return self._path(DOA_NAME)

    def size_bytes(self) -> int:
        """Total size of everything in the session directory."""
        total = 0
        for entry in os.scandir(self.directory):
            if entry.is_file():
                total += entry.stat().st_size
        return total


def write_manifest(paths: SessionPaths, manifest: SessionManifest) -> None:
    """Write a manifest into its session directory.

    Args:
        paths: Where the session lives.
        manifest: What to write.

    Written to a temporary file and renamed, so a manifest is never half
    written: the recorder rewrites it while recording so that a session
    interrupted by a crash still describes itself.
    """
    temporary = f"{paths.manifest}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(manifest.as_dict(), handle, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(temporary, paths.manifest)


def read_manifest(paths: SessionPaths) -> SessionManifest:
    """Read the manifest out of a session directory.

    Args:
        paths: Where the session lives.

    Returns:
        The manifest.

    Raises:
        SessionError: If it is missing or unreadable.
    """
    try:
        with open(paths.manifest, encoding="utf-8") as handle:
            return SessionManifest.from_dict(json.load(handle))
    except FileNotFoundError as error:
        raise SessionError(f"{paths.session_id!r} has no manifest") from error
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise SessionError(
            f"{paths.session_id!r} has an unreadable manifest: {error}"
        ) from error


def listing(root: str) -> list[SessionManifest]:
    """Describe the sessions on disk, newest first.

    Args:
        root: Where sessions are kept.

    Returns:
        One manifest per readable session. A directory without a readable
        manifest is skipped rather than raising - a session being recorded right
        now has one, but a crashed one may not.
    """
    if not os.path.isdir(root):
        return []
    found: list[tuple[float, SessionManifest]] = []
    for name in os.listdir(root):
        if not ID_PATTERN.match(name):
            continue
        try:
            paths = SessionPaths.resolve(root, name)
            manifest = read_manifest(paths)
        except SessionError:
            continue
        anchor = manifest.started_at.realtime if manifest.started_at else 0.0
        found.append((anchor, manifest))
    return [manifest for _, manifest in sorted(found, key=lambda item: -item[0])]


def _optional_float(value: object) -> float | None:
    """Read a float that is allowed to be absent."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _optional_str(value: object) -> str | None:
    """Read a string that is allowed to be absent."""
    return None if value is None else str(value)


def _optional_pair(value: object) -> ClockPair | None:
    """Read a clock pair that is allowed to be absent."""
    return ClockPair.from_dict(value) if isinstance(value, dict) else None  # type: ignore[arg-type]
