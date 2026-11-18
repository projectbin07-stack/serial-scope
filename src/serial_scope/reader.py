"""Background reader: moves chunks from a source into a queue for the UI thread."""

from __future__ import annotations

import queue
import threading

from serial_scope.connection import ReconnectingSource, StatusEvent
from serial_scope.events import Chunk

QueueItem = Chunk | StatusEvent


class ReaderThread(threading.Thread):
    """Reads ``source`` until stopped or until the source is finished.

    Every chunk and status event is put on :attr:`queue` in arrival order. The queue is
    unbounded on purpose: if the consumer falls behind, data waits rather than being lost.
    """

    def __init__(self, source: ReconnectingSource, poll_timeout: float = 0.05) -> None:
        super().__init__(name="serial-scope-reader", daemon=True)
        self.source = source
        self.poll_timeout = poll_timeout
        self.queue: queue.Queue[QueueItem] = queue.Queue()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    chunk = self.source.read(self.poll_timeout)
                    events = self.source.drain_events()
                for event in events:
                    self.queue.put(event)
                if chunk is not None:
                    self.queue.put(chunk)
                if self.source.finished:
                    break
        except Exception as exc:  # pragma: no cover - reported to the UI, not swallowed
            self.error = exc
        finally:
            with self._lock:
                self.source.close()
                for event in self.source.drain_events():
                    self.queue.put(event)

    def write(self, data: bytes) -> bool:
        """Send from another thread; serialised with reads."""
        with self._lock:
            return self.source.write(data)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    def drain(self, max_items: int = 10_000) -> list[QueueItem]:
        """Take up to ``max_items`` queued items without blocking."""
        items: list[QueueItem] = []
        try:
            while len(items) < max_items:
                items.append(self.queue.get_nowait())
        except queue.Empty:
            pass
        return items
