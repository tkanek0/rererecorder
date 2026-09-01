"""Fan-out of the newest frame set to however many consumers want it.

A ``FrameSource`` can be iterated exactly once by exactly one reader, which is
awkward when a web server, an analysis pass and a point-cloud channel all want
the same frames. The hub reads the source on its own thread and lets anyone ask
for the most recent result.

Two decisions shape it, both borrowed from what the THETA playground learned:

* Only the newest set is kept. Every consumer here wants "what the camera sees
  now"; a queue would add latency and memory pressure to deliver frames nobody
  will look at.
* The source is reference counted. It opens when the first consumer arrives and
  closes shortly after the last one leaves, so an idle server does not hold the
  camera - which matters more here than it did there, because holding it stops
  anything else on the machine from opening it at all.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable

from .source import FrameSource, StreamError
from .types import Calibration, DeviceInfo, FrameSet

logger = logging.getLogger(__name__)

#: Seconds to wait before reopening the source after it failed.
RECONNECT_DELAY_S = 2.0

#: How long the source stays open after the last consumer leaves.
#:
#: Without this, a series of one-shot requests would reopen the device every
#: time, and a RealSense pipeline takes on the order of a second to start -
#: plus the auto-exposure needs a few frames to settle, so the first image
#: after a restart is also the worst one.
IDLE_SHUTDOWN_S = 10.0


class FrameHub:
    """Keeps the most recent frame set available to any number of readers."""

    def __init__(
        self,
        open_source: Callable[[], FrameSource],
        idle_shutdown_s: float = IDLE_SHUTDOWN_S,
        reconnect_delay_s: float = RECONNECT_DELAY_S,
    ) -> None:
        """Prepare the hub without opening anything.

        Args:
            open_source: Called on the hub's thread to create and open a
                source. It is called again after a failure or a restart, so it
                must be repeatable rather than hand back the same object.
            idle_shutdown_s: Seconds the source stays open with no consumers.
            reconnect_delay_s: Seconds to wait after a failure before retrying.
        """
        self._open_source = open_source
        self._idle_shutdown_s = idle_shutdown_s
        self._reconnect_delay_s = reconnect_delay_s

        self._lock = threading.Lock()
        self._updated = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._users = 0
        self._released_at = 0.0
        self._generation = 0
        self._wanted_generation = 0
        #: Index below which frames are from a superseded source. Without it a
        #: consumer that asks for "the current frame" right after a restart is
        #: handed one from before it - which means the calibration that comes
        #: back describes the resolution the caller just changed away from.
        self._floor = 0

        self._source: FrameSource | None = None
        self._latest: FrameSet | None = None
        #: Counted by the hub rather than taken from the frame set, so that it
        #: keeps increasing across a restart. A consumer polling with `after`
        #: would otherwise be handed frames it thinks it has already seen.
        self._index = 0
        #: Sticky: not cleared when frames resume. A USB drop that the hub
        #: recovers from in two seconds is invisible to anything polling at 1 Hz
        #: if the error disappears with the next frame, and "it broke a moment
        #: ago and came back" is exactly what someone debugging wants to know.
        #: Callers decide when it is stale, using _error_at.
        self._error: str | None = None
        self._error_at = 0.0
        self._device: DeviceInfo | None = None
        #: Smoothed frame *interval*, not frame rate. Averaging instantaneous
        #: rates instead reads high whenever delivery is jittery, because the
        #: mean of 1/dt exceeds 1/mean(dt): alternating 5 ms and 60 ms gaps
        #: average to 30.8 fps but their reciprocals average to 108.
        self._interval = 0.0
        self._last_at = 0.0

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        """Register a consumer, opening the source if it is not running."""
        with self._lock:
            self._users += 1
            self._ensure_thread()

    def release(self) -> None:
        """Deregister a consumer; the source closes after an idle period."""
        with self._lock:
            self._users = max(0, self._users - 1)
            self._released_at = time.monotonic()

    def held(self) -> _Hold:
        """Return a context manager that holds the hub open.

        Returns:
            An object usable in a ``with`` statement, releasing on exit.
        """
        return _Hold(self)

    def restart(self) -> None:
        """Close the current source and open a fresh one.

        Used when the requested stream configuration changed: the SDK settles
        resolution and frame rate at pipeline start, so there is no way to
        change them in place.

        With no consumers this only records the intent. Starting the reader
        would spawn a thread that immediately decides it should not be running -
        briefly reporting the hub as active while it did - and the next consumer
        to arrive opens with the new configuration regardless.
        """
        with self._lock:
            self._wanted_generation += 1
            self._floor = self._index
            if self._users > 0:
                self._ensure_thread()

    def stop(self) -> None:
        """Shut the hub down for good and wait for its thread to finish."""
        with self._lock:
            self._users = 0
            self._released_at = 0.0
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)

    def _ensure_thread(self) -> None:
        """Start the reader thread if it is not already running. Holds _lock."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="frame-hub", daemon=True
        )
        self._thread.start()

    # -- reading -----------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether the reader thread is currently running."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> str | None:
        """The most recent source failure, whether or not it has recovered.

        Compare ``error_at`` against the current time to tell a live problem
        from one the hub has already worked around.
        """
        with self._lock:
            return self._error

    @property
    def error_at(self) -> float:
        """``time.monotonic()`` of the most recent failure; 0 if there was none."""
        with self._lock:
            return self._error_at

    @property
    def device(self) -> DeviceInfo | None:
        """Identity of the camera, once it has been opened at least once."""
        with self._lock:
            return self._device

    @property
    def fps(self) -> float:
        """Recent delivery rate, smoothed. Zero before the second frame."""
        with self._lock:
            return round(1.0 / self._interval, 1) if self._interval > 0 else 0.0

    @property
    def source(self) -> FrameSource | None:
        """The source currently open, or None if the hub is not streaming.

        A way to reach controls that belong to the device rather than to the
        stream - recording, most of all, which can be paused and resumed without
        restarting the pipeline. The hub owns this object's lifetime, so it may
        be closed and replaced between the moment this returns and the moment a
        caller uses it: expect a call on the result to raise, and treat that as
        "the stream restarted" rather than as a failure.
        """
        with self._lock:
            return self._source

    @property
    def calibration(self) -> Calibration | None:
        """Calibration of the newest frame set, or None if there is none."""
        with self._lock:
            return self._latest.calibration if self._latest else None

    def latest(self, timeout: float = 5.0, after: int = 0) -> FrameSet | None:
        """Return the newest frame set, waiting for one if necessary.

        Args:
            timeout: Seconds to wait for a set newer than ``after``.
            after: Only return a set whose index exceeds this. Pass the index of
                the set you last handled to avoid handling it twice; pass 0 to
                take whatever is current - which, after a restart, means waiting
                for the new source rather than being handed the old one's last
                frame.

        Returns:
            The frame set, or None if nothing new arrived within the timeout.
        """
        deadline = time.monotonic() + timeout
        with self._updated:
            after = max(after, self._floor)
            while self._index <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._updated.wait(remaining)
            return self._latest

    # -- reader thread -----------------------------------------------------

    def _should_stop(self) -> bool:
        """Whether the source should be closed. Takes _lock."""
        with self._lock:
            if self._thread is None:
                return True
            if self._users > 0:
                return False
            return time.monotonic() - self._released_at > self._idle_shutdown_s

    def _superseded(self) -> bool:
        """Whether a restart was requested since this source opened."""
        with self._lock:
            return self._generation != self._wanted_generation

    def _record_error(self, message: str) -> None:
        """Store a failure and when it happened."""
        with self._lock:
            self._error = message
            self._error_at = time.monotonic()

    def _publish(self, frame_set: FrameSet) -> None:
        """Store a frame set and wake everyone waiting for one."""
        now = frame_set.received_at
        with self._updated:
            self._index += 1
            # The source's own counter restarts with each source; the hub's does
            # not, and `after` compares against the hub's.
            self._latest = dataclasses.replace(frame_set, index=self._index)
            if self._last_at:
                interval = now - self._last_at
                if interval > 0:
                    self._interval = (
                        interval
                        if self._interval == 0.0
                        else 0.9 * self._interval + 0.1 * interval
                    )
            self._last_at = now
            self._updated.notify_all()

    def _run(self) -> None:
        """Open the source, publish its frames, reopen it when it breaks."""
        logger.info("frame hub starting")
        while not self._should_stop():
            with self._lock:
                self._generation = self._wanted_generation
            try:
                with self._open_source() as source:
                    with self._lock:
                        self._source = source
                        self._device = getattr(source, "device", None)
                    for frame_set in source.frames():
                        if self._should_stop() or self._superseded():
                            break
                        self._publish(frame_set)
            except StreamError as exc:
                logger.warning("source failed: %s", exc)
                self._record_error(str(exc))
            except Exception as exc:  # noqa: BLE001 - any SDK failure retries
                logger.exception("unexpected source failure")
                self._record_error(str(exc))
            finally:
                with self._lock:
                    self._source = None

            if self._should_stop():
                break
            if not self._superseded():
                # A restart should take effect now; a failure should back off.
                time.sleep(self._reconnect_delay_s)

        logger.info("frame hub stopped")
        with self._updated:
            self._source = None
            self._latest = None
            self._last_at = 0.0
            self._interval = 0.0
            self._updated.notify_all()


class _Hold:
    """Context manager returned by ``FrameHub.held``."""

    def __init__(self, hub: FrameHub) -> None:
        self._hub = hub

    def __enter__(self) -> FrameHub:
        self._hub.acquire()
        return self._hub

    def __exit__(self, *exc: object) -> None:
        self._hub.release()
