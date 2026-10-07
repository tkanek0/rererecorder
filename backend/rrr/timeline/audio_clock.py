"""When each audio sample in the file was actually captured.

Measured ``(file sample, ADC time)`` points written beside the WAV, so sample
to time is fitted rather than assumed from the nominal rate, and gaps the
recorder filled with silence are recorded. See docs/features.md "The array".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .jsonl import read_jsonl


@dataclass(frozen=True)
class AudioClockPoint:
    """When one block of audio was captured, and where it landed in the file.

    Attributes:
        sample: Absolute index, in the file, of the block's first sample,
            counting inserted silence.
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


@dataclass(frozen=True)
class TimelineReport:
    """What the measured points say about a recording's time axis.

    Attributes:
        points: How many measurements the sidecar holds.
        span_s: Seconds between the first and last measurement.
        nominal_rate: The rate the file's header claims.
        measured_rate: Rate fitted to the measurements, in Hz.
        rate_error_ppm: How far the measured rate is from the nominal one.
            Tens of ppm is an ordinary crystal.
        residual_rms_ms: Scatter of the measurements about the fitted line,
            i.e. the ADC timestamps' own jitter.
        residual_max_ms: Worst single departure from the fitted line. Far above
            the RMS means a discontinuity, such as an unfilled drop.
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

    Fitted to the measured points; with fewer than two, the nominal rate is
    used and the report's measured fields are None.
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
        self._samples = np.array(
            [point.sample for point in self._points], dtype=np.float64
        )
        self._times = np.array(
            [point.monotonic for point in self._points], dtype=np.float64
        )

        if len(self._points) >= 2:
            # Seconds per sample and the intercept, by least squares.
            slope, intercept = np.polyfit(self._samples, self._times, 1)
            self._slope = float(slope)
            self._intercept = float(intercept)
        else:
            self._slope = 1.0 / rate
            self._intercept = self._times[0] - self._samples[0] / rate

    @staticmethod
    def read(path: str, rate: int) -> AudioTimeline:
        """Load a timeline from its sidecar.

        Args:
            path: The sidecar to read.
            rate: Nominal sample rate from the audio file's header.

        Returns:
            The timeline.

        Raises:
            ValueError: If the sidecar holds no usable points.
        """
        return AudioTimeline(read_jsonl(path, AudioClockPoint.from_dict), rate)

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
            sample: Sample index in the file. May be fractional or outside
                the measured range, which extrapolates.

        Returns:
            The monotonic time of that sample.
        """
        return self._intercept + self._slope * sample

    def sample_at(self, monotonic: float) -> float:
        """Which sample position in the file corresponds to an instant.

        Args:
            monotonic: A ``CLOCK_MONOTONIC`` time.

        Returns:
            The sample index, fractional; outside the file if the instant is
            outside the recording.
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
            The report.
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
