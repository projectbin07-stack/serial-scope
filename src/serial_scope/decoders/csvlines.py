"""CSV with a header line.

The decoder waits for a header (a line of at least two non-numeric names separated by
``,``, ``;`` or a tab) and then decodes every line with the same number of fields as a
row of numbers. A column whose name is a known device-clock name (``t_ms``, ``millis``,
``time_us``, ...) becomes the sample's device time instead of a channel.

A header repeated later (the board was reset and printed it again) is accepted and may
change the columns. Rows with the wrong number of fields or with a non-numeric value
are reported as errors. If the header was missed (the device printed it once at boot,
before the monitor was attached), the column names can be given up front with
``CsvDecoder(columns=[...])`` (``--columns`` on the command line).
"""

from __future__ import annotations

import math

from serial_scope.decoders.base import DEVICE_TIME_KEYS, LineDecoder, parse_number
from serial_scope.events import Event, Sample

_DELIMITERS = (",", ";", "\t")


def _split(text: str, delimiter: str) -> list[str]:
    return [field.strip() for field in text.split(delimiter)]


def detect_header(text: str) -> tuple[str, list[str]] | None:
    """Return ``(delimiter, names)`` if ``text`` looks like a CSV header line."""
    stripped = text.strip()
    for delimiter in _DELIMITERS:
        if delimiter not in stripped:
            continue
        names = _split(stripped, delimiter)
        if len(names) < 2 or any(not name for name in names):
            continue
        if any(parse_number(name) is not None for name in names):
            continue
        if len(set(names)) != len(names):
            continue
        if any(name[0] in "$#{[" or ":" in name for name in names):
            continue  # frames, JSON and plotter 'label:value' pairs are not headers
        return delimiter, names
    return None


class CsvDecoder(LineDecoder):
    name = "csv"
    description = "CSV with a header line; t_ms/millis/time_us columns become device time"

    def __init__(self, columns: list[str] | None = None, delimiter: str = ",") -> None:
        """``columns`` names the fields up front, for a device that printed its header
        before the monitor was attached; a header seen later still replaces them."""
        super().__init__()
        self.delimiter: str | None = delimiter if columns else None
        self.columns: list[str] = list(columns) if columns else []

    def decode_line(self, t: float, text: str, raw: bytes) -> list[Event]:
        header = detect_header(text)
        if header is not None:
            self.delimiter, self.columns = header
            self.counts["headers"] += 1
            return [self.text(t, text)]
        if self.delimiter is None or not text.strip():
            return [self.text(t, text)]
        fields = _split(text.strip(), self.delimiter)
        if len(fields) != len(self.columns):
            if len(fields) == 1:
                # A line without the delimiter is a log message, not a broken row.
                return [self.text(t, text)]
            return [
                self.text(t, text),
                self.error(
                    t,
                    "columns",
                    f"expected {len(self.columns)} fields, got {len(fields)}: {text!r}",
                    raw,
                ),
            ]
        values: dict[str, float] = {}
        device_t: float | None = None
        for name, field in zip(self.columns, fields, strict=True):
            if not field:
                continue
            value = parse_number(field)
            if value is None:
                return [
                    self.text(t, text),
                    self.error(t, "value", f"column {name!r} is not a number: {field!r}", raw),
                ]
            scale = DEVICE_TIME_KEYS.get(name.lower())
            if scale is not None and device_t is None:
                device_t = value * scale
            elif math.isfinite(value):
                values[name] = value
        self.counts["samples"] += 1
        return [Sample(t=t, values=values, device_t=device_t, source="csv")]
