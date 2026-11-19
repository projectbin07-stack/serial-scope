"""A monitoring session: decoding, channels, logging, CSV export and triggers in one place.

Both the CLI and the GUI drive a :class:`Session` with the chunks their transport
delivers. The session never touches the transport itself, which keeps it synchronous
and easy to test.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from serial_scope.channels import DEFAULT_BUFFER, ChannelSet
from serial_scope.csvexport import CsvExporter, write_snapshot
from serial_scope.decoders import Decoder, create_decoder
from serial_scope.decoders.auto import AutoDecoder
from serial_scope.events import (
    INFO,
    MARK,
    RX,
    TX,
    Chunk,
    DecodeError,
    Event,
    Notice,
    Sample,
    TriggerFired,
)
from serial_scope.logfile import LogWriter
from serial_scope.triggers import TriggerSet

SEQUENCE_KEYS = ("dropped", "lost", "restarts", "gateway_restarts")
"""Decoder counters reported as sequence problems: frames missing by sequence number
(``dropped`` / ``lost``) and device restarts."""


@dataclass
class Marker:
    t: float
    label: str


@dataclass
class SessionTotals:
    rx_bytes: int = 0
    tx_bytes: int = 0
    chunks: int = 0
    samples: int = 0
    text_lines: int = 0
    first_t: float | None = None
    last_t: float | None = None
    errors: Counter[str] = field(default_factory=Counter)
    notices: Counter[str] = field(default_factory=Counter)


class Session:
    def __init__(
        self,
        decoder: Decoder | str = "auto",
        *,
        buffer_len: int = DEFAULT_BUFFER,
        time_base: str = "host",
        triggers: TriggerSet | None = None,
        snapshot_dir: str | Path = ".",
    ) -> None:
        self.decoder = create_decoder(decoder) if isinstance(decoder, str) else decoder
        self.channels = ChannelSet(buffer_len, time_base)
        self.triggers = triggers or TriggerSet()
        self.snapshot_dir = Path(snapshot_dir)
        self.logger: LogWriter | None = None
        self.csv: CsvExporter | None = None
        self.totals = SessionTotals()
        self.markers: list[Marker] = []
        self.snapshots: list[Path] = []
        self.pending_beeps = 0
        self.stop_logging_at: float | None = None

    # -- recording ---------------------------------------------------------------------

    def start_logging(
        self,
        path: str | Path,
        meta: dict[str, Any] | None = None,
        max_bytes: int | None = None,
        start_time: float | None = None,
    ) -> LogWriter:
        self.stop_logging()
        info = {"decoder": self.decoder_name, "tool": "serial-scope"}
        info.update(meta or {})
        self.logger = LogWriter(path, info, max_bytes=max_bytes, start_time=start_time)
        self.stop_logging_at = None
        return self.logger

    def stop_logging(self) -> None:
        if self.logger is not None:
            self.logger.close()
            self.logger = None
        self.stop_logging_at = None

    def start_csv(self, path: str | Path) -> CsvExporter:
        self.stop_csv()
        self.csv = CsvExporter(path)
        return self.csv

    def stop_csv(self) -> None:
        if self.csv is not None:
            self.csv.close()
            self.csv = None

    @property
    def active_decoder(self) -> Decoder | None:
        """The decoder doing the work: the forced one, or auto's choice (None while detecting)."""
        d = self.decoder
        if isinstance(d, AutoDecoder):
            return d.chosen
        return d

    @property
    def decoder_name(self) -> str:
        d = self.decoder
        if isinstance(d, AutoDecoder):
            return f"auto ({d.chosen.name})" if d.chosen else "auto (detecting)"
        return d.name

    # -- processing --------------------------------------------------------------------

    def process(self, chunk: Chunk) -> list[Event]:
        """Handle one chunk from the transport; return the resulting events."""
        self._check_stop(chunk.t)
        if self.logger is not None:
            self.logger.write(chunk)
        totals = self.totals
        totals.chunks += 1
        if totals.first_t is None:
            totals.first_t = chunk.t
        totals.last_t = chunk.t
        if chunk.direction == TX:
            totals.tx_bytes += len(chunk.data)
            return []
        if chunk.direction == MARK:
            self.markers.append(Marker(chunk.t, chunk.data.decode("utf-8", "replace")))
            return []
        if chunk.direction == INFO:
            return []
        assert chunk.direction == RX
        totals.rx_bytes += len(chunk.data)
        return self._handle(self.decoder.feed(chunk.data, chunk.t))

    def finish(self, t: float | None = None) -> list[Event]:
        """Flush the decoder at the end of the stream."""
        if t is None:
            t = self.totals.last_t if self.totals.last_t is not None else time.time()
        return self._handle(self.decoder.flush(t))

    def _handle(self, events: list[Event]) -> list[Event]:
        out: list[Event] = []
        for event in events:
            out.append(event)
            if isinstance(event, Sample):
                self.totals.samples += 1
                self.channels.add_sample(event)
                if self.csv is not None:
                    self.csv.write(event)
            elif isinstance(event, DecodeError):
                self.totals.errors[event.kind] += 1
            elif isinstance(event, Notice):
                self.totals.notices[event.kind] += 1
            elif not isinstance(event, TriggerFired):
                self.totals.text_lines += 1
            for fired in self.triggers.evaluate(event):
                out.append(fired)
                self._run_actions(fired)
        return out

    def _run_actions(self, fired: TriggerFired) -> None:
        for action in fired.actions:
            if action == "mark":
                self.mark(f"{fired.trigger}: {fired.detail}", fired.t)
            elif action == "beep":
                self.pending_beeps += 1
            elif action.startswith("stop"):
                seconds = float(action.partition(":")[2] or 0)
                due = fired.t + seconds
                if self.stop_logging_at is None or due < self.stop_logging_at:
                    self.stop_logging_at = due
                self._check_stop(fired.t)
            elif action == "snapshot":
                self.snapshot(fired.t)

    def _check_stop(self, t: float) -> None:
        if self.stop_logging_at is not None and t >= self.stop_logging_at:
            if self.logger is not None:
                self.logger.write(Chunk(b"logging stopped by trigger", t, INFO))
            self.stop_logging()

    def mark(self, label: str, t: float | None = None) -> Marker:
        """Add a marker; it is stored in the log so replays show it too."""
        marker = Marker(time.time() if t is None else t, label)
        self.markers.append(marker)
        if self.logger is not None:
            self.logger.write(Chunk(label.encode("utf-8"), marker.t, MARK))
        return marker

    def snapshot(self, t: float | None = None) -> Path:
        """Write the channel buffers to ``snapshot-<time>.csv`` in :attr:`snapshot_dir`."""
        t = time.time() if t is None else t
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
        millis = int((t % 1) * 1000)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.snapshot_dir / f"snapshot-{stamp}-{millis:03d}.csv"
        write_snapshot(self.channels, path)
        self.snapshots.append(path)
        return path

    def record_tx(self, data: bytes, t: float | None = None) -> None:
        """Account for (and log) bytes sent to the device."""
        self.process(Chunk(data, time.time() if t is None else t, TX))

    def record_info(self, text: str, t: float | None = None) -> None:
        if self.logger is not None:
            self.logger.write(Chunk(text.encode("utf-8"), time.time() if t is None else t, INFO))

    def error_counts(self) -> dict[str, int]:
        """Decode errors by kind (crc, cobs, overlong, ...)."""
        return {k: v for k, v in sorted(self.decoder.errors.items()) if v}

    def sequence_counts(self) -> dict[str, int]:
        """Frames missing by sequence number (``dropped`` / ``lost``) and device restarts."""
        counts = self.decoder.counts
        return {k: counts[k] for k in SEQUENCE_KEYS if counts.get(k)}

    def link_counts(self) -> dict[str, int]:
        """Link-quality counters reported by the device (empty for most formats)."""
        return dict(self.decoder.link_counts)

    def close(self) -> None:
        self.stop_logging()
        self.stop_csv()
