"""The manifest that makes a recorded session self-describing.

The directory layout and what the manifest refuses to claim are in
docs/design.md "A session is a directory" and "What it refuses to claim".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace

from .audio_clock import SUFFIX as AUDIO_CLOCK_SUFFIX
from .clock import ClockPair, ClockTrack
from .events import SUFFIX as EVENTS_SUFFIX

#: Bumped when the layout changes in a way a reader must know about.
FORMAT_VERSION = 1

#: File names inside a session directory, fixed so it reads without the manifest.
MANIFEST_NAME = "session.json"
VIDEO_NAME = "video.rrdb"
AUDIO_NAME = "audio.wav"
AUDIO_CLOCK_NAME = f"audio{AUDIO_CLOCK_SUFFIX}"
DOA_NAME = "doa.jsonl"
EVENTS_NAME = f"events{EVENTS_SUFFIX}"

#: Default names of derived copies, kept inside the session so they are deleted
#: with it; nothing reads them back.
EXPORT_NAME = "export"
REVIEW_NAME = "review.mp4"

#: Session directory names accepted. An id arrives from an HTTP path, so it is
#: rejected rather than sanitised; no dots means it cannot be a relative path.
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SessionError(Exception):
    """Raised for a session that cannot be named, found or read."""


@dataclass(frozen=True)
class VideoTrack:
    """What was recorded from the camera.

    Attributes:
        file: Archive name inside the session directory.
        frames: Frames written.
        dropped: Frames the encoder queue could not accept.
        skipped_warmup: Sets discarded before the first one was written, while
            the SDK's syncer settled. Not a loss.
        skipped_duplicate: Sets discarded mid-stream because every frame in them
            had already been delivered.
        first_monotonic: Capture time of the first frame written, on the
            monotonic axis, or None if nothing was written.
        last_monotonic: Capture time of the last frame written.
        motion: Inertial samples written, both streams at their own rates.
            Zero means inertial recording was off or failed.
        motion_overrun: Samples discarded because the writer did not drain the
            source's buffer in time. Should be zero.
        timestamp_domain: ``frame.get_frame_timestamp_domain()``, normally
            ``global_time``. See docs/features.md "Timing".
        fps: Frames written divided by the span they cover.
    """

    file: str = VIDEO_NAME
    frames: int = 0
    dropped: int = 0
    motion: int = 0
    motion_overrun: int = 0
    skipped_warmup: int = 0
    skipped_duplicate: int = 0
    first_monotonic: float | None = None
    last_monotonic: float | None = None
    timestamp_domain: str = "unknown"
    fps: float | None = None

    @property
    def skipped(self) -> int:
        """Sets discarded mid-stream. Excludes startup."""
        return self.skipped_duplicate

    @property
    def motion_hz(self) -> float | None:
        """Inertial samples per second across the recording, both streams.

        Returns:
            The rate, or None if there is nothing to divide. Approximate,
            since the span is the video's; ``ArchiveSource.motion_rate`` gives
            the measured one.
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
            "first_monotonic": self.first_monotonic,
            "last_monotonic": self.last_monotonic,
            "timestamp_domain": self.timestamp_domain,
            "fps": round(self.fps, 3) if self.fps is not None else None,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> VideoTrack:
        """Rebuild a track from its stored form.

        Legacy ``skipped_unpaired`` and combined ``skipped`` are ignored
        (docs/decisions.md 21).
        """
        return VideoTrack(
            file=str(raw.get("file", VIDEO_NAME)),
            frames=int(raw.get("frames", 0)),
            dropped=int(raw.get("dropped", 0)),
            motion=int(raw.get("motion", 0)),
            motion_overrun=int(raw.get("motion_overrun", 0)),
            skipped_warmup=int(raw.get("skipped_warmup", 0)),
            skipped_duplicate=int(raw.get("skipped_duplicate", 0)),
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
        channels: Channels written; always all of them.
        samples: Frames written to the WAV, counting inserted silence.
        filled: Samples of silence inserted to replace dropped audio.
        overruns: How often the driver reported an input overflow.
        first_monotonic: Capture time of sample zero, on the monotonic axis.
        timeline: What the measured points say about the time axis, as
            :meth:`~rrr.timeline.audio_clock.AudioTimeline.report` produced it.
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

    The residual between a microphone and a shutter that neither device
    reports. See docs/design.md "What it refuses to claim".

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


@dataclass(frozen=True)
class Rig:
    """How the two devices are mounted, and how well that is known.

    Every field starts empty and is filled in by hand; never defaulted to
    identity. See docs/design.md "What it refuses to claim".

    Attributes:
        source: ``"unset"`` until somebody fills it in, then ``"nominal"`` for
            design or datasheet values and ``"measured"`` for values obtained
            from this hardware.
        rotation: Row-major 3x3 rotation of ``depth_from_array``: applied to a
            direction in the array frame, it gives that direction in the depth
            stream's frame, which every other transform in a recording is
            expressed against (the left infrared imager on a D400).
        translation: Position of the array's origin in the depth stream's
            frame, in metres.
        microphones: Position of each microphone in the array frame, in metres,
            in the order :attr:`channels` names.
        channels: Which channel of ``audio.wav`` each microphone in
            :attr:`microphones` is; not simply ``0..n``.
        description: How the mount is arranged, in words, for whoever reads the
            session later.
        note: Anything worth knowing about where these numbers came from.
    """

    source: str = "unset"
    rotation: tuple[float, ...] | None = None
    translation: tuple[float, ...] | None = None
    microphones: tuple[tuple[float, float, float], ...] | None = None
    channels: tuple[int, ...] | None = None
    description: str | None = None
    note: str | None = None

    @property
    def known(self) -> bool:
        """Whether the array can actually be placed against the camera.

        Returns:
            True only when a placement is present; otherwise a consumer must
            not assume the devices share an origin.
        """
        return self.rotation is not None and self.translation is not None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this rig."""
        return {
            "source": self.source,
            "rotation": list(self.rotation) if self.rotation else None,
            "translation": list(self.translation) if self.translation else None,
            "microphones": (
                [list(position) for position in self.microphones]
                if self.microphones
                else None
            ),
            "channels": list(self.channels) if self.channels else None,
            "description": self.description,
            "note": self.note,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> Rig:
        """Rebuild a rig from its stored form.

        Args:
            raw: A mapping as :meth:`as_dict` produced, usually hand-edited;
                a field absent, null or the wrong shape is left unset rather
                than raising.

        Returns:
            The rig.
        """
        return Rig(
            source=str(raw.get("source") or "unset"),
            rotation=_optional_floats(raw.get("rotation"), 9),
            translation=_optional_floats(raw.get("translation"), 3),
            microphones=_optional_points(raw.get("microphones")),
            channels=_optional_ints(raw.get("channels")),
            description=_optional_str(raw.get("description")),
            note=_optional_str(raw.get("note")),
        )


@dataclass
class SessionManifest:
    """Everything needed to read a session directory as one recording.

    Attributes:
        session_id: Directory name.
        format_version: Layout version.
        started_at: Both host clocks, read when recording began.
        stopped_at: The same, read when it ended. None while recording.
        clock_samples: Pairs taken throughout the recording.
        video: What the camera contributed, or None if it was not recorded.
        audio: What the array contributed, or None.
        doa_file: Direction sidecar name, or None.
        events_file: Mark sidecar name, or None if nobody marked anything.
            No count is kept, so it cannot disagree with the file.
        calibration: The measured offset between the devices, if any.
        rig: How the two devices are mounted relative to each other.
        errors: What went wrong; a failed device does not stop the other.
    """

    session_id: str
    format_version: int = FORMAT_VERSION
    started_at: ClockPair | None = None
    stopped_at: ClockPair | None = None
    clock_samples: list[ClockPair] = field(default_factory=list)
    video: VideoTrack | None = None
    audio: AudioTrack | None = None
    doa_file: str | None = None
    events_file: str | None = None
    calibration: SyncCalibration = field(default_factory=SyncCalibration)
    rig: Rig = field(default_factory=Rig)
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
            "events_file": self.events_file,
            "calibration": self.calibration.as_dict(),
            "rig": self.rig.as_dict(),
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
        rig = raw.get("rig")
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
            events_file=_optional_str(raw.get("events_file")),
            calibration=(
                SyncCalibration.from_dict(calibration)
                if isinstance(calibration, dict)
                else SyncCalibration()
            ),
            rig=Rig.from_dict(rig) if isinstance(rig, dict) else Rig(),
            errors=[str(entry) for entry in raw.get("errors") or []],
        )

    def with_calibration(self, calibration: SyncCalibration) -> SessionManifest:
        """Return a copy carrying a different calibration.

        Args:
            calibration: The measurement to record.

        Returns:
            A new manifest; the rest of the session is untouched.
        """
        return replace(self, calibration=calibration)

    def with_rig(self, rig: Rig) -> SessionManifest:
        """Return a copy carrying a different rig.

        Args:
            rig: The mounting to record.

        Returns:
            A new manifest; the rest of the session is untouched.
        """
        return replace(self, rig=rig)


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
                session of that name already exists (never overwritten).
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
                session exists. This guards the filesystem from HTTP paths.
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

    @property
    def events(self) -> str:
        """Path to the mark sidecar."""
        return self._path(EVENTS_NAME)

    @property
    def export(self) -> str:
        """Default directory for the neutral export."""
        return self._path(EXPORT_NAME)

    @property
    def review(self) -> str:
        """Default path for the review movie."""
        return self._path(REVIEW_NAME)

    def size_bytes(self) -> int:
        """Total size of everything in the session directory.

        Recursive, so it includes an export and a review movie.
        """
        total = 0
        for root, _, files in os.walk(self.directory):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except FileNotFoundError:
                    # An export renaming its temporary directory into place.
                    pass
        return total


def write_manifest(paths: SessionPaths, manifest: SessionManifest) -> None:
    """Write a manifest into its session directory.

    Args:
        paths: Where the session lives.
        manifest: What to write.

    Written to a temporary file and renamed, so a manifest the recorder
    rewrites while recording is never half written.
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
        One manifest per readable session; a directory without one is
        skipped.
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


def _optional_floats(value: object, count: int) -> tuple[float, ...] | None:
    """Read a fixed-length sequence of numbers that is allowed to be absent.

    Args:
        value: The stored value, typically straight from a hand-edited file.
        count: How many numbers the field is supposed to hold.

    Returns:
        The numbers, or None if the field is absent, the wrong length, or holds
        anything that is not a number.
    """
    if not isinstance(value, (list, tuple)) or len(value) != count:
        return None
    try:
        return tuple(float(entry) for entry in value)
    except (TypeError, ValueError):
        return None


def _optional_points(value: object) -> tuple[tuple[float, float, float], ...] | None:
    """Read a list of three-dimensional points that is allowed to be absent.

    Args:
        value: The stored value.

    Returns:
        The points, or None (all or nothing) if any is not three numbers.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    points = []
    for entry in value:
        point = _optional_floats(entry, 3)
        if point is None:
            return None
        points.append((point[0], point[1], point[2]))
    return tuple(points)


def _optional_ints(value: object) -> tuple[int, ...] | None:
    """Read a sequence of channel indices that is allowed to be absent."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        return tuple(int(entry) for entry in value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    """Read a string that is allowed to be absent."""
    return None if value is None else str(value)


def _optional_pair(value: object) -> ClockPair | None:
    """Read a clock pair that is allowed to be absent."""
    return ClockPair.from_dict(value) if isinstance(value, dict) else None  # type: ignore[arg-type]
