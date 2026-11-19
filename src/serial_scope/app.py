"""GUI application logic without Tk.

:class:`AppController` owns the transport, the reader thread and the session. The Tk
view calls its methods from button handlers and calls :meth:`AppController.poll` from a
timer; ``poll`` returns a :class:`UiUpdate` describing what to draw. Keeping this out of
``gui.py`` means the behaviour behind every button is unit-tested headlessly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from serial_scope.channels import DEFAULT_BUFFER
from serial_scope.connection import CONNECTED, FINISHED, ReconnectingSource, StatusEvent
from serial_scope.events import (
    INFO,
    MARK,
    RX,
    TX,
    Chunk,
    DecodeError,
    Notice,
    TextLine,
    TriggerFired,
)
from serial_scope.plotmodel import format_value
from serial_scope.reader import ReaderThread
from serial_scope.session import Session
from serial_scope.terminal import TextRenderer, encode_input, hexdump
from serial_scope.transport import (
    ReplayTransport,
    SerialTransport,
    TcpTransport,
    Transport,
)
from serial_scope.triggers import TriggerSet, parse_trigger

BAUD_RATES = (
    300,
    1200,
    2400,
    4800,
    9600,
    19200,
    38400,
    57600,
    74880,
    115200,
    230400,
    250000,
    460800,
    500000,
    921600,
    1000000,
    2000000,
)
"""74880 is the ESP8266 boot ROM rate; 250000 and above are common for fast telemetry."""

MAX_TERMINAL_CHARS = 200_000


@dataclass
class UiUpdate:
    """What changed since the last poll."""

    terminal: list[tuple[str, str]] = field(default_factory=list)  # (text, tag)
    status: list[str] = field(default_factory=list)
    new_channels: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    beeps: int = 0
    chunks: int = 0
    connected: bool | None = None
    finished: bool = False


class AppController:
    def __init__(
        self,
        decoder: str = "auto",
        buffer_len: int = DEFAULT_BUFFER,
        time_base: str = "host",
        snapshot_dir: str | Path = ".",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.decoder_name = decoder
        self.buffer_len = buffer_len
        self.time_base = time_base
        self.snapshot_dir = Path(snapshot_dir)
        self.clock = clock
        self.triggers = TriggerSet()
        self.session = self._new_session()
        self.reader: ReaderThread | None = None
        self.source: ReconnectingSource | None = None
        self.hex_mode = False
        self._renderer = TextRenderer()
        self._hex_offset = 0
        self.is_replay = False

    def _new_session(self) -> Session:
        return Session(
            self.decoder_name,
            buffer_len=self.buffer_len,
            time_base=self.time_base,
            triggers=self.triggers,
            snapshot_dir=self.snapshot_dir,
        )

    # -- connection --------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.reader is not None and self.reader.is_alive()

    @property
    def connected(self) -> bool:
        return self.source is not None and self.source.state == CONNECTED

    def start(self, transport: Transport, auto_reconnect: bool = True) -> None:
        """Start reading ``transport`` in the background, replacing any current source."""
        self.disconnect()
        self.is_replay = isinstance(transport, ReplayTransport)
        self.source = ReconnectingSource(
            transport, auto_reconnect=auto_reconnect and not self.is_replay
        )
        self.reader = ReaderThread(self.source)
        self.reader.start()

    def connect_serial(self, port: str, baudrate: int) -> None:
        self.start(SerialTransport(port, baudrate))

    def connect_tcp(self, host: str, port: int) -> None:
        self.start(TcpTransport(host, port))

    def open_replay(self, path: str | Path, speed: float = 1.0) -> None:
        """Replay a log into a fresh session (channels and counters start empty)."""
        self.disconnect()
        self.reset()
        self.start(ReplayTransport(path, speed), auto_reconnect=False)

    def disconnect(self) -> None:
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.stop()
        self.source = None

    def reset(self) -> None:
        """Forget channels, counters and terminal state; keep triggers and settings.

        A running recording or CSV export is closed (the files stay complete).
        """
        self.session.close()
        self.session = self._new_session()
        self._renderer = TextRenderer()
        self._hex_offset = 0

    def set_decoder(self, name: str) -> None:
        self.decoder_name = name
        self.reset()

    # -- user actions ------------------------------------------------------------------

    def send(self, text: str, line_ending: str = "LF", hex_mode: bool = False) -> bytes:
        """Send the send-box contents; returns the bytes sent. Raises ValueError on bad input."""
        data = encode_input(text, line_ending, hex_mode)
        if self.reader is None or self.is_replay:
            raise ValueError("not connected")
        if not self.reader.write(data):
            raise ValueError("device is not connected; nothing was sent")
        self.session.record_tx(data, self.clock())
        return data

    def start_recording(self, path: str | Path, max_mb: float | None = None) -> Path:
        meta: dict[str, object] = {}
        if self.source is not None:
            meta["source"] = self.source.transport.name
        max_bytes = int(max_mb * 1024 * 1024) if max_mb else None
        writer = self.session.start_logging(path, meta, max_bytes=max_bytes)
        return writer.path

    def stop_recording(self) -> None:
        self.session.stop_logging()

    @property
    def recording(self) -> bool:
        return self.session.logger is not None

    def add_trigger(self, spec: str) -> str:
        trigger = parse_trigger(spec, holdoff=0.5)
        self.triggers.add(trigger)
        return trigger.label

    def mark(self, label: str = "mark") -> None:
        self.session.mark(label, self.clock())

    def snapshot(self) -> Path:
        return self.session.snapshot(self.clock())

    # -- polling -----------------------------------------------------------------------

    def poll(self, max_items: int = 2000) -> UiUpdate:
        """Process what the reader has queued since the last call."""
        update = UiUpdate()
        if self.reader is None:
            return update
        before = set(self.session.channels.names())
        for item in self.reader.drain(max_items):
            if isinstance(item, StatusEvent):
                update.status.append(item.message)
                update.terminal.append((f"[{item.message}]\n", "info"))
                if item.state == FINISHED:
                    update.finished = True
                update.connected = item.state == CONNECTED
                self.session.record_info(item.message, item.t)
                continue
            self._chunk(item, update)
        if self.reader is not None and not self.reader.is_alive() and self.reader.queue.empty():
            update.finished = True
        update.new_channels = [n for n in self.session.channels.names() if n not in before]
        update.beeps = self.session.pending_beeps
        self.session.pending_beeps = 0
        return update

    def _chunk(self, chunk: Chunk, update: UiUpdate) -> None:
        update.chunks += 1
        events = self.session.process(chunk)
        active = self.session.active_decoder
        binary = active is not None and active.binary
        if chunk.direction == RX:
            if binary and not self.hex_mode:
                # COBS frames rendered as text are noise; show what the frames said instead.
                for event in events:
                    if isinstance(event, TextLine):
                        update.terminal.append((event.text + "\n", "rx"))
            elif self.hex_mode:
                lines = hexdump(chunk.data, self._hex_offset)
                self._hex_offset += len(chunk.data)
                update.terminal.append(("\n".join(lines) + "\n", "rx"))
            else:
                text = self._renderer.feed(chunk.data)
                if text:
                    update.terminal.append((text, "rx"))
        elif chunk.direction == TX and self.is_replay:
            update.terminal.append((f"> {chunk.data!r}\n", "tx"))
        elif chunk.direction in (MARK, INFO):
            label = chunk.data.decode("utf-8", "replace")
            update.terminal.append((f"[{chunk.direction}: {label}]\n", "info"))
        for event in events:
            if isinstance(event, DecodeError):
                update.events.append(f"{_clock_str(event.t)}  error  {event.kind}: {event.detail}")
            elif isinstance(event, Notice):
                update.events.append(f"{_clock_str(event.t)}  notice  {event.kind}: {event.detail}")
            elif isinstance(event, TriggerFired):
                update.events.append(
                    f"{_clock_str(event.t)}  trigger  {event.trigger}: {event.detail}"
                    f"  [{', '.join(event.actions)}]"
                )

    # -- tables ------------------------------------------------------------------------

    def stats_rows(self) -> list[tuple[str, str, str, str, str, str, str]]:
        rows = []
        for channel in self.session.channels.channels.values():
            s = channel.summary()
            rows.append(
                (
                    s.name,
                    format_value(s.last),
                    format_value(s.min),
                    format_value(s.max),
                    format_value(s.mean),
                    f"{s.rate_hz:.1f}",
                    str(s.count),
                )
            )
        return rows

    def counter_rows(self) -> list[tuple[str, str]]:
        totals = self.session.totals
        rows = [
            ("decoder", self.session.decoder_name),
            ("bytes received", str(totals.rx_bytes)),
            ("bytes sent", str(totals.tx_bytes)),
            ("samples", str(totals.samples)),
            ("text lines", str(totals.text_lines)),
        ]
        rows += [(f"error: {k}", str(v)) for k, v in self.session.error_counts().items()]
        rows += [(f"sequence: {k}", str(v)) for k, v in self.session.sequence_counts().items()]
        rows += [(f"radio link: {k}", str(v)) for k, v in self.session.link_counts().items()]
        rows += [(f"notices: {k}", str(v)) for k, v in sorted(totals.notices.items())]
        if self.source is not None:
            rows.append(("reconnects", str(max(0, self.source.connects - 1))))
            rows.append(("link drops", str(self.source.drops)))
        rows.append(("markers", str(len(self.session.markers))))
        for trigger in self.triggers.triggers:
            rows.append((f"trigger: {trigger.label}", f"fired {trigger.fired}"))
        if self.session.logger is not None:
            logger = self.session.logger
            rows.append(("recording", f"{logger.paths[-1].name} ({logger.bytes_written} B)"))
        return rows

    def close(self) -> None:
        self.disconnect()
        self.session.close()


def _clock_str(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"
