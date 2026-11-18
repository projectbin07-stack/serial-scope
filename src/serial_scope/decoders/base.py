"""Decoder interface and the shared line splitter used by the text-based formats."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import ClassVar

from serial_scope.events import DecodeError, Event, TextLine

MAX_LINE_BYTES = 4096
"""Lines longer than this are reported as an ``overlong`` error and discarded."""

# Names a device might use for its own clock, with the factor that converts to seconds.
DEVICE_TIME_KEYS: dict[str, float] = {
    "t_us": 1e-6,
    "time_us": 1e-6,
    "micros": 1e-6,
    "us": 1e-6,
    "t_ms": 1e-3,
    "time_ms": 1e-3,
    "millis": 1e-3,
    "ms": 1e-3,
    "t_s": 1.0,
    "time_s": 1.0,
    "t": 1.0,
    "time": 1.0,
    "timestamp": 1.0,
}

_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def parse_number(token: str) -> float | None:
    """Parse a decimal number the way a C ``printf`` would print it.

    Returns ``None`` if ``token`` is not a number. ``nan`` and ``inf`` (which Arduino's
    ``Serial.print`` emits for non-finite floats) are recognised and returned as such.
    """
    token = token.strip()
    if _NUMBER.match(token):
        return float(token)
    low = token.lower()
    if low in ("nan", "-nan", "+nan"):
        return math.nan
    if low in ("inf", "+inf", "ovf"):
        return math.inf
    if low == "-inf":
        return -math.inf
    return None


class Decoder(ABC):
    """Turns a byte stream into :class:`~serial_scope.events.Event` objects.

    Decoders are fed arbitrary chunks (a frame or line may be split across any number of
    reads) and must keep whatever state they need between calls. Everything that cannot
    be decoded is reported as a :class:`DecodeError` and counted in :attr:`errors`.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    def __init__(self) -> None:
        self.errors: Counter[str] = Counter()
        self.counts: Counter[str] = Counter()

    @abstractmethod
    def feed(self, data: bytes, t: float) -> list[Event]:
        """Consume ``data`` received at host time ``t`` and return complete events."""

    def flush(self, t: float) -> list[Event]:
        """Return whatever is pending at the end of the stream."""
        return []

    def error(self, t: float, kind: str, detail: str, raw: bytes = b"") -> DecodeError:
        """Count an error of ``kind`` and build the event that reports it."""
        self.errors[kind] += 1
        return DecodeError(t=t, kind=kind, detail=detail, raw=raw)


class LineSplitter:
    """Splits a byte stream into lines terminated by LF, CR LF or a lone CR.

    Each line carries the host time of the chunk that contained its first byte.
    """

    def __init__(self, max_line: int = MAX_LINE_BYTES) -> None:
        self.max_line = max_line
        self._buf = bytearray()
        self._t_start: float | None = None
        self._skip_lf = False
        self._overflow = False

    def feed(self, data: bytes, t: float) -> list[tuple[float, bytes | None]]:
        """Return ``(t, line)`` pairs; ``line`` is ``None`` for a discarded overlong line."""
        out: list[tuple[float, bytes | None]] = []
        start = 0
        n = len(data)
        while start < n:
            if self._skip_lf:
                self._skip_lf = False
                if data[start] == 0x0A:
                    start += 1
                    continue
            lf = data.find(b"\n", start)
            cr = data.find(b"\r", start)
            ends = [i for i in (lf, cr) if i >= 0]
            end = min(ends) if ends else -1
            piece = data[start:] if end < 0 else data[start:end]
            if piece and self._t_start is None:
                self._t_start = t
            if not self._overflow:
                self._buf += piece
                if len(self._buf) > self.max_line:
                    self._overflow = True
                    self._buf.clear()
            if end < 0:
                break
            line_t = self._t_start if self._t_start is not None else t
            if self._overflow:
                out.append((line_t, None))
            else:
                out.append((line_t, bytes(self._buf)))
            self._buf.clear()
            self._overflow = False
            self._t_start = None
            if data[end] == 0x0D:
                self._skip_lf = True
            start = end + 1
        return out

    def flush(self) -> tuple[float, bytes] | None:
        """Return an unterminated final line, if any."""
        if self._buf and self._t_start is not None and not self._overflow:
            line = (self._t_start, bytes(self._buf))
        else:
            line = None
        self._buf.clear()
        self._t_start = None
        self._overflow = False
        return line


class LineDecoder(Decoder):
    """Base class for formats that are one record per text line."""

    def __init__(self) -> None:
        super().__init__()
        self._splitter = LineSplitter()

    def feed(self, data: bytes, t: float) -> list[Event]:
        events: list[Event] = []
        for line_t, raw in self._splitter.feed(data, t):
            if raw is None:
                events.append(
                    self.error(line_t, "overlong", f"line longer than {MAX_LINE_BYTES} bytes")
                )
                continue
            events.extend(self._line(line_t, raw))
        return events

    def flush(self, t: float) -> list[Event]:
        pending = self._splitter.flush()
        if pending is None:
            return []
        return self._line(*pending)

    def _line(self, t: float, raw: bytes) -> list[Event]:
        self.counts["lines"] += 1
        text = raw.decode("utf-8", errors="replace")
        return self.decode_line(t, text, raw)

    @abstractmethod
    def decode_line(self, t: float, text: str, raw: bytes) -> list[Event]:
        """Decode one line (without its terminator)."""

    def text(self, t: float, text: str) -> TextLine:
        """Build a text event and count it."""
        self.counts["text"] += 1
        return TextLine(t=t, text=text)
