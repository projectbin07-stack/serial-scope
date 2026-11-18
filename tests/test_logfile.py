from __future__ import annotations

import random
import struct
from pathlib import Path

import pytest

from serial_scope.events import INFO, MARK, RX, TX, Chunk
from serial_scope.logfile import (
    LogFormatError,
    LogReader,
    LogWriter,
    _uvarint,
    log_parts,
    part_path,
)

START = 1_700_000_000.123456


def _random_chunks(n: int, seed: int = 1) -> list[Chunk]:
    rng = random.Random(seed)
    t = START
    chunks = []
    for _ in range(n):
        t += rng.choice([0.0, 0.000001, 0.0003, 0.02, 1.5, 300.0])
        size = rng.choice([0, 1, 2, 63, 64, 200, 5000])
        data = bytes(rng.randrange(256) for _ in range(size))
        direction = rng.choice([RX, RX, RX, TX, MARK, INFO])
        chunks.append(Chunk(data, round(t, 6), direction))
    return chunks


def test_round_trip_is_byte_exact(tmp_path: Path) -> None:
    chunks = _random_chunks(400)
    path = tmp_path / "run.sslog"
    with LogWriter(path, {"port": "COM5", "baudrate": 115200}, start_time=START) as w:
        for c in chunks:
            w.write(c)
    reader = LogReader(path)
    assert reader.info.meta == {"port": "COM5", "baudrate": 115200, "part": 1}
    assert reader.info.start_time == pytest.approx(START, abs=1e-6)
    back = list(reader)
    assert [(c.data, c.direction) for c in back] == [(c.data, c.direction) for c in chunks]
    for original, replayed in zip(chunks, back, strict=True):
        assert replayed.t == pytest.approx(original.t, abs=1e-6)
    assert not reader.info.truncated
    # Overhead: header + 3..~8 bytes per record.
    payload = sum(len(c.data) for c in chunks)
    assert path.stat().st_size < payload + 30 + 8 * len(chunks) + 200


def test_times_are_kept_to_the_microsecond(tmp_path: Path) -> None:
    path = tmp_path / "t.sslog"
    times = [START + 0.000001 * k for k in (0, 1, 2, 1_000_000, 86_400_000_000)]
    with LogWriter(path, start_time=START) as w:
        for t in times:
            w.write(Chunk(b"x", t))
    back = [c.t for c in LogReader(path)]
    expected = [round(t * 1e6) / 1e6 for t in times]
    assert back == pytest.approx(expected, abs=5e-7)


def test_backwards_time_is_clamped(tmp_path: Path) -> None:
    path = tmp_path / "b.sslog"
    with LogWriter(path, start_time=START) as w:
        w.write(Chunk(b"a", START + 2))
        w.write(Chunk(b"b", START + 1))  # host clock stepped back
    a, b = LogReader(path)
    assert b.t == a.t


def test_varint_encoding() -> None:
    assert _uvarint(0) == b"\x00"
    assert _uvarint(127) == b"\x7f"
    assert _uvarint(128) == b"\x80\x01"
    assert _uvarint(300) == b"\xac\x02"


def test_truncated_log_reads_complete_records(tmp_path: Path) -> None:
    path = tmp_path / "cut.sslog"
    with LogWriter(path, start_time=START) as w:
        for i in range(10):
            w.write(Chunk(bytes([i]) * 20, START + i))
    data = path.read_bytes()
    path.write_bytes(data[:-7])  # crash in the middle of the last record
    reader = LogReader(path)
    back = list(reader)
    assert len(back) == 9
    assert reader.info.truncated
    path.write_bytes(data[:-21])  # cut inside the varints of the last record
    reader = LogReader(path)
    assert len(list(reader)) == 9


def test_bad_files(tmp_path: Path) -> None:
    p = tmp_path / "x.sslog"
    p.write_bytes(b"hello")
    with pytest.raises(LogFormatError, match="too short"):
        LogReader(p)
    p.write_bytes(b"NOTSS" + bytes(13))
    with pytest.raises(LogFormatError, match="bad magic"):
        LogReader(p)
    p.write_bytes(struct.pack("<5sBQI", b"SSLOG", 9, 0, 0))
    with pytest.raises(LogFormatError, match="version 9"):
        LogReader(p)
    p.write_bytes(struct.pack("<5sBQI", b"SSLOG", 1, 0, 10) + b"{}")
    with pytest.raises(LogFormatError, match="header truncated"):
        LogReader(p)
    p.write_bytes(struct.pack("<5sBQI", b"SSLOG", 1, 0, 2) + b"{x")
    with pytest.raises(LogFormatError, match="JSON"):
        LogReader(p)
    p.write_bytes(struct.pack("<5sBQI", b"SSLOG", 1, 0, 0) + b"\x09\x00\x00")
    with pytest.raises(LogFormatError, match="unknown record kind"):
        list(LogReader(p))


def test_write_after_close_fails(tmp_path: Path) -> None:
    w = LogWriter(tmp_path / "c.sslog")
    w.close()
    assert w.closed
    with pytest.raises(ValueError):
        w.write(Chunk(b"x", START))


def test_rotation_by_size_and_reading_the_set(tmp_path: Path) -> None:
    path = tmp_path / "run.sslog"
    chunks = [Chunk(bytes([i % 256]) * 100, START + i * 0.01) for i in range(100)]
    with LogWriter(path, {"port": "COM7"}, max_bytes=1000, start_time=START) as w:
        for c in chunks:
            w.write(c)
    parts = log_parts(path)
    assert parts == w.paths
    assert len(parts) > 5
    assert parts[1] == tmp_path / "run_002.sslog"
    assert all(p.stat().st_size <= 1000 for p in parts)
    back = list(LogReader(path))
    assert [c.data for c in back] == [c.data for c in chunks]
    assert [c.t for c in back] == pytest.approx([c.t for c in chunks], abs=1e-6)
    # Each part is a complete log on its own, with its part number in the header.
    second = LogReader(parts[1], follow_parts=False)
    assert second.info.meta == {"port": "COM7", "part": 2}
    assert len(list(second)) > 0


def test_log_parts_stops_at_a_gap(tmp_path: Path) -> None:
    base = tmp_path / "a.sslog"
    for p in (base, part_path(base, 2), part_path(base, 4)):
        p.write_bytes(b"")
    assert log_parts(base) == [base, tmp_path / "a_002.sslog"]


def test_record_larger_than_max_bytes_still_written(tmp_path: Path) -> None:
    path = tmp_path / "big.sslog"
    with LogWriter(path, max_bytes=50, start_time=START) as w:
        w.write(Chunk(b"x" * 500, START))
        w.write(Chunk(b"y" * 500, START + 1))
    assert len(w.paths) == 2
    assert [len(c.data) for c in LogReader(path)] == [500, 500]
