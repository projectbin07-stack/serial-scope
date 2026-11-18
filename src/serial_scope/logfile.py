"""The ``.sslog`` raw log format: every byte, with host timestamps, replayable exactly.

Layout (all integers little-endian)::

    header  = b"SSLOG" version:u8 start_ns:u64 meta_len:u32 meta:JSON(meta_len bytes)
    record  = kind:u8 dt_us:uvarint length:uvarint payload[length]

``start_ns`` is the host wall-clock time of the start of the file in nanoseconds since the
Unix epoch. ``dt_us`` is the time since the previous record (or since ``start_ns`` for the
first one) in whole microseconds, so a record's time is ``start + sum(dt_us)``. Kinds are
1 = received bytes, 2 = sent bytes, 3 = marker (UTF-8 text), 4 = info (UTF-8 text, e.g.
"connected"). Received and sent payloads are stored unmodified.

A record costs 3 bytes of overhead for a typical USB-serial chunk (kind, a 1-2 byte delta,
a 1 byte length). A file cut short by a crash or a full disk is read up to the last
complete record and reported as truncated.

With ``max_bytes`` set, the writer rotates: ``run.sslog`` is followed by ``run_002.sslog``,
``run_003.sslog`` and so on, each a complete file with its own header. :func:`log_parts`
finds the parts of a set and :class:`LogReader` reads them as one stream.
"""

from __future__ import annotations

import json
import re
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from serial_scope.events import INFO, MARK, RX, TX, Chunk

MAGIC = b"SSLOG"
VERSION = 1
_HEADER = struct.Struct("<5sBQI")
KIND_CODES = {RX: 1, TX: 2, MARK: 3, INFO: 4}
CODE_KINDS = {code: kind for kind, code in KIND_CODES.items()}


class LogFormatError(ValueError):
    """The file is not an ``.sslog`` file or uses an unsupported version."""


def _uvarint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_uvarint(f: BinaryIO) -> int | None:
    result = 0
    shift = 0
    while True:
        b = f.read(1)
        if not b:
            return None
        result |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return result
        shift += 7
        if shift > 63:
            raise LogFormatError("varint too long")


def part_path(path: Path, index: int) -> Path:
    """Path of part ``index`` (1-based) of a rotated log set."""
    if index == 1:
        return path
    return path.with_name(f"{path.stem}_{index:03d}{path.suffix}")


def log_parts(path: str | Path) -> list[Path]:
    """Return ``path`` followed by any rotation parts that exist next to it, in order."""
    path = Path(path)
    parts = [path]
    pattern = re.compile(re.escape(path.stem) + r"_(\d{3,})" + re.escape(path.suffix) + "$")
    found = sorted(
        (int(m.group(1)), p)
        for p in path.parent.glob(f"{path.stem}_*{path.suffix}")
        if (m := pattern.match(p.name))
    )
    expected = 2
    for index, p in found:
        if index != expected:
            break
        parts.append(p)
        expected += 1
    return parts


class LogWriter:
    """Append chunks to an ``.sslog`` file, flushing after every record.

    ``meta`` is stored as JSON in the header of every part (port, baud, decoder, ...).
    """

    def __init__(
        self,
        path: str | Path,
        meta: dict[str, Any] | None = None,
        max_bytes: int | None = None,
        start_time: float | None = None,
    ) -> None:
        self.path = Path(path)
        self.meta = dict(meta or {})
        self.max_bytes = max_bytes
        self.part = 0
        self.paths: list[Path] = []
        self.bytes_written = 0  # over all parts
        self.records = 0
        self._f: BinaryIO | None = None
        self._open_part(time.time() if start_time is None else start_time)

    def _open_part(self, start_time: float) -> None:
        self.part += 1
        path = part_path(self.path, self.part)
        meta = dict(self.meta, part=self.part)
        meta_bytes = json.dumps(meta, sort_keys=True).encode("utf-8")
        self._start_us = round(start_time * 1e6)
        header = _HEADER.pack(MAGIC, VERSION, self._start_us * 1000, len(meta_bytes)) + meta_bytes
        self._f = path.open("wb")
        self._f.write(header)
        self._f.flush()
        self._size = len(header)
        self._header_size = len(header)
        self._last_us = self._start_us
        self.bytes_written += len(header)
        self.paths.append(path)

    @property
    def closed(self) -> bool:
        return self._f is None

    def write(self, chunk: Chunk) -> None:
        """Append one chunk. Times earlier than the previous record are clamped to it."""
        if self._f is None:
            raise ValueError("log is closed")
        code = KIND_CODES[chunk.direction]
        t_us = round(chunk.t * 1e6)
        record_len = len(chunk.data) + 12
        if (
            self.max_bytes is not None
            and self._size > self._header_size
            and self._size + record_len > self.max_bytes
        ):
            self._f.close()
            self._open_part(max(chunk.t, self._last_us / 1e6))
        dt = max(0, t_us - self._last_us)
        self._last_us += dt
        record = bytes([code]) + _uvarint(dt) + _uvarint(len(chunk.data)) + chunk.data
        assert self._f is not None
        self._f.write(record)
        self._f.flush()
        self._size += len(record)
        self.bytes_written += len(record)
        self.records += 1

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def __enter__(self) -> LogWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class LogInfo:
    """Header data and integrity status of a log (set)."""

    paths: list[Path]
    meta: dict[str, Any] = field(default_factory=dict)
    start_time: float = 0.0
    truncated: bool = False


def read_header(f: BinaryIO, name: str = "log") -> tuple[float, dict[str, Any]]:
    """Read and check the header; return ``(start_time_s, meta)``."""
    raw = f.read(_HEADER.size)
    if len(raw) < _HEADER.size:
        raise LogFormatError(f"{name}: too short to be an .sslog file")
    magic, version, start_ns, meta_len = _HEADER.unpack(raw)
    if magic != MAGIC:
        raise LogFormatError(f"{name}: not an .sslog file (bad magic)")
    if version != VERSION:
        raise LogFormatError(f"{name}: unsupported .sslog version {version}")
    meta_raw = f.read(meta_len)
    if len(meta_raw) < meta_len:
        raise LogFormatError(f"{name}: header truncated")
    try:
        meta = json.loads(meta_raw.decode("utf-8")) if meta_len else {}
    except ValueError as exc:
        raise LogFormatError(f"{name}: header metadata is not valid JSON") from exc
    return start_ns / 1e9, meta


class LogReader:
    """Iterate over the chunks of a log, or of a rotated set when ``follow_parts``."""

    def __init__(self, path: str | Path, follow_parts: bool = True) -> None:
        paths = log_parts(path) if follow_parts else [Path(path)]
        self.info = LogInfo(paths=paths)
        with paths[0].open("rb") as f:
            self.info.start_time, self.info.meta = read_header(f, paths[0].name)

    def __iter__(self) -> Iterator[Chunk]:
        for path in self.info.paths:
            yield from self._read_part(path)

    def _read_part(self, path: Path) -> Iterator[Chunk]:
        with path.open("rb") as f:
            start, _ = read_header(f, path.name)
            start_us = round(start * 1e6)
            t_us = start_us
            while True:
                kind_byte = f.read(1)
                if not kind_byte:
                    return
                dt = _read_uvarint(f)
                length = _read_uvarint(f) if dt is not None else None
                if length is None:
                    self.info.truncated = True
                    return
                kind = CODE_KINDS.get(kind_byte[0])
                if kind is None:
                    raise LogFormatError(f"{path.name}: unknown record kind {kind_byte[0]}")
                payload = f.read(length)
                if len(payload) < length:
                    self.info.truncated = True
                    return
                assert dt is not None
                t_us += dt
                yield Chunk(payload, t_us / 1e6, kind)
