"""Writing the array to a WAV whose sample positions still mean times.

A WAV is a bare sequence of samples. The only thing that turns position 48000
into "one second in" is the assumption that every sample the converter produced
is present, and that assumption breaks in two different ways:

* **The driver drops input.** The callback is simply not called for what was
  lost, so the tap never sees those samples. Capture time jumps; the sample
  count does not.
* **The reader falls behind.** The tap captured the audio and the ring buffer
  overwrote it before this writer collected it. The sample count jumps; capture
  time was continuous all along.

The two are opposite in shape and identical in consequence: without repair,
every sample after the hole sits earlier in the file than it was captured, and
nothing in the file says so. The upstream respeaker-playground counts the second
case and writes neither a marker nor the missing samples, which silently
contracts the recording's time axis.

This writer keeps the axis by filling each hole with exactly as much silence as
was lost, and by writing a measured point beside the audio so the repair can be
checked rather than trusted. ``timeline.AudioTimeline`` is what checks it.

Both holes are measured against the block stamps, which is why the two cases can
be told apart at all - and why the arithmetic below subtracts one from the other
rather than adding them: a slow reader loses samples *and* the time they
occupied, so counting both would fill the hole twice.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np

from rrr.audio import AudioTap, BlockStamp, DoaTap
from rrr.timeline import AudioClockPoint, AudioClockWriter

logger = logging.getLogger(__name__)

#: How far capture time may run past what the samples account for before it is
#: called a gap, in blocks.
#:
#: Measured ADC jitter on a ReSpeaker is 0.35 ms, and a block at the default
#: 256 frames is 16 ms. Half a block therefore sits 23 times the jitter away from
#: zero and half a block below the smallest real gap - a driver that drops input
#: skips whole callbacks, so it cannot lose less than one block.
GAP_THRESHOLD_BLOCKS = 0.5

#: Seconds between clock points in the ordinary case.
#:
#: Every block would be 62.5 points a second at the default block size, or 225k
#: an hour. One a second is enough to fit a rate and to see a step, because a
#: gap is written whether or not the interval has elapsed - the points exist to
#: describe the axis, and between gaps the axis is a straight line.
CLOCK_POINT_INTERVAL_S = 1.0

#: How long to wait for audio before checking whether recording should stop.
READ_TIMEOUT_S = 0.5


@dataclass
class AudioStats:
    """What the audio writer has done so far.

    Attributes:
        rate: Sample rate, so that ``seconds`` needs nothing else.
        samples: Frames written to the WAV, counting inserted silence. This is
            what the WAV header will say, and what a file position is measured
            against.
        filled: Samples of silence inserted to replace audio that was lost.
        gaps: How many separate holes were filled.
        dropped_by_reader: Samples the ring buffer overwrote before this writer
            collected them, as the tap reported it.
        overruns: Input overflows the driver reported.
        clock_points: Measured points written to the sidecar.
        first_monotonic: ADC time of sample zero, or None before anything is
            written. The anchor that places this recording against the video.
        last_monotonic: ADC time of the newest sample written.
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
    last_monotonic: float | None = None
    error: str | None = None

    @property
    def seconds(self) -> float:
        """Length of what has been written, by the file's own reckoning."""
        return self.samples / self.rate if self.rate else 0.0


class AudioWriter:
    """Writes the array to a WAV, a clock sidecar and a direction sidecar.

    Runs on its own thread. Whatever asked for the recording - an HTTP request,
    a CLI loop - is not the thing that has to keep up with the device.

    All six channels are written whatever anything happens to be listening to.
    The raw microphones are the part worth keeping: they are what a direction
    estimator needs, and they cannot be recovered from the processed channel.
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
    def running(self) -> bool:
        """Whether the writer thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

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
            self._doa.acquire()
        directions = None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._wav_path)), exist_ok=True)
            with (
                wave.open(self._wav_path, "wb") as out,
                AudioClockWriter(self._clock_path) as clock,
            ):
                out.setnchannels(self._tap.channels)
                out.setsampwidth(2)
                out.setframerate(self._tap.rate)
                if self._doa is not None and self._doa_path is not None:
                    directions = open(self._doa_path, "w", encoding="utf-8")
                self._pump(out, clock, directions)
        except Exception as error:  # noqa: BLE001 - reported through stats
            logger.exception("audio recording failed")
            with self._lock:
                self._stats.error = str(error)
        finally:
            if directions is not None:
                directions.close()
            self._tap.release()
            if self._doa is not None:
                self._doa.release()
            logger.info(
                "audio recording stopped: %s, %d samples, %d filled",
                self._wav_path,
                self._stats.samples,
                self._stats.filled,
            )

    def _pump(self, out: wave.Wave_write, clock: AudioClockWriter, directions) -> None:
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
        seen_reading = 0

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
                    # The ring overwrote this block before it could be read. Its
                    # samples were already accounted for as a fill above.
                    previous = stamp
                    continue

                # The point is written before the samples so that the position
                # it names is where they are about to land.
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
                        )
                    )
                    last_point_at = stamp.monotonic
                    with self._lock:
                        self._stats.clock_points = clock.count

                out.writeframes(_to_int16(block).tobytes())
                with self._lock:
                    if self._stats.first_monotonic is None:
                        self._stats.first_monotonic = stamp.monotonic
                    self._stats.samples += len(block)
                    self._stats.last_monotonic = stamp.monotonic + len(block) / rate
                previous = stamp

            if directions is not None and self._doa is not None:
                seen_reading = _write_directions(directions, self._doa, seen_reading)

        # The last block, whether or not the interval has elapsed: a fit
        # extrapolates past its final point, and the end of a recording is
        # exactly where that matters.
        #
        # end_monotonic, not monotonic: the position being recorded is where the
        # file now ends, which is one block *past* the last stamp's first sample.
        # Pairing that position with the block's start time puts the final point
        # one block off the line every other point sits on - 16 ms at the default
        # block size, which a least-squares fit then spreads over the whole
        # recording.
        if previous is not None:
            clock.append(
                AudioClockPoint(
                    sample=self._stats.samples,
                    monotonic=previous.end_monotonic(rate),
                )
            )
            with self._lock:
                self._stats.clock_points = clock.count

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

        Two losses, and they overlap:

        * ``stamp.sample`` jumping past ``previous.end_sample`` means the ring
          overwrote audio the tap did have. The count of missing samples is
          exact.
        * capture time running past where those samples put it means the driver
          never delivered some audio at all. That count has to be inferred from
          the clock.

        The second is measured *after* accounting for the first, because a
        reader that lost 8 blocks also lost the 128 ms they occupied - and
        filling both would put twice the silence in the file.
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
            ``(n, channels)`` samples, or None if none of the block survived in
            the chunk - which happens when the ring wrapped past it.
        """
        start = max(stamp.sample, chunk.first_sample) - chunk.first_sample
        end = min(stamp.end_sample, chunk.cursor) - chunk.first_sample
        if end <= start:
            return None
        return chunk.samples[start:end]


def _write_directions(handle, doa: DoaTap, seen: int) -> int:
    """Append any new angle readings to the sidecar.

    Args:
        handle: Open text file to append JSON lines to.
        doa: The tap to read from.
        seen: Index of the last reading written.

    Returns:
        The new last-written index.

    The timestamps are ``time.monotonic()``, the same axis the audio's ADC times
    and the camera's converted frame times are on, so an angle can be placed
    against both the waveform it came from and the picture of whoever was
    talking.
    """
    reading = doa.latest(timeout=0.0, after=seen)
    if reading is None:
        return seen
    handle.write(
        json.dumps(
            {
                "t": reading.captured_at,
                "angle": reading.angle,
                "voice": reading.voice_activity,
            }
        )
        + "\n"
    )
    return reading.index


def _to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float32 in [-1, 1] back to the int16 the device sent."""
    return np.clip(samples * 32768.0, -32768, 32767).astype("<i2")
