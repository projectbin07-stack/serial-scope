"""Reconnect logic, driven by the fake transport and a fake clock."""

from __future__ import annotations

import time

import pytest

from serial_scope.connection import (
    CLOSED,
    CONNECTED,
    FINISHED,
    WAITING,
    ReconnectingSource,
)
from serial_scope.events import Chunk
from serial_scope.reader import ReaderThread
from serial_scope.transport import FakeTransport, TransportError


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _source(fake: FakeTransport, clock: FakeClock, **kw: bool) -> ReconnectingSource:
    return ReconnectingSource(
        fake,
        clock=clock,
        wall_clock=clock,
        sleep=clock.sleep,
        initial_backoff=0.5,
        max_backoff=2.0,
        **kw,
    )


def _drive(src: ReconnectingSource, steps: int) -> list[bytes]:
    out = []
    for _ in range(steps):
        chunk = src.read(0.1)
        if chunk is not None:
            out.append(chunk.data)
        if src.finished:
            break
    return out


def test_retries_with_exponential_backoff_until_device_appears() -> None:
    clock = FakeClock()
    fake = FakeTransport([b"hello"], open_failures=4)
    src = _source(fake, clock)
    data = _drive(src, 200)
    assert data == [b"hello"]
    events = src.drain_events()
    waits = [e for e in events if e.state == WAITING]
    assert len(waits) == 4
    assert [w.message.split("retrying in ")[1] for w in waits] == [
        "0.5 s",
        "1.0 s",
        "2.0 s",
        "2.0 s",  # capped at max_backoff
    ]
    connected = [e for e in events if e.state == CONNECTED]
    assert len(connected) == 1 and connected[0].message == "connected to fake"
    # Attempts happened at 0, 0.5, 1.5, 3.5, 5.5 s: the device was found at 5.5 s.
    assert connected[0].t == pytest.approx(5.5)
    assert events[-1].state == FINISHED
    assert src.failed_attempts == 4


def test_reconnects_after_unplug_and_resumes() -> None:
    clock = FakeClock()
    fake = FakeTransport([b"a", b"b", TransportError("device disconnected"), b"c", b"d"])
    src = _source(fake, clock)
    assert _drive(src, 100) == [b"a", b"b", b"c", b"d"]
    states = [e.state for e in src.drain_events()]
    assert states == [CONNECTED, WAITING, CONNECTED, FINISHED]
    assert src.connects == 2
    assert src.drops == 1
    assert fake.open_calls == 2


def test_unplugged_device_that_stays_away_for_a_while() -> None:
    clock = FakeClock()
    fake = FakeTransport([b"a", TransportError("gone")])
    src = _source(fake, clock)
    assert src.read(0.1) is not None
    assert src.read(0.1) is None  # the drop
    fake.open_failures = 3  # replug takes a few attempts
    fake.script = [b"back"]
    assert _drive(src, 100) == [b"back"]
    messages = [e.message for e in src.drain_events()]
    assert messages[1].startswith("link lost (gone)")
    assert messages[-2] == "reconnected to fake"


def test_no_auto_reconnect_closes() -> None:
    clock = FakeClock()
    fake = FakeTransport([b"a", TransportError("gone"), b"never"])
    src = _source(fake, clock, auto_reconnect=False)
    assert _drive(src, 10) == [b"a"]
    assert src.state == CLOSED and src.finished
    assert src.read(0.1) is None
    failing = _source(FakeTransport([b"x"], open_failures=1), clock, auto_reconnect=False)
    assert failing.read(0.1) is None
    assert failing.state == CLOSED


def test_write_paths() -> None:
    clock = FakeClock()
    fake = FakeTransport([None, None, None])
    src = _source(fake, clock)
    assert src.write(b"early") is False  # not connected yet
    src.read(0.1)
    assert src.write(b"cmd\n") is True
    assert fake.written == b"cmd\n"

    class Broken(FakeTransport):
        def write(self, data: bytes) -> None:
            raise TransportError("write timeout")

    broken = Broken([None])
    bsrc = _source(broken, clock)
    bsrc.read(0.1)
    assert bsrc.write(b"x") is False
    assert bsrc.state == WAITING
    assert not broken.is_open
    src.close()
    assert src.drain_events()[-1].state == CLOSED


def test_reader_thread_delivers_everything_in_order() -> None:
    payload = [bytes([i]) * 10 for i in range(50)]
    fake = FakeTransport([*payload[:20], TransportError("blip"), *payload[20:]])
    src = ReconnectingSource(fake, initial_backoff=0.01)
    reader = ReaderThread(src, poll_timeout=0.01)
    reader.start()
    reader.join(10)
    assert not reader.is_alive()
    items = reader.drain()
    chunks = [i.data for i in items if isinstance(i, Chunk)]
    assert chunks == payload
    states = [i.state for i in items if not isinstance(i, Chunk)]
    assert states == [CONNECTED, WAITING, CONNECTED, FINISHED, CLOSED]
    assert reader.error is None


def test_reader_thread_stop_and_write() -> None:
    fake = FakeTransport([None] * 100000)
    reader = ReaderThread(ReconnectingSource(fake), poll_timeout=0.01)
    reader.start()
    deadline = time.monotonic() + 5
    while not fake.is_open and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reader.write(b"x") is True
    assert fake.written == b"x"
    reader.stop()
    assert not reader.is_alive()
    assert not fake.is_open
