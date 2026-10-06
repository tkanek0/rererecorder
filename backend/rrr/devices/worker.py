"""One device read on a background thread and shared by every consumer.

Reference counted: the device opens with the first consumer and closes an idle
period after the last leaves. A failure is reported, never retried, until
:meth:`SharedWorker.reconnect` (docs/decisions.md 29).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SharedWorker(Generic[T]):
    """Reads one device on its own thread and hands what it reads to consumers.

    Pollers take the newest item with :meth:`latest`; a listener receives every
    item, in order. Subclasses implement :meth:`_serve`.
    """

    #: Thread and log name.
    name = "worker"

    def __init__(self, idle_shutdown_s: float) -> None:
        """Prepare the worker without opening anything.

        Args:
            idle_shutdown_s: Seconds the device stays open with no consumers.
        """
        self._idle_shutdown_s = idle_shutdown_s
        self._lock = threading.Lock()
        self._updated = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._users = 0
        self._released_at = 0.0
        self._latest: T | None = None
        #: Counts every item published, across reopenings.
        self._index = 0
        #: Items at or below this came from a device opening that has ended.
        self._floor = 0
        self._failed = False
        self._error: str | None = None
        self._error_at = 0.0
        self._listeners: list[Callable[[T], None]] = []
        self._listener_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        """Register a consumer, opening the device unless it is open or failed."""
        with self._lock:
            self._users += 1
            self._ensure_thread()

    def release(self) -> None:
        """Deregister a consumer; the device closes after an idle period."""
        with self._lock:
            self._users = max(0, self._users - 1)
            self._released_at = time.monotonic()

    def reconnect(self) -> None:
        """Clear a failure and, if anyone is waiting, open the device again."""
        with self._lock:
            self._failed = False
            self._error = None
            self._error_at = 0.0
            if self._users > 0:
                self._ensure_thread()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Close the device now, without waiting out the idle period.

        Args:
            timeout: Seconds to wait for the thread to finish.
        """
        with self._lock:
            self._users = 0
            self._released_at = 0.0
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def add_listener(self, listener: Callable[[T], None]) -> None:
        """Call ``listener`` with every item, in order, on the worker's thread.

        It must return quickly and should not raise; a raise is logged. Adding
        one does not hold the device open: pair it with :meth:`acquire`.
        """
        with self._listener_lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[T], None]) -> None:
        """Stop calling a listener; removing an unknown one is not an error."""
        with self._listener_lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether the worker thread is running."""
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

    @property
    def error_at(self) -> float:
        """``time.monotonic()`` of the latest failure; 0 if there is none."""
        with self._lock:
            return self._error_at

    def latest(self, timeout: float = 5.0, after: int = 0) -> T | None:
        """Return the newest item, waiting for one if necessary.

        Args:
            timeout: Seconds to wait for an item newer than ``after``.
            after: Only return an item published after this index; 0 takes the
                current device opening's newest.

        Returns:
            The item, or None if nothing new arrived in time - at once while
            the device has failed.
        """
        with self._updated:
            if not self._wait(lambda: self._index > max(after, self._floor), timeout):
                return None
            return self._latest

    # -- for subclasses ----------------------------------------------------

    def _serve(self) -> None:
        """Open the device and publish what it reads until :meth:`_should_stop`.

        Raises:
            Exception: Any failure, which is reported and not retried.
        """
        raise NotImplementedError

    def _closed(self) -> None:
        """Called on the worker's thread once the device is closed."""

    def _should_stop(self) -> bool:
        """Whether nobody has wanted the device for the idle period."""
        with self._lock:
            if self._users > 0:
                return False
            return time.monotonic() - self._released_at > self._idle_shutdown_s

    def _wait(self, ready: Callable[[], bool], timeout: float) -> bool:
        """Wait until ``ready()``, giving up at once while failed.

        The caller holds ``self._updated``.
        """
        deadline = time.monotonic() + timeout
        while not ready():
            if self._failed:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._updated.wait(remaining)
        return True

    def _publish(self, item: T) -> T:
        """Store an item as the newest, wake pollers and call listeners.

        Returns:
            What was stored, which :meth:`_stamp` may have changed.
        """
        with self._updated:
            self._index += 1
            stored = self._stamp(item, self._index)
            self._latest = stored
            self._updated.notify_all()
        with self._listener_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(stored)
            except Exception:  # noqa: BLE001 - one consumer is not the device
                logger.exception("a %s listener failed", self.name)
        return stored

    def _stamp(self, item: T, index: int) -> T:
        """Give an item its index, for items that carry one."""
        return item

    def _fail(self, error: str) -> None:
        """Mark the device failed now, before it is closed, and wake readers.

        Detaches the thread, so a reconnect while it is still closing starts a
        fresh one.
        """
        with self._updated:
            if self._failed:
                return
            first_line = error.splitlines()[0] if error else error
            logger.warning("%s failed, not retrying: %s", self.name, first_line)
            self._failed = True
            self._error = error
            self._error_at = time.monotonic()
            if self._thread is threading.current_thread():
                self._thread = None
            self._updated.notify_all()

    # -- the thread --------------------------------------------------------

    def _ensure_thread(self) -> None:
        """Start the worker thread unless it is running or has failed.

        Holds _lock.
        """
        if self._failed:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        logger.info("%s starting", self.name)
        try:
            self._serve()
        except Exception as error:  # noqa: BLE001 - reported, not raised
            self._fail(str(error))
        finally:
            self._closed()
        logger.info("%s stopped", self.name)
        with self._updated:
            self._latest = None
            # A reader arriving after this waits for the next opening's items.
            self._floor = self._index
            if self._thread is threading.current_thread():
                self._thread = None
                # A consumer that arrived while this thread was finishing.
                if self._users > 0:
                    self._ensure_thread()
            self._updated.notify_all()
