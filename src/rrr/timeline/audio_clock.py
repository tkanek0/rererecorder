"""When each audio sample in the file was actually captured.

A WAV file has no timestamps. It has a sample rate, which invites the assumption
that sample *n* was captured at ``start + n / rate`` - and that assumption fails
in two ways that both matter here:

* **The nominal rate is not the real one.** The array clocks its own converter,
  and a USB audio device's crystal is not the host's. An error of 100 ppm - well
  within what these devices exhibit - is 0.36 s over an hour, which is a lip-sync
  failure against video that has its own, correct, clock.
* **Samples go missing.** An input overflow drops audio that never reaches the
  file, so every sample after it sits earlier in the file than it was captured.
  Unrecorded, the timeline silently contracts.

This module keeps a measured point per capture block - the absolute position of
the block's first sample *in the file*, and the ADC time of that sample - and
writes them beside the audio. That turns "sample to time" from an assumption into
a measurement, and makes the two failures above visible: the first as a fitted
rate that differs from the nominal one, the second as a residual that steps.

The recorder is responsible for keeping file position and capture continuous by
filling a detected gap with silence. This module is what checks that it did.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import numpy as np

#: Suffix for the sidecar this module writes.
SUFFIX = ".clock.jsonl"


@dataclass(frozen=True)
class AudioClockPoint:
    """When one block of audio was captured, and where it landed in the file.

    Attributes:
        sample: Absolute index, in the file, of the block's first sample. Counts
            silence inserted to fill a gap, because the point of the index is to
            address the file.
        monotonic: ``CLOCK_MONOTONIC`` time at which that sample entered the
            converter, from PortAudio's ``inputBufferAdcTime``.
        filled: Samples of silence inserted immediately before this block to
            replace audio that was dropped. Zero for an uninterrupted block.
    """

    sample: int
    monotonic: float
    filled: int = 0

    def as_dict(self) -> dict[str, float | int]:
        """Return a JSON-serialisable view, omitting the common zero case."""
        entry: dict[str, float | int] = {"sample": self.sample, "t": self.monotonic}
        if self.filled:
            entry["filled"] = self.filled
        return entry

    @staticmethod
    def from_dict(raw: dict[str, float | int]) -> AudioClockPoint:
        """Rebuild a point from its stored form.

        Args:
            raw: A mapping as :meth:`as_dict` produced.

        Returns:
            The point.
        """
        return AudioClockPoint(
            sample=int(raw["sample"]),
            monotonic=float(raw["t"]),
            filled=int(raw.get("filled", 0)),
        )


class AudioClockWriter:
    """Appends clock points beside a recording, one JSON object per line.

    JSON lines rather than a column in the manifest: at 16 kHz with 1024-sample
    blocks this is 15.6 entries a second, so an hour is 56k of them. That is
    small next to the audio and far too large to sit inside a file meant to be
    read at a glance.
    """

    def __init__(self, path: str) -> None:
        """Open the sidecar for writing.

        Args:
            path: File to create. Overwritten if it exists.
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._handle = open(path, "w", encoding="utf-8")
        self._path = path
        self._count = 0

    def __enter__(self) -> AudioClockWriter:
        """Return the open writer."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the sidecar."""
        self.close()

    @property
    def path(self) -> str:
        """Where the sidecar is being written."""
        return self._path

    @property
    def count(self) -> int:
        """How many points have been written."""
        return self._count

    def append(self, point: AudioClockPoint) -> None:
        """Write one point.

        Args:
            point: The measurement to record.
        """
        self._handle.write(json.dumps(point.as_dict()) + "\n")
        self._count += 1

    def close(self) -> None:
        """Flush and close."""
        if not self._handle.closed:
            self._handle.close()


@dataclass(frozen=True)
class TimelineReport:
    """What the measured points say about a recording's time axis.

    Attributes:
        points: How many measurements the sidecar holds.
        span_s: Seconds between the first and last measurement.
        nominal_rate: The rate the file's header claims.
        measured_rate: Rate fitted to the measurements, in Hz. What the
            converter actually ran at, as the host observed it.
        rate_error_ppm: How far the measured rate is from the nominal one.
            Tens of ppm is an ordinary crystal; hundreds means a recording
            longer than a few minutes will visibly drift against the video.
        residual_rms_ms: Scatter of the measurements about the fitted line.
            This is the jitter of the ADC timestamps themselves; measured at
            0.35 ms on a ReSpeaker.
        residual_max_ms: Worst single departure from the fitted line. Much
            larger than the RMS means something discontinuous happened - a
            dropped block that was not filled, or the process being starved.
        filled: Samples of silence inserted to replace dropped audio.
    """

    points: int
    span_s: float
    nominal_rate: int
    measured_rate: float | None
    rate_error_ppm: float | None
    residual_rms_ms: float | None
    residual_max_ms: float | None
    filled: int

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable view of this report."""
        return {
            "points": self.points,
            "span_s": round(self.span_s, 3),
            "nominal_rate": self.nominal_rate,
            "measured_rate": (
                round(self.measured_rate, 3) if self.measured_rate is not None else None
            ),
            "rate_error_ppm": (
                round(self.rate_error_ppm, 1)
                if self.rate_error_ppm is not None
                else None
            ),
            "residual_rms_ms": (
                round(self.residual_rms_ms, 4)
                if self.residual_rms_ms is not None
                else None
            ),
            "residual_max_ms": (
                round(self.residual_max_ms, 4)
                if self.residual_max_ms is not None
                else None
            ),
            "filled": self.filled,
        }


class AudioTimeline:
    """Maps between a file's sample positions and the monotonic clock.

    Built from the measured points rather than from the nominal rate, so the
    mapping is what was observed. With fewer than two points there is nothing to
    fit and the nominal rate is used, which is said plainly in the report rather
    than hidden.
    """

    def __init__(self, points: list[AudioClockPoint], rate: int) -> None:
        """Build a timeline from measurements.

        Args:
            points: Measured points, in file order.
            rate: Nominal sample rate from the file's header.

        Raises:
            ValueError: If no points were given, or the rate is not positive.
        """
        if not points:
            raise ValueError("a timeline needs at least one measured point")
        if rate <= 0:
            raise ValueError(f"{rate} is not a usable sample rate")

        self._points = sorted(points, key=lambda point: point.sample)
        self._rate = rate
        self._samples = np.array([point.sample for point in self._points], dtype=np.float64)
        self._times = np.array([point.monotonic for point in self._points], dtype=np.float64)

        if len(self._points) >= 2:
            # Seconds per sample and the intercept, in one least-squares fit.
            # polyfit rather than a hand-rolled normal equation so that the
            # conditioning is somebody else's problem.
            slope, intercept = np.polyfit(self._samples, self._times, 1)
            self._slope = float(slope)
            self._intercept = float(intercept)
        else:
            self._slope = 1.0 / rate
            self._intercept = self._times[0] - self._samples[0] / rate

    @staticmethod
    def read(path: str, rate: int) -> AudioTimeline:
        """Load a timeline from a sidecar written by :class:`AudioClockWriter`.

        Args:
            path: The sidecar to read.
            rate: Nominal sample rate from the audio file's header.

        Returns:
            The timeline.

        Raises:
            ValueError: If the sidecar holds no usable points.
        """
        points = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    points.append(AudioClockPoint.from_dict(json.loads(line)))
        return AudioTimeline(points, rate)

    @property
    def points(self) -> list[AudioClockPoint]:
        """The measurements this timeline was built from, in file order."""
        return list(self._points)

    @property
    def measured_rate(self) -> float | None:
        """Fitted sample rate in Hz, or None if only one point was measured."""
        if len(self._points) < 2 or self._slope <= 0:
            return None
        return 1.0 / self._slope

    def monotonic_at(self, sample: float) -> float:
        """When a sample position in the file was captured.

        Args:
            sample: Sample index in the file. May be fractional, and may sit
                outside the measured range - the fitted line extrapolates,
                which is what a query about the last partial block needs.

        Returns:
            The monotonic time of that sample.
        """
        return self._intercept + self._slope * sample

    def sample_at(self, monotonic: float) -> float:
        """Which sample position in the file corresponds to an instant.

        Args:
            monotonic: A ``CLOCK_MONOTONIC`` time.

        Returns:
            The sample index, fractional. Negative or past the end of the file
            if the instant falls outside the recording; the caller decides
            whether that is an error.
        """
        return (monotonic - self._intercept) / self._slope

    def residuals_ms(self) -> np.ndarray:
        """How far each measurement sits from the fitted line, in milliseconds.

        Returns:
            One value per measured point, in file order.
        """
        fitted = self._intercept + self._slope * self._samples
        return (self._times - fitted) * 1000.0

    def report(self) -> TimelineReport:
        """Summarise what the measurements say about this recording.

        Returns:
            The report. Read the rate error and the maximum residual: those are
            the two numbers that say whether audio can be trusted against video.
        """
        measured = self.measured_rate
        residuals = self.residuals_ms() if len(self._points) >= 2 else None
        return TimelineReport(
            points=len(self._points),
            span_s=float(self._times[-1] - self._times[0]),
            nominal_rate=self._rate,
            measured_rate=measured,
            rate_error_ppm=(
                (measured - self._rate) / self._rate * 1e6
                if measured is not None
                else None
            ),
            residual_rms_ms=(
                float(np.sqrt(np.mean(np.square(residuals))))
                if residuals is not None
                else None
            ),
            residual_max_ms=(
                float(np.max(np.abs(residuals))) if residuals is not None else None
            ),
            filled=sum(point.filled for point in self._points),
        )
