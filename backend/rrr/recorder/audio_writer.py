"""Writing the array to a WAV whose sample positions still mean times.

Holes are filled with exactly as much silence as was lost, and measured clock
points are written beside the audio so ``rrr.timeline.AudioTimeline`` can check
the repair. See docs/features.md "The array".
"""

from __future__ import annotations

import logging
import os
import threading
import wave
from dataclasses import dataclass

import numpy as np
from respeaker_adapter import BlockStamp

from rrr.devices import AudioTap, DoaTap, Reading
from rrr.timeline import AudioClockPoint, JsonlWriter

logger = logging.getLogger(__name__)

#: How far capture time may run past what the samples account for before it is
#: called a gap, in blocks. Far above the measured 0.35 ms ADC jitter, and below
#: the smallest real gap: a driver drops whole callbacks, so at least one block.
GAP_THRESHOLD_BLOCKS = 0.5

#: Seconds between clock points in the ordinary case. Every gap gets a point
#: regardless, and between gaps the axis is a straight line.
CLOCK_POINT_INTERVAL_S = 1.0

#: How long to wait for audio before checking whether recording should stop.
READ_TIMEOUT_S = 0.5


@dataclass
class AudioStats:
    """What the audio writer has done so far.

    Attributes:
        rate: Sample rate, so that ``seconds`` needs nothing else.
        samples: Frames written to the WAV, counting inserted silence.
        filled: Samples of silence inserted to replace audio that was lost.
        gaps: How many separate holes were filled.
        dropped_by_reader: Samples the ring buffer overwrote before this writer
            collected them, as the tap reported it.
        overruns: Input overflows the driver reported.
        clock_points: Measured points written to the sidecar.
        first_monotonic: ADC time of sample zero, or None before anything is
            written.
        error: What went wrong, if anything.
    """

    rate: int = 0
    samples: int = 0
    filled: int = 0
    gaps: int = 0
    dropped_by_reader: int = 0
    overruns: int = 0
    clock_points: int = 0
    first_monotonic: float | None = None
    error: str | None = None

    @property
    def seconds(self) -> float:
        """Length of what has been written, by the file's own reckoning."""
        return self.samples / self.rate if self.rate else 0.0


class AudioWriter:
    """Writes the array to a WAV, a clock sidecar and a direction sidecar.

    Runs on its own thread, and always writes every channel.
    """

    def __init__(
        self,
        tap: AudioTap,
        *,
        wav_path: str,
        clock_path: str,
        doa: DoaTap | None = None,
        doa_path: str | None = None,
        clock_interval_s: float = CLOCK_POINT_INTERVAL_S,
    ) -> None:
        """Bind a writer to its tap and its output files.

        Args:
            tap: Where the audio comes from.
            wav_path: WAV to write.
            clock_path: Sidecar of measured capture times.
            doa: Where the direction comes from, or None to skip it.
            doa_path: Sidecar for the direction, required if ``doa`` is given.
            clock_interval_s: Seconds between clock points in the ordinary case.

        Raises:
            ValueError: If a direction tap was given without a path for it.
        """
        if doa is not None and doa_path is None:
            raise ValueError("recording the direction needs a path to write it to")

        self._tap = tap
        self._wav_path = wav_path
        self._clock_path = clock_path
        self._doa = doa
        self._doa_path = doa_path
        self._clock_interval_s = clock_interval_s

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = AudioStats()
        #: Direction readings received since the writer last wrote them.
        self._readings: list[Reading] = []

    # -- control -----------------------------------------------------------

    def start(self) -> None:
        """Begin writing, on a thread of its own.

        Raises:
            RuntimeError: If this writer is already running.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("this audio writer is already running")
            self._stats = AudioStats(rate=self._tap.rate)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="audio-writer", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> AudioStats:
        """Finish writing and wait for the files to be closed.

        Args:
            timeout: Seconds to wait for the writer to finish.

        Returns:
            The final statistics.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.stats

    @property
    def stats(self) -> AudioStats:
        """A snapshot of what has been written."""
        with self._lock:
            return AudioStats(**vars(self._stats))

    # -- writer thread -----------------------------------------------------

    def _run(self) -> None:
        """Open everything, write until told to stop, and close in order."""
        self._tap.acquire()
        if self._doa is not None:
            # Every reading, not the newest at each pass: none is lost.
            self._doa.add_listener(self._on_reading)
            self._doa.acquire()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._wav_path)), exist_ok=True)
            with (
                wave.open(self._wav_path, "wb") as out,
                JsonlWriter(self._clock_path) as clock,
            ):
                out.setnchannels(self._tap.channels)
                out.setsampwidth(2)
                out.setframerate(self._tap.rate)
                directions = (
                    JsonlWriter(self._doa_path)
                    if self._doa is not None and self._doa_path is not None
                    else None
                )
                try:
                    self._pump(out, clock, directions)
                finally:
                    if directions is not None:
                        directions.close()
        except Exception as error:  # noqa: BLE001 - reported through stats
            logger.exception("audio recording failed")
            with self._lock:
                self._stats.error = str(error)
        finally:
            self._tap.release()
            if self._doa is not None:
                self._doa.remove_listener(self._on_reading)
                self._doa.release()
            logger.info(
                "audio recording stopped: %s, %d samples, %d filled",
                self._wav_path,
                self._stats.samples,
                self._stats.filled,
            )

    def _pump(
        self, out: wave.Wave_write, clock: JsonlWriter, directions: JsonlWriter | None
    ) -> None:
        """Read the tap and write it out until asked to stop.

        Args:
            out: The open WAV.
            clock: The open clock sidecar.
            directions: Open direction sidecar, or None.
        """
        rate = self._tap.rate
        gap_threshold_s = GAP_THRESHOLD_BLOCKS * self._tap.block_size / rate
        cursor = self._tap.cursor
        previous: BlockStamp | None = None
        last_point_at = 0.0

        while not self._stop.is_set():
            chunk = self._tap.stream(cursor, timeout=READ_TIMEOUT_S)
            if chunk is None:
                if self._tap.error:
                    raise RuntimeError(self._tap.error)
                continue
            cursor = chunk.cursor

            with self._lock:
                self._stats.dropped_by_reader += chunk.dropped
                self._stats.overruns = self._tap.overruns

            for stamp in chunk.stamps:
                filled = self._fill_for(stamp, previous, rate, gap_threshold_s)
                if filled:
                    out.writeframes(
                        np.zeros((filled, self._tap.channels), dtype="<i2").tobytes()
                    )
                    with self._lock:
                        self._stats.samples += filled
                        self._stats.filled += filled
                        self._stats.gaps += 1
                    logger.warning(
                        "filled a %.1f ms hole in the audio at %.3f s",
                        filled / rate * 1000.0,
                        self._stats.samples / rate,
                    )

                block = self._samples_for(stamp, chunk)
                if block is None:
                    # Overwritten before it was read; already counted as a fill.
                    previous = stamp
                    continue

                # Before the samples, so the position it names is where they land.
                if (
                    filled
                    or previous is None
                    or stamp.monotonic - last_point_at >= self._clock_interval_s
                ):
                    clock.append(
                        AudioClockPoint(
                            sample=self._stats.samples,
                            monotonic=stamp.monotonic,
                            filled=filled,
                        ).as_dict()
                    )
                    last_point_at = stamp.monotonic
                    with self._lock:
                        self._stats.clock_points = clock.count

                out.writeframes(_to_int16(block).tobytes())
                with self._lock:
                    if self._stats.first_monotonic is None:
                        self._stats.first_monotonic = stamp.monotonic
                    self._stats.samples += len(block)
                previous = stamp

            if directions is not None:
                self._write_readings(directions)

        if directions is not None:
            self._write_readings(directions)
        # Always close with a point at the end of the file. end_monotonic, not
        # monotonic: the position is one block past the last stamp's start, and
        # pairing it with the start would skew the fit by a block.
        if previous is not None:
            clock.append(
                AudioClockPoint(
                    sample=self._stats.samples,
                    monotonic=previous.end_monotonic(rate),
                ).as_dict()
            )
            with self._lock:
                self._stats.clock_points = clock.count

    def _on_reading(self, reading: Reading) -> None:
        """Keep a direction reading for the writer thread. Runs on the poller's."""
        with self._lock:
            self._readings.append(reading)

    def _write_readings(self, sidecar: JsonlWriter) -> None:
        """Append the readings received since the last call to the sidecar."""
        with self._lock:
            readings, self._readings = self._readings, []
        for reading in readings:
            sidecar.append(
                {
                    "t": reading.captured_at,
                    "angle": reading.angle,
                    "voice": reading.voice_activity,
                }
            )

    @staticmethod
    def _fill_for(
        stamp: BlockStamp,
        previous: BlockStamp | None,
        rate: int,
        gap_threshold_s: float,
    ) -> int:
        """How much silence must precede this block to keep the axis honest.

        Args:
            stamp: The block about to be written.
            previous: The block written before it, or None for the first.
            rate: Sample rate in Hz.
            gap_threshold_s: How far capture time may run past the samples that
                account for it before it is called a gap.

        Returns:
            Samples of silence to insert. Zero in the ordinary case.

        Ring overwrites (exact, from the sample count) and driver drops
        (inferred from the clock) overlap: the clock lag is measured after the
        overwritten samples' own duration, or the hole would be filled twice.
        """
        if previous is None:
            return 0

        overwritten = max(0, stamp.sample - previous.end_sample)
        expected_at = previous.end_monotonic(rate) + overwritten / rate
        lag = stamp.monotonic - expected_at
        undelivered = round(lag * rate) if lag > gap_threshold_s else 0
        return overwritten + undelivered

    @staticmethod
    def _samples_for(stamp: BlockStamp, chunk) -> np.ndarray | None:
        """The part of a chunk belonging to one block.

        Args:
            stamp: The block wanted.
            chunk: The chunk it was delivered in.

        Returns:
            ``(n, channels)`` samples, or None if the ring wrapped past it.
        """
        start = max(stamp.sample, chunk.first_sample) - chunk.first_sample
        end = min(stamp.end_sample, chunk.cursor) - chunk.first_sample
        if end <= start:
            return None
        return chunk.samples[start:end]


def _to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float32 in [-1, 1] back to the int16 the device sent."""
    return np.clip(samples * 32768.0, -32768, 32767).astype("<i2")
