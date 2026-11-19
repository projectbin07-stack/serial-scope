"""Shared machinery of the COBS-framed binary decoders.

Both firmware protocols that serial-scope decodes in binary (encoder-odometry-firmware
and the nrf24-telemetry-link gateway) send ``COBS(packet) 0x00`` frames. This module
splits a byte stream at the ``0x00`` delimiters, bounds the size of a frame, remembers
the host time of each frame's first byte, and hands complete frames to a subclass.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from serial_scope.decoders.base import Decoder
from serial_scope.events import Event


class CobsFrameDecoder(Decoder):
    """Base class for formats of ``COBS(packet) 0x00`` frames.

    Subclasses set :attr:`max_frame` (the longest legal frame between two delimiters,
    without the delimiter) and implement :meth:`decode_frame`. Anything longer is
    reported once as an ``overlong`` error and skipped up to the next delimiter, which
    is how such a receiver resynchronises after boot text or a lost delimiter. Empty
    frames (back-to-back delimiters) carry nothing and are ignored.
    """

    binary = True
    max_frame: ClassVar[int] = 255

    def __init__(self) -> None:
        super().__init__()
        self._buf = bytearray()
        self._t_start: float | None = None
        self._overlong = False
        self._preview = b""

    def feed(self, data: bytes, t: float) -> list[Event]:
        events: list[Event] = []
        start = 0
        n = len(data)
        while start < n:
            end = data.find(b"\x00", start)
            piece = data[start:] if end < 0 else data[start:end]
            if piece and self._t_start is None:
                self._t_start = t
            if not self._overlong:
                self._buf += piece
                if len(self._buf) > self.max_frame:
                    self._overlong = True
                    self._preview = bytes(self._buf[:24])
                    self._buf.clear()
            if end < 0:
                break
            frame_t = self._t_start if self._t_start is not None else t
            events.extend(self._frame(frame_t))
            start = end + 1
        return events

    def _frame(self, t: float) -> list[Event]:
        raw = bytes(self._buf)
        overlong = self._overlong
        self._buf.clear()
        self._overlong = False
        self._t_start = None
        if overlong:
            return [
                self.error(
                    t,
                    "overlong",
                    f"more than {self.max_frame} bytes between delimiters"
                    f" (starts {self._preview!r})",
                    self._preview,
                )
            ]
        if not raw:
            return []  # back-to-back delimiters carry nothing
        return self.decode_frame(t, raw)

    @abstractmethod
    def decode_frame(self, t: float, raw: bytes) -> list[Event]:
        """Decode the bytes between two delimiters (still COBS-encoded, non-empty)."""


class ClockUnwrapper:
    """Extends a device's free-running unsigned counter across wraparound.

    ``bits`` is the counter width and ``scale`` converts counts to seconds. A large step
    backwards (more than half the range) is a wrap; a small one means the device
    restarted, and the offset starts again from zero.
    """

    def __init__(self, bits: int, scale: float) -> None:
        self.range = 1 << bits
        self.scale = scale
        self._offset = 0
        self._last: int | None = None

    def __call__(self, value: int) -> float:
        last = self._last
        if last is not None and value < last:
            if last - value > self.range // 2:
                self._offset += self.range
            else:
                self._offset = 0
        self._last = value
        return (value + self._offset) * self.scale
