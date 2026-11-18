"""Keeps a transport connected: opens it, notices when it drops, and retries.

The common case this handles is an Arduino or ESP32 being unplugged, reset into the
bootloader for flashing, or re-enumerated by Windows. The port disappears, reads fail,
and the board comes back a few seconds later under the same name. Instead of exiting,
:class:`ReconnectingSource` closes the port, waits with exponential backoff and opens it
again, reporting each transition as a :class:`StatusEvent`.

Time is injectable (``clock`` and ``sleep``) so the logic is tested without waiting.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from serial_scope.events import Chunk
from serial_scope.transport import Transport, TransportError

CONNECTED = "connected"
DISCONNECTED = "disconnected"
WAITING = "waiting"
CLOSED = "closed"
FINISHED = "finished"


@dataclass(frozen=True)
class StatusEvent:
    t: float
    state: str
    message: str


class ReconnectingSource:
    """Wraps a :class:`Transport` with connect / reconnect handling.

    With ``auto_reconnect`` false, a failed open or a lost link leaves the source in the
    ``closed`` state instead of retrying.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        auto_reconnect: bool = True,
        initial_backoff: float = 0.5,
        max_backoff: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.auto_reconnect = auto_reconnect
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self._clock = clock
        self._wall = wall_clock
        self._sleep = sleep
        self._backoff = initial_backoff
        self._next_attempt = clock()
        self.state = DISCONNECTED
        self.connects = 0
        self.drops = 0
        self.failed_attempts = 0
        self._events: list[StatusEvent] = []

    def _emit(self, state: str, message: str) -> None:
        self.state = state
        self._events.append(StatusEvent(self._wall(), state, message))

    def drain_events(self) -> list[StatusEvent]:
        """Return and forget the status events since the last call."""
        events, self._events = self._events, []
        return events

    @property
    def finished(self) -> bool:
        return self.state in (CLOSED, FINISHED)

    def _try_open(self) -> bool:
        try:
            self.transport.open()
        except TransportError as exc:
            self.failed_attempts += 1
            if not self.auto_reconnect:
                self._emit(CLOSED, str(exc))
                return False
            self._next_attempt = self._clock() + self._backoff
            self._emit(WAITING, f"{exc}; retrying in {self._backoff:.1f} s")
            self._backoff = min(self._backoff * 2, self.max_backoff)
            return False
        self.connects += 1
        self._backoff = self.initial_backoff
        verb = "reconnected to" if self.connects > 1 else "connected to"
        self._emit(CONNECTED, f"{verb} {self.transport.name}")
        return True

    def read(self, timeout: float) -> Chunk | None:
        """Read once, connecting first if needed. Never raises :class:`TransportError`."""
        if self.finished:
            self._sleep(min(timeout, 0.05))
            return None
        if not self.transport.is_open:
            wait = self._next_attempt - self._clock()
            if wait > 0:
                self._sleep(min(timeout, wait))
                return None
            if not self._try_open():
                return None
        try:
            chunk = self.transport.read(timeout)
        except TransportError as exc:
            self.transport.close()
            self.drops += 1
            if self.auto_reconnect:
                self._next_attempt = self._clock() + self._backoff
                self._emit(WAITING, f"link lost ({exc}); reconnecting")
            else:
                self._emit(CLOSED, f"link lost ({exc})")
            return None
        if chunk is None and self.transport.finished:
            self._emit(FINISHED, f"{self.transport.name}: end of data")
        return chunk

    def write(self, data: bytes) -> bool:
        """Send ``data``; returns False (and schedules a reconnect) if the link is down."""
        if not self.transport.is_open:
            return False
        try:
            self.transport.write(data)
        except TransportError as exc:
            self.transport.close()
            self.drops += 1
            self._next_attempt = self._clock() + self._backoff
            self._emit(WAITING if self.auto_reconnect else CLOSED, f"write failed ({exc})")
            return False
        return True

    def close(self) -> None:
        self.transport.close()
        if self.state != CLOSED:
            self._emit(CLOSED, f"closed {self.transport.name}")
