"""Decoded CSV export: one row per sample, one column per channel.

Columns are ``host_time`` (Unix seconds), ``device_time`` (seconds, empty when the format
has no device clock), ``source`` (record type) and then every channel in order of first
appearance. Channels are discovered while the stream runs; when a new one appears the
file is rewritten once with the wider header, so the file on disk is always a valid CSV.
"""

from __future__ import annotations

import csv
import math
import os
from pathlib import Path
from types import TracebackType

from serial_scope.channels import ChannelSet
from serial_scope.events import Sample

FIXED_COLUMNS = ("host_time", "device_time", "source")


def format_value(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


class CsvExporter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.columns: list[str] = []
        self.rows = 0
        self.rewrites = 0
        self._known: set[str] = set()
        self._f = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._f, lineterminator="\n")
        self._writer.writerow(FIXED_COLUMNS)
        self._f.flush()

    def write(self, sample: Sample) -> None:
        if self._f.closed:
            raise ValueError("CSV export is closed")
        new = [name for name in sample.values if name not in self._known]
        if new:
            self.columns.extend(new)
            self._known.update(new)
            if self.rows:
                self._rewrite()
            else:
                self._restart_header()
        row = [
            f"{sample.t:.6f}",
            "" if sample.device_t is None else f"{sample.device_t:.6f}",
            sample.source,
        ]
        row += [format_value(sample.values.get(name)) for name in self.columns]
        self._writer.writerow(row)
        self._f.flush()
        self.rows += 1

    def _header(self) -> list[str]:
        return [*FIXED_COLUMNS, *self.columns]

    def _restart_header(self) -> None:
        self._f.seek(0)
        self._f.truncate()
        self._writer.writerow(self._header())

    def _rewrite(self) -> None:
        """Re-emit the file with the wider header, padding existing rows."""
        self._f.close()
        with self.path.open(newline="", encoding="utf-8") as f:
            old = list(csv.reader(f))
        width = len(self._header())
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(self._header())
            for row in old[1:]:
                writer.writerow(row + [""] * (width - len(row)))
        os.replace(tmp, self.path)
        self._f = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._f, lineterminator="\n")
        self.rewrites += 1

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def __enter__(self) -> CsvExporter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def write_snapshot(channels: ChannelSet, path: str | Path) -> int:
    """Write the buffered points of every channel as ``channel,time,value`` rows."""
    count = 0
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["channel", "time", "value"])
        for name, channel in channels.channels.items():
            for t, v in zip(channel.buffer.times(), channel.buffer.values(), strict=True):
                writer.writerow([name, f"{t:.6f}", format_value(v)])
                count += 1
    return count
