"""Byte sources and sinks: serial port, TCP socket, log replay, and a scripted fake.

Every transport implements :class:`Transport`. ``read`` returns a
:class:`~serial_scope.events.Chunk` stamped with the host time it arrived, or ``None``
if nothing arrived within the timeout. A lost link (USB cable pulled, socket closed)
raises :class:`TransportError`; :mod:`serial_scope.connection` turns that into
automatic reconnection.
"""

from __future__ import annotations

import contextlib
import socket
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from serial_scope.events import RX, Chunk

if TYPE_CHECKING:
    import serial


class TransportError(OSError):
    """The link failed or could not be opened. Usually recoverable by reconnecting."""


class Transport(ABC):
    """Interface shared by every byte source."""

    name: str = ""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def open(self) -> None:
        """Open the link. Raises :class:`TransportError` on failure."""

    @abstractmethod
    def close(self) -> None:
        """Close the link. Safe to call more than once."""

    @abstractmethod
    def read(self, timeout: float) -> Chunk | None:
        """Wait up to ``timeout`` seconds for data."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send ``data``. Raises :class:`TransportError` on failure."""

    @property
    def finished(self) -> bool:
        """True when a finite source (a replay) has nothing more to deliver."""
        return False


class SerialTransport(Transport):
    """A serial port through pyserial (USB-UART bridges, native USB CDC, real UARTs)."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        dtr: bool | None = None,
        rts: bool | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.dtr = dtr
        self.rts = rts
        self.name = f"{port}@{baudrate}"
        self._clock = clock
        self._serial: serial.Serial | None = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    def open(self) -> None:
        import serial  # imported lazily so replay and tests do not need a port

        try:
            # serial_for_url also accepts pyserial URLs such as loop:// and rfc2217://.
            ser = serial.serial_for_url(self.port, do_not_open=True)
            ser.baudrate = self.baudrate
            ser.bytesize = self.bytesize
            ser.parity = self.parity
            ser.stopbits = self.stopbits
            ser.timeout = 0.05
            ser.write_timeout = 1.0
            # Set before opening so that boards with auto-reset circuits can be held.
            if self.dtr is not None:
                ser.dtr = self.dtr
            if self.rts is not None:
                ser.rts = self.rts
            ser.open()
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportError(f"cannot open {self.port}: {exc}") from exc
        self._serial = ser

    def close(self) -> None:
        ser = self._serial
        self._serial = None
        if ser is not None:
            # Closing a device that has vanished can fail; it is gone either way.
            with contextlib.suppress(Exception):
                ser.close()

    def read(self, timeout: float) -> Chunk | None:
        import serial

        ser = self._serial
        if ser is None:
            raise TransportError("port is not open")
        try:
            ser.timeout = timeout
            waiting = ser.in_waiting
            data = ser.read(max(1, waiting))
            if data and ser.in_waiting:
                data += ser.read(ser.in_waiting)
        except (serial.SerialException, OSError) as exc:
            self.close()
            raise TransportError(f"{self.port}: {exc}") from exc
        if not data:
            return None
        return Chunk(bytes(data), self._clock(), RX)

    def write(self, data: bytes) -> None:
        import serial

        ser = self._serial
        if ser is None:
            raise TransportError("port is not open")
        try:
            ser.write(data)
        except (serial.SerialException, OSError) as exc:
            self.close()
            raise TransportError(f"{self.port}: {exc}") from exc


class TcpTransport(Transport):
    """A TCP client, e.g. an ESP32 that serves its telemetry on a socket over Wi-Fi."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout: float = 3.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.name = f"tcp://{host}:{port}"
        self._clock = clock
        self._sock: socket.socket | None = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        except OSError as exc:
            raise TransportError(f"cannot connect to {self.name}: {exc}") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            sock.close()

    def read(self, timeout: float) -> Chunk | None:
        sock = self._sock
        if sock is None:
            raise TransportError("socket is not open")
        sock.settimeout(timeout)
        try:
            data = sock.recv(65536)
        except TimeoutError:
            return None
        except OSError as exc:
            self.close()
            raise TransportError(f"{self.name}: {exc}") from exc
        if not data:
            self.close()
            raise TransportError(f"{self.name}: connection closed by peer")
        return Chunk(data, self._clock(), RX)

    def write(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise TransportError("socket is not open")
        try:
            sock.sendall(data)
        except OSError as exc:
            self.close()
            raise TransportError(f"{self.name}: {exc}") from exc


class ReplayTransport(Transport):
    """Plays an ``.sslog`` file back with its original timestamps.

    ``speed`` 1.0 reproduces the original pacing, 4.0 plays four times faster and 0
    delivers everything as fast as it can be read. The chunks keep their recorded host
    times, so decoded samples and exports carry the original times too. Sent (``tx``),
    marker and info records are delivered as well; consumers check
    :attr:`Chunk.direction`. Writes are counted and discarded.
    """

    def __init__(
        self,
        path: str | Path,
        speed: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from serial_scope.logfile import LogReader

        if speed < 0:
            raise ValueError("speed must be >= 0")
        self.path = Path(path)
        self.speed = speed
        self.name = f"replay:{self.path.name}"
        self._clock = clock
        self._sleep = sleep
        self.reader = LogReader(self.path)
        self._iter: Iterator[Chunk] | None = None
        self._next: Chunk | None = None
        self._origin: tuple[float, float] | None = None  # (log time, wall time)
        self._done = False
        self.discarded_writes = 0

    @property
    def is_open(self) -> bool:
        return self._iter is not None

    @property
    def finished(self) -> bool:
        return self._done

    def open(self) -> None:
        if self._iter is None:
            self._iter = iter(self.reader)
            self._done = False

    def close(self) -> None:
        self._iter = None

    def read(self, timeout: float) -> Chunk | None:
        if self._iter is None:
            raise TransportError("replay is not open")
        if self._next is None:
            self._next = next(self._iter, None)
            if self._next is None:
                self._done = True
                self._sleep(min(timeout, 0.01))
                return None
        chunk = self._next
        if self.speed > 0:
            now = self._clock()
            if self._origin is None:
                self._origin = (chunk.t, now)
            due = self._origin[1] + (chunk.t - self._origin[0]) / self.speed
            wait = due - now
            if wait > timeout:
                self._sleep(timeout)
                return None
            if wait > 0:
                self._sleep(wait)
        self._next = None
        return chunk

    def write(self, data: bytes) -> None:
        self.discarded_writes += 1


class FakeTransport(Transport):
    """A scripted transport for tests and demos.

    ``script`` items are delivered one per ``read``: ``bytes`` become a chunk, an
    exception instance is raised (use :class:`TransportError` to simulate an unplug) and
    ``None`` is a read timeout. ``open_failures`` makes the next N ``open`` calls fail.
    Times come from ``clock``, by default a counter that advances 10 ms per read. With
    ``sleep`` given, a timeout (a ``None`` item or an exhausted script) waits for the read
    timeout like a real port would, instead of returning at once.
    """

    def __init__(
        self,
        script: Iterable[bytes | BaseException | None] = (),
        *,
        open_failures: int = 0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        name: str = "fake",
    ) -> None:
        self.script = list(script)
        self.open_failures = open_failures
        self.name = name
        self.written = bytearray()
        self.open_calls = 0
        self._open = False
        self._time = 1_700_000_000.0
        self._clock = clock
        self._sleep = sleep

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        self._time += 0.01
        return self._time

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def finished(self) -> bool:
        return not self.script

    def open(self) -> None:
        self.open_calls += 1
        if self.open_failures > 0:
            self.open_failures -= 1
            raise TransportError(f"{self.name}: device not present")
        self._open = True

    def close(self) -> None:
        self._open = False

    def read(self, timeout: float) -> Chunk | None:
        if not self._open:
            raise TransportError(f"{self.name}: not open")
        item = self.script.pop(0) if self.script else None
        if item is None:
            if self._sleep is not None:
                self._sleep(timeout)
            return None
        if isinstance(item, BaseException):
            self._open = False
            raise item
        return Chunk(item, self._now(), RX)

    def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportError(f"{self.name}: not open")
        self.written += data


def parse_source(spec: str, baudrate: int = 115200, speed: float = 1.0) -> Transport:
    """Build a transport from a command-line style source description.

    ``COM5`` / ``/dev/ttyUSB0`` -> serial port; ``tcp://host:port`` -> TCP client;
    ``*.sslog`` (an existing file) -> replay.
    """
    if spec.startswith("tcp://"):
        rest = spec[len("tcp://") :]
        host, sep, port = rest.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(f"expected tcp://HOST:PORT, got {spec!r}")
        return TcpTransport(host.strip("[]"), int(port))
    if spec.lower().endswith(".sslog"):
        return ReplayTransport(spec, speed)
    return SerialTransport(spec, baudrate)
