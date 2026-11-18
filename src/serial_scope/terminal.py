"""Terminal helpers that do not depend on Tk: rendering received bytes and parsing input."""

from __future__ import annotations

import codecs
import re

LINE_ENDINGS: dict[str, bytes] = {
    "None": b"",
    "LF": b"\n",
    "CR": b"\r",
    "CR+LF": b"\r\n",
}

_CONTROL_NAMES = {
    0x00: "NUL",
    0x07: "BEL",
    0x08: "BS",
    0x1B: "ESC",
    0x7F: "DEL",
}


def hexdump_line(data: bytes, offset: int = 0, width: int = 16) -> str:
    """Format up to ``width`` bytes as ``OFFSET  HH HH ...  ascii``."""
    hexpart = " ".join(f"{b:02X}" for b in data)
    ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data)
    return f"{offset:08X}  {hexpart:<{width * 3 - 1}}  {ascii_part}"


def hexdump(data: bytes, offset: int = 0, width: int = 16) -> list[str]:
    return [
        hexdump_line(data[i : i + width], offset + i, width) for i in range(0, len(data), width)
    ]


class TextRenderer:
    """Turns received bytes into display text.

    UTF-8 sequences split across reads are reassembled; CR LF and lone CR become a
    newline; other control characters are shown as ``<NAME>`` / ``<xHH>`` so binary
    noise is visible instead of corrupting the display.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_cr = False

    def feed(self, data: bytes) -> str:
        text = self._decoder.decode(data)
        out: list[str] = []
        for ch in text:
            code = ord(ch)
            if self._pending_cr:
                self._pending_cr = False
                if ch == "\n":
                    continue
            if ch == "\r":
                out.append("\n")
                self._pending_cr = True
            elif ch in "\n\t":
                out.append(ch)
            elif code < 0x20 or code == 0x7F:
                out.append(f"<{_CONTROL_NAMES.get(code, f'x{code:02X}')}>")
            else:
                out.append(ch)
        return "".join(out)


_HEX_INPUT = re.compile(r"^(?:[0-9A-Fa-f]{2}[\s,:]*)*$")


def encode_input(text: str, line_ending: str = "LF", hex_mode: bool = False) -> bytes:
    """Bytes to send for the send box contents.

    In text mode the C escapes ``\\n``, ``\\r``, ``\\t``, ``\\\\`` and ``\\xHH`` are
    understood and the chosen line ending is appended. In hex mode the input is pairs of
    hex digits, optionally separated by spaces, commas or colons, and no line ending is
    added.
    """
    if hex_mode:
        compact = re.sub(r"[\s,:]", "", text)
        if not _HEX_INPUT.match(text) or len(compact) % 2:
            raise ValueError(f"not a hex byte string: {text!r}")
        return bytes.fromhex(compact)
    if line_ending not in LINE_ENDINGS:
        raise ValueError(f"unknown line ending {line_ending!r}")
    out = bytearray()
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            simple = {"n": b"\n", "r": b"\r", "t": b"\t", "\\": b"\\", "0": b"\x00"}
            if nxt in simple:
                out += simple[nxt]
                i += 2
                continue
            if nxt == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 2 : i + 4]):
                out.append(int(text[i + 2 : i + 4], 16))
                i += 4
                continue
        out += ch.encode("utf-8")
        i += 1
    return bytes(out) + LINE_ENDINGS[line_ending]
