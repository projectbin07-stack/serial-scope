"""Telemetry lines of the ina219-battery-monitor firmware.

Format (one line per sample, CR LF terminated)::

    $BM1,<seq>,<t_ms>,<bus_mV>,<current_uA>,<power_uW>,<charge_uAh>,<energy_uWh>,<soc_permille>,<flags>*<HH>

``HH`` is a CRC-8 (polynomial 0x07, init 0x00) over every character between ``$`` and
``*``. Lines starting with ``#`` are status messages. This is an independent
re-implementation of the format documented in
https://github.com/projectbin07-stack/ina219-battery-monitor (README, "Telemetry frame
format"); the values are converted to V, mA, mW, mAh, mWh and percent.

Besides CRC and format errors the decoder counts frames missing according to the
sequence number (``dropped``) and sequence restarts (``restarts``, a device reset).
"""

from __future__ import annotations

from serial_scope.decoders.base import LineDecoder
from serial_scope.decoders.cobsframes import ClockUnwrapper
from serial_scope.decoders.framing import crc8
from serial_scope.events import Event, Sample

FRAME_TAG = "BM1"
UINT32_MAX = 0xFFFFFFFF
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

# (wire name, low, high, channel name, scale)
_FIELDS: tuple[tuple[str, int, int, str, float], ...] = (
    ("seq", 0, UINT32_MAX, "", 1.0),
    ("t_ms", 0, UINT32_MAX, "", 1.0),
    ("bus_mV", INT32_MIN, INT32_MAX, "bus_v", 1e-3),
    ("current_uA", INT32_MIN, INT32_MAX, "current_ma", 1e-3),
    ("power_uW", INT32_MIN, INT32_MAX, "power_mw", 1e-3),
    ("charge_uAh", INT32_MIN, INT32_MAX, "charge_mah", 1e-3),
    ("energy_uWh", INT32_MIN, INT32_MAX, "energy_mwh", 1e-3),
    ("soc_permille", 0, 1000, "soc_pct", 0.1),
    ("flags", 0, 255, "flags", 1.0),
)


class Ina219FrameError(ValueError):
    """A ``$BM1`` line that failed validation. ``kind`` is ``crc`` or ``format``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _parse_int(text: str, low: int, high: int, name: str) -> int:
    digits = text[1:] if text.startswith("-") else text
    if not digits or not digits.isascii() or not digits.isdigit() or len(digits) > 10:
        raise Ina219FrameError("format", f"field {name!r} is not a decimal integer: {text!r}")
    value = int(text)
    if not low <= value <= high:
        raise Ina219FrameError("format", f"field {name!r} out of range: {value}")
    return value


def decode_bm1(line: str) -> list[int]:
    """Validate one ``$BM1`` line and return its nine integer fields in wire order."""
    text = line.strip()
    if not text.startswith("$"):
        raise Ina219FrameError("format", "missing '$' start marker")
    body, star, checksum = text[1:].rpartition("*")
    if not star:
        raise Ina219FrameError("format", "missing '*' checksum marker")
    if len(checksum) != 2 or any(c not in "0123456789ABCDEFabcdef" for c in checksum):
        raise Ina219FrameError("format", f"malformed checksum: {checksum!r}")
    try:
        raw = body.encode("ascii")
    except UnicodeEncodeError as exc:
        raise Ina219FrameError("format", "non-ASCII character in frame") from exc
    expected = crc8(raw)
    if int(checksum, 16) != expected:
        raise Ina219FrameError(
            "crc", f"checksum mismatch: got {checksum.upper()}, computed {expected:02X}"
        )
    fields = body.split(",")
    if fields[0] != FRAME_TAG:
        raise Ina219FrameError("format", f"unknown frame tag: {fields[0]!r}")
    if len(fields) != len(_FIELDS) + 1:
        raise Ina219FrameError("format", f"expected {len(_FIELDS)} fields, got {len(fields) - 1}")
    return [
        _parse_int(field, low, high, name)
        for field, (name, low, high, _, _) in zip(fields[1:], _FIELDS, strict=True)
    ]


def encode_bm1(fields: list[int]) -> str:
    """Build a ``$BM1`` line (without terminator) from nine integer fields."""
    body = FRAME_TAG + "," + ",".join(str(v) for v in fields)
    return f"${body}*{crc8(body.encode('ascii')):02X}"


class Ina219Decoder(LineDecoder):
    name = "ina219"
    description = "ina219-battery-monitor '$BM1,...*HH' lines with CRC-8"

    def __init__(self) -> None:
        super().__init__()
        self.last_seq: int | None = None
        # t_ms is the firmware's millis(); unwrap it across the 49.7-day wrap.
        self._clock = ClockUnwrapper(32, 1e-3)

    def decode_line(self, t: float, text: str, raw: bytes) -> list[Event]:
        stripped = text.strip()
        if not stripped.startswith("$"):
            return [self.text(t, text)]
        try:
            fields = decode_bm1(stripped)
        except Ina219FrameError as exc:
            return [self.text(t, text), self.error(t, exc.kind, str(exc), raw)]
        seq, t_ms = fields[0], fields[1]
        if self.last_seq is not None:
            if seq > self.last_seq:
                self.counts["dropped"] += seq - self.last_seq - 1
            elif self.last_seq - seq > UINT32_MAX // 2:  # 32-bit sequence wrap
                self.counts["dropped"] += (seq - self.last_seq - 1) % (UINT32_MAX + 1)
            else:
                self.counts["restarts"] += 1
        self.last_seq = seq
        self.counts["frames"] += 1
        values = {
            channel: value * scale
            for value, (_, _, _, channel, scale) in zip(fields, _FIELDS, strict=True)
            if channel
        }
        return [Sample(t=t, values=values, device_t=self._clock(t_ms), source="BM1")]
