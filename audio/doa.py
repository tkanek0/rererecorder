"""Polled access to the direction the array currently hears.

The XVF-3000 does not stream its direction estimate; it holds a current value
that has to be asked for. :class:`DoaTap` does the asking on a background
thread and keeps both the newest reading and a short trail of previous ones,
so a consumer that wakes up at its own rate always has something to show.

Timestamps come from ``time.monotonic()``, the same clock
:mod:`respeaker.capture` stamps audio with. That is what makes it possible to
line an angle up against the waveform it came from; converting to wall-clock
time is the delivery layer's job, and it happens once, at the edge.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import config
from .tuning import AccessDenied, DeviceNotFound, Tuning, find

logger = logging.getLogger(__name__)

#: Longer than the ordinary reconnect delay. A refused device node or an
#: unplugged array will not fix itself in two seconds, and hammering the bus
#: while it is broken only fills the log.
_FATAL_RETRY_S = 5.0


@dataclass(frozen=True)
class Reading:
    """One sample of what the chip believes about the sound field.

    Attributes:
        angle: Direction of arrival in degrees, 0-359.
        voice_activity: Whether the chip's VAD is currently firing.
        index: Monotonically increasing counter, used to detect a new reading.
        captured_at: ``time.monotonic()`` when the value was read.
    """

    angle: int
    voice_activity: bool
    index: int
    captured_at: float


class DoaTap:
    """Poll the array's direction estimate, and keep a short trail of it.

    Reference counted like :class:`respeaker.capture.AudioTap`: polling starts
    when the first consumer arrives and stops shortly after the last one
    leaves, so an idle process does not keep the USB bus busy.

    A missing device or a refused device node is reported through
    :attr:`error` rather than raised. The array is a thing that gets unplugged,
    and a viewer that keeps working with the angle greyed out beats one that
    dies.
    """

    def __init__(
        self,
        poll_hz: float = config.DOA_POLL_HZ,
        history_s: float = config.DOA_HISTORY_S,
    ) -> None:
        """Initialise the tap without opening the device.

        Args:
            poll_hz: How often to ask the chip for its current angle.
            history_s: Seconds of readings to keep.
        """
        self._interval = 1.0 / max(1e-3, poll_hz)
        self._history: deque[Reading] = deque(
            maxlen=max(1, int(history_s * poll_hz))
        )

        self._lock = threading.Lock()
        self._updated = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._users = 0
        self._released_at = 0.0
        self._latest: Reading | None = None
        self._index = 0
        self._error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        """Register a consumer, starting the poller if it is not running."""
        with self._lock:
            self._users += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="doa-tap", daemon=True
                )
                self._thread.start()

    def release(self) -> None:
        """Deregister a consumer; polling stops after an idle period."""
        with self._lock:
            self._users = max(0, self._users - 1)
            self._released_at = time.monotonic()

    @property
    def active(self) -> bool:
        """Whether the poller thread is currently running."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> str | None:
        """The most recent device error, if any."""
        with self._lock:
            return self._error

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop polling now, without waiting out the idle period.

        The reader runs on a daemon thread, so a process that exits while it
        is still open never closes its USB handle. Long-running consumers can rely on
        the idle timeout; anything that is about to exit should call this.

        Args:
            timeout: Seconds to wait for the thread to finish.
        """
        with self._lock:
            self._users = 0
            # Backdated so the idle check fires on the reader's next pass.
            self._released_at = 0.0
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def __enter__(self) -> DoaTap:
        """Acquire the tap for the duration of a ``with`` block."""
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        """Release the tap."""
        self.release()

    # -- reading -----------------------------------------------------------

    def latest(self, timeout: float = 1.0, after: int = 0) -> Reading | None:
        """Return the newest reading, waiting for a new one if necessary.

        Args:
            timeout: Seconds to wait for a reading newer than ``after``.
            after: Only return a reading whose index exceeds this value. Pass
                the index you last handled to avoid seeing it twice.

        Returns:
            The reading, or None if none arrived within the timeout.
        """
        deadline = time.monotonic() + timeout
        with self._updated:
            while self._index <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._updated.wait(remaining)
            return self._latest

    def history(self, seconds: float | None = None) -> list[Reading]:
        """Return recent readings, oldest first.

        Args:
            seconds: How far back to go, capped at what is kept. None asks for
                everything available.

        Returns:
            The readings, which may be empty if polling has not produced any.
        """
        with self._lock:
            readings = list(self._history)
        if seconds is None:
            return readings
        cutoff = time.monotonic() - seconds
        return [reading for reading in readings if reading.captured_at >= cutoff]

    # -- poller thread -----------------------------------------------------

    def _should_stop(self) -> bool:
        with self._lock:
            if self._users > 0:
                return False
            return time.monotonic() - self._released_at > config.IDLE_SHUTDOWN_S

    def _publish(self, angle: int, voice_activity: bool) -> None:
        with self._updated:
            self._index += 1
            reading = Reading(
                angle=angle,
                voice_activity=voice_activity,
                index=self._index,
                captured_at=time.monotonic(),
            )
            self._latest = reading
            self._history.append(reading)
            self._updated.notify_all()

    def _run(self) -> None:
        logger.info("doa tap starting")
        while not self._should_stop():
            device: Tuning | None = None
            try:
                device = find()
                with self._lock:
                    self._error = None
                next_at = time.monotonic()
                while not self._should_stop():
                    # Two transfers per poll, read together so the angle and
                    # the voice flag describe the same instant. They are not
                    # cheap - see the measurement in config.DOA_POLL_HZ.
                    angle = device.direction
                    voice = device.voice_activity
                    self._publish(angle, voice)

                    # Sleep to the next scheduled instant rather than for a
                    # fixed interval: the transfers take a good fraction of the
                    # period, and adding the interval on top of them would make
                    # the real rate roughly half the configured one.
                    next_at += self._interval
                    delay = next_at - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        # Fell behind. Resync rather than accumulating a debt
                        # that would later come out as a burst of transfers.
                        next_at = time.monotonic()
            except (DeviceNotFound, AccessDenied) as error:
                # Neither resolves on its own; say so once and back off.
                logger.warning("doa unavailable: %s", str(error).splitlines()[0])
                self._fail(str(error), _FATAL_RETRY_S)
            except Exception as error:  # noqa: BLE001 - any USB failure retries
                logger.warning("doa read failed: %s", error)
                self._fail(str(error), config.RECONNECT_DELAY_S)
            finally:
                if device is not None:
                    device.close()

        logger.info("doa tap stopped")
        with self._updated:
            self._latest = None
            self._updated.notify_all()

    def _fail(self, message: str, delay: float) -> None:
        """Record an error and wait, unless the tap is shutting down."""
        with self._lock:
            self._error = message
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not self._should_stop():
            time.sleep(min(0.25, deadline - time.monotonic()))
