from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from serial_scope.events import MARK, RX, Chunk
from serial_scope.logfile import LogWriter
from serial_scope.transport import (
    FakeTransport,
    ReplayTransport,
    SerialTransport,
    TcpTransport,
    TransportError,
    parse_source,
)

T0 = 1_700_000_000.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _write_log(path: Path, chunks: list[Chunk]) -> None:
    with LogWriter(path, start_time=T0) as w:
        for c in chunks:
            w.write(c)


def test_replay_paces_by_recorded_time(tmp_path: Path) -> None:
    path = tmp_path / "r.sslog"
    _write_log(
        path,
        [
            Chunk(b"a", T0),
            Chunk(b"b", T0 + 1.0),
            Chunk(b"m", T0 + 1.0, MARK),
            Chunk(b"c", T0 + 3.0),
        ],
    )
    clock = FakeClock()
    replay = ReplayTransport(path, speed=2.0, clock=clock, sleep=clock.sleep)
    replay.open()
    got: list[Chunk] = []
    while not replay.finished:
        chunk = replay.read(0.25)
        if chunk is not None:
            got.append(chunk)
            assert clock.now == pytest.approx((chunk.t - T0) / 2.0)
    assert [c.data for c in got] == [b"a", b"b", b"m", b"c"]
    assert got[2].direction == MARK
    assert got[3].t == pytest.approx(T0 + 3.0)  # original timestamps are kept
    assert max(clock.sleeps) <= 0.25  # never blocks longer than the read timeout


def test_replay_unpaced_and_writes_discarded(tmp_path: Path) -> None:
    path = tmp_path / "r.sslog"
    _write_log(path, [Chunk(b"x", T0 + i * 100) for i in range(5)])
    clock = FakeClock()
    replay = ReplayTransport(path, speed=0, clock=clock, sleep=clock.sleep)
    with pytest.raises(TransportError):
        replay.read(0.1)
    replay.open()
    assert replay.is_open
    data = [replay.read(0.1) for _ in range(5)]
    assert all(c is not None for c in data)
    assert clock.now == 0.0
    assert replay.read(0.1) is None and replay.finished
    replay.write(b"ignored")
    assert replay.discarded_writes == 1
    replay.close()
    assert not replay.is_open
    with pytest.raises(ValueError):
        ReplayTransport(path, speed=-1)


def test_fake_transport_script() -> None:
    fake = FakeTransport([b"one", None, TransportError("unplugged"), b"two"], open_failures=1)
    with pytest.raises(TransportError):
        fake.read(0.1)
    with pytest.raises(TransportError, match="not present"):
        fake.open()
    fake.open()
    first = fake.read(0.1)
    assert first is not None and first.data == b"one" and first.direction == RX
    assert fake.read(0.1) is None
    with pytest.raises(TransportError, match="unplugged"):
        fake.read(0.1)
    assert not fake.is_open
    with pytest.raises(TransportError):
        fake.write(b"x")
    fake.open()
    fake.write(b"hi")
    assert fake.written == b"hi"
    assert fake.open_calls == 3


def test_serial_transport_with_pyserial_loopback() -> None:
    """Exercises the real pyserial code path via its built-in loop:// device."""
    port = SerialTransport("loop://", 115200, dtr=False, rts=False)
    assert port.name == "loop://@115200"
    with pytest.raises(TransportError):
        port.read(0.1)
    with pytest.raises(TransportError):
        port.write(b"x")
    port.open()
    assert port.is_open
    port.write(b"ping\r\n")
    received = b""
    for _ in range(20):
        chunk = port.read(0.05)
        if chunk is not None:
            received += chunk.data
        if received == b"ping\r\n":
            break
    assert received == b"ping\r\n"
    assert port.read(0.01) is None
    port.close()
    port.close()
    assert not port.is_open


def test_serial_transport_open_failure() -> None:
    port = SerialTransport("this-port-does-not-exist-42", 9600)
    with pytest.raises(TransportError, match="cannot open"):
        port.open()


@pytest.fixture
def tcp_server() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


def test_tcp_transport_round_trip_and_peer_close(tcp_server: tuple[socket.socket, int]) -> None:
    server, port = tcp_server
    received: list[bytes] = []

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            conn.sendall(b"temp:21.5\n")
            received.append(conn.recv(100))
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    tcp = TcpTransport("127.0.0.1", port)
    tcp.open()
    chunk = None
    for _ in range(40):
        chunk = tcp.read(0.05)
        if chunk is not None:
            break
    assert chunk is not None and chunk.data == b"temp:21.5\n"
    tcp.write(b"hello")
    thread.join(5)
    assert received == [b"hello"]
    with pytest.raises(TransportError, match="closed by peer"):
        for _ in range(40):
            tcp.read(0.05)
    assert not tcp.is_open
    with pytest.raises(TransportError):
        tcp.write(b"x")
    with pytest.raises(TransportError):
        tcp.read(0.01)


def test_tcp_connect_refused() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here now
    with pytest.raises(TransportError, match="cannot connect"):
        TcpTransport("127.0.0.1", port, connect_timeout=1.0).open()


def test_parse_source(tmp_path: Path) -> None:
    serial = parse_source("COM5", 9600)
    assert isinstance(serial, SerialTransport) and serial.baudrate == 9600
    tcp = parse_source("tcp://192.168.4.1:3333")
    assert isinstance(tcp, TcpTransport) and (tcp.host, tcp.port) == ("192.168.4.1", 3333)
    v6 = parse_source("tcp://[::1]:23")
    assert isinstance(v6, TcpTransport) and v6.host == "::1"
    log = tmp_path / "a.sslog"
    _write_log(log, [])
    assert isinstance(parse_source(str(log)), ReplayTransport)
    for bad in ("tcp://nohost", "tcp://:80", "tcp://host:http"):
        with pytest.raises(ValueError):
            parse_source(bad)
