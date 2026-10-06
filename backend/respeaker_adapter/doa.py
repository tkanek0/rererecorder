"""Polled access to the direction the array currently hears.

The XVF-3000 holds a current value rather than streaming it, so
:class:`DoaTap` polls it on a background thread.
Timestamps are ``time.monotonic()``, the axis audio is on.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Self

from . import config
from .tuning import AccessDenied, DeviceNotFound, Tuning, find

logger = logging.getLogger(__name__)

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
    """Poll the array's direction estimate.

    Reference counted like :class:`respeaker_adapter.capture.AudioTap`. A missing or
    refused device is reported through :attr:`error`, not raised, and not
    retried until :meth:`reconnect` (docs/decisions.md 29).
    """

    def __init__(self, poll_hz: float = config.DOA_POLL_HZ) -> None:
        """Initialise the tap without opening the device.

        Args:
            poll_hz: How often to ask the chip for its current angle.
        """
        self._interval = 1.0 / max(1e-3, poll_hz)

        self._lock = threading.Lock()
        self._updated = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._users = 0
        self._released_at = 0.0
        self._latest: Reading | None = None
        self._index = 0
        #: Set when the device fails, and kept until reconnect().
        self._failed = False
        #: Why the device last failed, cleared along with _failed.
        self._error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        """Register a consumer, starting the poller unless it runs or failed."""
        with self._lock:
            self._users += 1
            self._ensure_thread()

    def reconnect(self) -> None:
        """Clear a failure and, if anyone is waiting, poll the device again.

        Nothing calls this automatically; see :meth:`AudioTap.reconnect`.
        """
        with self._lock:
            self._failed = False
            self._error = None
            if self._users > 0:
                self._ensure_thread()

    def _ensure_thread(self) -> None:
        """Start the poller thread unless it is running or has failed.

        Holds _lock.
        """
        if self._failed:
            return
        if self._thread is not None and self._thread.is_alive():
            return
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
    def failed(self) -> bool:
        """Whether the device failed and is waiting for :meth:`reconnect`."""
        with self._lock:
            return self._failed

    @property
    def error(self) -> str | None:
        """Why the device failed, while :attr:`failed` is set; else None."""
        with self._lock:
            return self._error

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop polling now, without waiting out the idle period.

        Call before exiting: the daemon poller would otherwise never close its
        USB handle.

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

    def __enter__(self) -> Self:
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
            after: Only return a reading whose index exceeds this value.

        Returns:
            The reading, or None if none arrived within the timeout - at once,
            without waiting, while the tap has failed.
        """
        deadline = time.monotonic() + timeout
        with self._updated:
            while self._index <= after:
                if self._failed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._updated.wait(remaining)
            return self._latest

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
            self._updated.notify_all()

    def _run(self) -> None:
        logger.info("doa tap starting")
        failure: str | None = None
        device: Tuning | None = None
        try:
            device = find()
            next_at = time.monotonic()
            while not self._should_stop():
                # Read together so both describe the same instant; see
                # config.DOA_POLL_HZ for their cost.
                angle = device.direction
                voice = device.voice_activity
                self._publish(angle, voice)

                # Sleep to a schedule, not a fixed interval, or the transfer
                # time would roughly halve the real rate.
                next_at += self._interval
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    # Fell behind: resync rather than burst to catch up.
                    next_at = time.monotonic()
        except (DeviceNotFound, AccessDenied) as error:
            logger.warning(
                "doa unavailable, not retrying: %s", str(error).splitlines()[0]
            )
            failure = str(error)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            logger.warning("doa read failed, not retrying: %s", error)
            failure = str(error)
        finally:
            if device is not None:
                device.close()

        logger.info("doa tap stopped")
        with self._updated:
            self._latest = None
            if failure is not None:
                self._failed = True
                self._error = failure
                # Detach so a reconnect during return starts a fresh poller.
                if self._thread is threading.current_thread():
                    self._thread = None
            self._updated.notify_all()
