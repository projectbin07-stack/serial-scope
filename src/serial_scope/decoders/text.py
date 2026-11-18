"""Plain text: every line is shown as-is, nothing is plotted."""

from __future__ import annotations

from serial_scope.decoders.base import LineDecoder
from serial_scope.events import Event


class TextDecoder(LineDecoder):
    name = "text"
    description = "plain text lines, no channels"

    def decode_line(self, t: float, text: str, raw: bytes) -> list[Event]:
        return [self.text(t, text)]
