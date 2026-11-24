"""Command-line interface: list, monitor, replay, stats, send, demo, decoders, gui."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, TextIO

from serial_scope import __version__
from serial_scope.commands import (
    REPEAT_HZ,
    Command,
    CommandError,
    OdometryCommander,
    parse_action,
)
from serial_scope.connection import ReconnectingSource
from serial_scope.decoders import available_decoders
from serial_scope.decoders.odometry import (
    PARAM_STATUS,
    PROTOCOL_VERSION,
    PROTOCOL_VERSION_PARAM,
    Message,
    OdometryDecoder,
)
from serial_scope.demo import DEMO_KINDS, write_demo_log
from serial_scope.events import RX, DecodeError, Event, Notice, Sample, TextLine, TriggerFired
from serial_scope.logfile import LogFormatError, LogReader, log_parts
from serial_scope.ports import list_ports
from serial_scope.session import Session
from serial_scope.terminal import encode_input, hexdump
from serial_scope.transport import ReplayTransport, Transport, TransportError, parse_source
from serial_scope.triggers import TriggerSet, TriggerSpecError, parse_trigger

SHOW_MODES = ("decoded", "text", "samples", "raw", "hex", "none")

DEFAULT_BAUD = 115200


class UsageError(ValueError):
    """A combination of command-line options that cannot work."""


# encoder-odometry-firmware: 921600 on ESP32 / ESP32-S3, 460800 on NUCLEO-F446RE.
ODOMETRY_BAUD = 921600
# Baud used when -b is not given, by decoder. ina219-battery-monitor and
# nrf24-telemetry-link run at 115200; auto-detect cannot know the rate, so it uses 115200.
DECODER_BAUD = {"odometry": ODOMETRY_BAUD}
ODOMETRY_BAUD_NOTE = (
    "encoder-odometry-firmware runs at 921600 on ESP32/ESP32-S3 and 460800 on "
    "NUCLEO-F446RE (pass -b 460800 there)"
)
FIRMWARE_BAUD_NOTE = (
    ODOMETRY_BAUD_NOTE + "; ina219-battery-monitor and nrf24-telemetry-link run at 115200"
)


def default_baud(decoder: str | None) -> int:
    """Baud rate used when the user gives no ``-b``: by decoder, else 115200."""
    return DECODER_BAUD.get(decoder or "", DEFAULT_BAUD)


def resolve_baud(args: argparse.Namespace, decoder: str | None) -> int:
    """Fill in ``args.baud`` if it was not given and record whether it was a default."""
    if getattr(args, "baud", None) is None:
        args.baud = default_baud(decoder)
        args.baud_defaulted = True
    elif not hasattr(args, "baud_defaulted"):
        args.baud_defaulted = False
    baud: int = args.baud
    return baud


def baud_hint(args: argparse.Namespace, note: str = FIRMWARE_BAUD_NOTE) -> str:
    """Text naming the baud rate in use, appended to link-level failure messages."""
    baud = getattr(args, "baud", None)
    if baud is None:
        return ""
    how = "the default" if getattr(args, "baud_defaulted", False) else "from -b"
    return f" (port opened at {baud} baud, {how}; {note})"


class _Clock:
    """Formats event times as an offset from the first event, or as wall-clock time."""

    def __init__(self, absolute: bool) -> None:
        self.absolute = absolute
        self.origin: float | None = None

    def __call__(self, t: float) -> str:
        if self.absolute:
            stamp = datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]
            return stamp
        if self.origin is None:
            self.origin = t
        return f"+{t - self.origin:9.3f}s"


def _fmt_number(value: float) -> str:
    return f"{value:.6g}"


def format_event(event: Event, clock: _Clock) -> str:
    stamp = clock(event.t)
    if isinstance(event, Sample):
        values = " ".join(f"{k}={_fmt_number(v)}" for k, v in event.values.items())
        return f"{stamp}  {values}"
    if isinstance(event, TextLine):
        return f"{stamp}  {event.text}"
    if isinstance(event, DecodeError):
        return f"{stamp}  ! {event.kind}: {event.detail}"
    if isinstance(event, Notice):
        return f"{stamp}  ~ {event.kind}: {event.detail}"
    return f"{stamp}  * trigger {event.trigger}: {event.detail} [{', '.join(event.actions)}]"


def _wants(event: Event, show: str) -> bool:
    if show == "decoded":
        return True
    if show == "text":
        return not isinstance(event, Sample)
    if show == "samples":
        return not isinstance(event, TextLine)
    return False


def _build_triggers(specs: Sequence[str], hysteresis: float, holdoff: float) -> TriggerSet:
    return TriggerSet([parse_trigger(spec, hysteresis, holdoff) for spec in specs])


def _make_session(args: argparse.Namespace) -> Session:
    triggers = _build_triggers(args.trigger, args.hysteresis, args.holdoff)
    session = Session(
        args.decode,
        buffer_len=args.buffer,
        time_base=args.time_base,
        triggers=triggers,
        snapshot_dir=args.snapshot_dir,
    )
    if args.log:
        max_bytes = int(args.rotate_mb * 1024 * 1024) if args.rotate_mb else None
        session.start_logging(args.log, {"source": args.source}, max_bytes=max_bytes)
    if args.csv:
        session.start_csv(args.csv)
    return session


def _emit(events: list[Event], args: argparse.Namespace, clock: _Clock, out: TextIO) -> None:
    for event in events:
        if _wants(event, args.show):
            out.write(format_event(event, clock) + "\n")
        if isinstance(event, TriggerFired) and "beep" in event.actions and args.show != "none":
            out.write("\a")


def run_stream(
    transport: Transport,
    args: argparse.Namespace,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Shared loop of ``monitor`` and ``replay``."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    session = _make_session(args)
    source = ReconnectingSource(
        transport,
        auto_reconnect=not args.no_reconnect and not isinstance(transport, ReplayTransport),
    )
    clock = _Clock(args.abs_time)
    deadline = time.monotonic() + args.duration if args.duration else None
    hex_offset = 0
    status = 0
    try:
        if args.send is not None:
            # Open first so the command goes out as soon as the port is up.
            while not transport.is_open and not source.finished:
                source.read(0.05)
                for ev in source.drain_events():
                    err.write(f"[{ev.message}]\n")
                if deadline is not None and time.monotonic() >= deadline:
                    break
            if transport.is_open:
                data = encode_input(args.send, "None")
                if source.write(data):
                    session.record_tx(data)
        while True:
            chunk = source.read(0.05)
            for ev in source.drain_events():
                err.write(f"[{ev.message}]\n")
                session.record_info(ev.message, ev.t)
            if chunk is not None:
                if args.show == "raw" and chunk.direction == RX:
                    out.write(chunk.data.decode("utf-8", "replace"))
                elif args.show == "hex" and chunk.direction == RX:
                    for line in hexdump(chunk.data, hex_offset):
                        out.write(line + "\n")
                    hex_offset += len(chunk.data)
                _emit(session.process(chunk), args, clock, out)
                out.flush()
            if source.finished:
                if source.connects == 0:
                    status = 2
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        err.write("[interrupted]\n")
    finally:
        _emit(session.finish(), args, clock, out)
        source.close()
        session.close()
    if not args.quiet:
        err.write(summary_text(session) + "\n")
    errors = sum(session.totals.errors.values())
    if getattr(args, "baud", None) is not None and errors > 0 and session.totals.samples == 0:
        err.write(
            f"warning: {errors} decode errors and no frame decoded; "
            f"is the baud rate right?{baud_hint(args)}\n"
        )
    if args.max_errors is not None and errors > args.max_errors:
        err.write(f"{errors} decode errors exceed --max-errors {args.max_errors}\n")
        return 3
    return status


def summary_text(session: Session) -> str:
    totals = session.totals
    duration = (
        totals.last_t - totals.first_t
        if totals.first_t is not None and totals.last_t is not None
        else 0.0
    )
    errors = session.error_counts()
    error_text = ", ".join(f"{k} {v}" for k, v in errors.items()) or "none"
    lines = [
        f"decoder {session.decoder_name}; {totals.rx_bytes} bytes in {duration:.3f} s; "
        f"{totals.samples} samples, {totals.text_lines} text lines; errors: {error_text}",
    ]
    sequence = session.sequence_counts()
    if sequence:
        lines.append("sequence: " + ", ".join(f"{k} {v}" for k, v in sequence.items()))
    link = session.link_counts()
    if link:
        lines.append(f"radio link (device totals): {_counts_text(link)}")
    if totals.notices:
        notices = ", ".join(f"{k} {v}" for k, v in sorted(totals.notices.items()))
        lines.append(f"notices: {notices}")
    if session.markers:
        lines.append(f"{len(session.markers)} markers")
    if session.snapshots:
        lines.append("snapshots: " + ", ".join(str(p) for p in session.snapshots))
    return "\n".join(lines)


def _counts_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items())


# -- commands ----------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    ports = list_ports()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "device": p.device,
                        "usb_id": p.usb_id or None,
                        "description": p.description,
                        "manufacturer": p.manufacturer,
                        "serial_number": p.serial_number,
                        "hint": p.hint or None,
                    }
                    for p in ports
                ],
                indent=2,
            )
        )
        return 0
    if not ports:
        print("no serial ports found")
        return 0
    width = max(len(p.device) for p in ports)
    for p in ports:
        usb = p.usb_id or "-"
        line = f"{p.device:<{width}}  {usb:<9}  {p.description}"
        if p.hint:
            line += f"  [{p.hint}]"
        print(line)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    resolve_baud(args, args.decode)
    try:
        transport = parse_source(args.source, args.baud)
    except (ValueError, LogFormatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return run_stream(transport, args)


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        transport = ReplayTransport(args.source, args.speed)
    except (ValueError, LogFormatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.no_reconnect = True
    args.send = None
    return run_stream(transport, args)


@dataclass
class ChannelStats:
    name: str
    count: int
    min: float | None
    max: float | None
    mean: float | None
    rate_hz: float


@dataclass
class LogStats:
    files: list[str]
    bytes_on_disk: int
    meta: dict[str, Any]
    truncated: bool
    start: float | None
    end: float | None
    records: dict[str, int]
    rx_bytes: int
    decoder: str
    samples: int
    text_lines: int
    errors: dict[str, int]
    sequence: dict[str, int]
    link: dict[str, int]
    notices: dict[str, int]
    markers: list[dict[str, Any]]
    channels: list[ChannelStats] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return self.end - self.start

    @property
    def decode_errors(self) -> int:
        return sum(self.errors.values())

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["duration_s"] = self.duration_s
        return out


def log_stats(path: str, decoder: str = "auto") -> LogStats:
    """Decode a whole log and summarise it (used by ``stats``)."""
    reader = LogReader(path)
    session = Session(decoder, buffer_len=16)
    records = {"rx": 0, "tx": 0, "mark": 0, "info": 0}
    rx_bytes = 0
    first: float | None = None
    last: float | None = None
    for chunk in reader:
        records[chunk.direction] += 1
        if chunk.direction == RX:
            rx_bytes += len(chunk.data)
        first = chunk.t if first is None else first
        last = chunk.t
        session.process(chunk)
    session.finish()
    channels = [
        ChannelStats(
            ch.name,
            ch.stats.count,
            ch.stats.min if ch.stats.count else None,
            ch.stats.max if ch.stats.count else None,
            ch.stats.mean if ch.stats.count else None,
            round(ch.overall_rate(), 3),
        )
        for ch in session.channels.channels.values()
    ]
    return LogStats(
        files=[str(p) for p in reader.info.paths],
        bytes_on_disk=sum(p.stat().st_size for p in reader.info.paths),
        meta=reader.info.meta,
        truncated=reader.info.truncated,
        start=first,
        end=last,
        records=records,
        rx_bytes=rx_bytes,
        decoder=session.decoder_name,
        samples=session.totals.samples,
        text_lines=session.totals.text_lines,
        errors=session.error_counts(),
        sequence=session.sequence_counts(),
        link=session.link_counts(),
        notices=dict(sorted(session.totals.notices.items())),
        markers=[{"t": m.t, "label": m.label} for m in session.markers],
        channels=channels,
    )


def _iso(t: float | None) -> str:
    if t is None:
        return "-"
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.6g}"


def cmd_stats(args: argparse.Namespace) -> int:
    try:
        info = log_stats(args.log, args.decode)
    except (ValueError, LogFormatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(info.to_json(), indent=2))
    else:
        rec = info.records
        print(
            f"file        {info.files[0]}  ({len(info.files)} part(s), {info.bytes_on_disk} bytes)"
        )
        if info.meta.get("source"):
            synthetic = "  (synthetic demo data)" if info.meta.get("synthetic") else ""
            print(f"source      {info.meta['source']}{synthetic}")
        print(f"time        {_iso(info.start)} .. {_iso(info.end)}")
        print(f"duration    {info.duration_s:.3f} s")
        print(
            f"records     rx {rec['rx']} ({info.rx_bytes} bytes), tx {rec['tx']}, "
            f"marks {rec['mark']}, info {rec['info']}" + ("  [TRUNCATED]" if info.truncated else "")
        )
        print(f"decoder     {info.decoder}")
        print(f"decoded     {info.samples} samples, {info.text_lines} text lines")
        errors = ", ".join(f"{k} {v}" for k, v in info.errors.items()) or "none"
        print(f"errors      {errors}")
        if info.sequence:
            print("sequence    " + ", ".join(f"{k} {v}" for k, v in info.sequence.items()))
        if info.link:
            print(f"radio link  {_counts_text(info.link)}  (device totals)")
        if info.notices:
            print(f"notices     {_counts_text(info.notices)}")
        for marker in info.markers:
            print(f"marker      {_iso(marker['t'])}  {marker['label']}")
        if info.channels:
            width = max(10, *(len(c.name) for c in info.channels))
            print()
            print(
                f"{'channel':<{width}}  {'count':>6}  {'min':>10}  {'max':>10}  "
                f"{'mean':>10}  {'rate Hz':>7}"
            )
            for c in info.channels:
                print(
                    f"{c.name:<{width}}  {c.count:>6}  {_num(c.min):>10}  {_num(c.max):>10}  "
                    f"{_num(c.mean):>10}  {c.rate_hz:>7.1f}"
                )
    if args.max_errors is not None and info.decode_errors > args.max_errors:
        return 3
    return 0


def cmd_decoders(args: argparse.Namespace) -> int:
    for name, description in available_decoders().items():
        print(f"{name:<10} {description}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    count = write_demo_log(args.out, args.kind, args.seconds)
    parts = log_parts(args.out)
    size = sum(p.stat().st_size for p in parts)
    print(
        f"wrote {args.out}: synthetic '{args.kind}' session, {args.seconds:g} s, "
        f"{count} chunks, {size} bytes"
    )
    return 0


# -- send --------------------------------------------------------------------------------

EXIT_PARAM_REJECTED = 1
EXIT_NO_REPLY = 4
EXIT_VERSION = 5
VERSION_TIMEOUT_S = 1.0


def _send_plan(
    args: argparse.Namespace, commander: OdometryCommander
) -> list[tuple[float, Command]]:
    """The frames to send and when (seconds after the first)."""
    first = parse_action(list(args.odometry), commander)
    plan = [(0.0, first)]
    if args.hold:
        if args.odometry[0] != "vel":
            raise CommandError("--hold only applies to vel")
        vx, vy, wz = (float(v) for v in args.odometry[1:4])
        period = 1.0 / REPEAT_HZ
        count = max(1, round(args.hold * REPEAT_HZ))
        plan += [(i * period, commander.velocity(vx, vy, wz)) for i in range(1, count)]
        plan.append((args.hold, commander.stop()))
    return plan


def _frame_line(t: float, command: Command) -> str:
    return f"+{t:9.3f}s  > {command.text}  seq {command.seq}  [{command.frame.hex(' ')}]"


def run_send(
    transport: Transport | None,
    args: argparse.Namespace,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``serial-scope send``: write odometry command frames and read the reply.

    Before a velocity command the firmware's protocol version (parameter 22) is read,
    and nothing that moves the motors is sent unless it is the version this encoder
    implements, as the protocol document asks of a host (``--no-version-check`` skips it).

    Exit status: 0 sent (and, for parameters, answered with status ok), 1 the firmware
    rejected a parameter request, 2 bad arguments or the port failed, 4 no PARAM_VALUE
    reply arrived in time, 5 the protocol version was not confirmed, 130 interrupted (a
    stop frame is sent after a vel).
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    commander = OdometryCommander(args.seq)
    check_version = (
        transport is not None and args.odometry[:1] == ["vel"] and not args.no_version_check
    )
    try:
        version_get = commander.param_get(PROTOCOL_VERSION_PARAM) if check_version else None
        plan = _send_plan(args, commander)
    except CommandError as exc:
        err.write(f"error: {exc}\n")
        return 2
    if transport is None:
        for t, command in plan:
            out.write(_frame_line(t, command) + "\n")
        return 0
    first = plan[0][1]
    is_motion = first.name == "velocity_command"
    wait = args.wait if args.wait is not None else (0.2 if is_motion else 1.0)
    decoder = OdometryDecoder()
    session = Session(decoder)
    if args.log:
        session.start_logging(args.log, {"source": args.source, "command": "send"})
    clock = _Clock(args.abs_time)
    try:
        transport.open()
    except (TransportError, OSError) as exc:
        err.write(f"error: {exc}\n")
        session.close()
        return 2
    replies_before = decoder.counts["param_value"]
    reply: Message | None = None
    sent = 0
    status = 0
    start = time.monotonic()
    clock.origin = time.time()  # event times count from the first frame sent

    def pump(timeout: float) -> None:
        chunk = transport.read(timeout)
        if chunk is not None:
            for event in session.process(chunk):
                if isinstance(event, (TextLine, DecodeError)):
                    out.write(format_event(event, clock) + "\n")

    try:
        if version_get is not None:
            transport.write(version_get.frame)
            session.record_tx(version_get.frame)
            out.write(_frame_line(0.0, version_get) + "\n")
            while decoder.protocol_version is None and time.monotonic() - start < VERSION_TIMEOUT_S:
                pump(0.05)
            if decoder.protocol_version != PROTOCOL_VERSION:
                found = decoder.protocol_version
                reason = (
                    f"no protocol_version reply within {VERSION_TIMEOUT_S:g} s"
                    + baud_hint(args, ODOMETRY_BAUD_NOTE)
                    if found is None
                    else f"firmware speaks protocol version {found}, not {PROTOCOL_VERSION}"
                )
                out.flush()
                err.write(f"not sending a velocity command: {reason}\n")
                status = EXIT_VERSION
                return status
            start = time.monotonic()
        while True:
            now = time.monotonic() - start
            while sent < len(plan) and plan[sent][0] <= now:
                t, command = plan[sent]
                transport.write(command.frame)
                session.record_tx(command.frame)
                if sent in (0, len(plan) - 1):
                    out.write(_frame_line(t, command) + "\n")
                elif sent == 1:
                    out.write(f"             (repeating every {1 / REPEAT_HZ:g} s)\n")
                sent += 1
            if reply is not None or (sent == len(plan) and now >= plan[-1][0] + wait):
                break
            due = plan[sent][0] - now if sent < len(plan) else 0.05
            pump(max(0.0, min(0.05, due)))
            last = decoder.last_param_value
            if (
                first.param_id is not None
                and last is not None
                and decoder.counts["param_value"] > replies_before
                and last.fields["id"] == first.param_id
            ):
                reply = last
    except KeyboardInterrupt:
        out.flush()
        err.write("[interrupted]\n")
        status = 130
        if is_motion:
            next_seq = plan[sent - 1][1].seq + 1 if sent else plan[0][1].seq
            stop = OdometryCommander(next_seq).stop()
            try:
                transport.write(stop.frame)
                session.record_tx(stop.frame)
                err.write(_frame_line(time.monotonic() - start, stop) + "\n")
            except (TransportError, OSError) as exc:
                err.write(f"error: stop not sent ({exc}); the firmware watchdog will stop\n")
    except (TransportError, OSError) as exc:
        err.write(f"error: {exc}\n")
        status = 2
    finally:
        out.flush()
        transport.close()
        session.close()
    if status:
        return status
    if is_motion:
        err.write(
            f"sent {sent} frame(s); the protocol has no acknowledgement for velocity commands\n"
        )
        return 0
    if reply is None:
        err.write(f"no PARAM_VALUE reply within {wait:g} s{baud_hint(args, ODOMETRY_BAUD_NOTE)}\n")
        return EXIT_NO_REPLY
    code = int(reply.fields["status"])
    if code != 0:
        err.write(f"firmware rejected the request: {PARAM_STATUS.get(code, code)}\n")
        return EXIT_PARAM_REJECTED
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    if args.dry_run:
        return run_send(None, args)
    if not args.source:
        print("error: SOURCE is required unless --dry-run is given", file=sys.stderr)
        return 2
    resolve_baud(args, "odometry")
    try:
        transport = parse_source(args.source, args.baud)
    except (ValueError, LogFormatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return run_send(transport, args)


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from serial_scope.gui import run_gui
    except ImportError as exc:  # pragma: no cover - depends on the Python build
        print(f"error: Tk is not available in this Python ({exc})", file=sys.stderr)
        return 2
    return run_gui(
        replay=args.replay,
        speed=args.speed,
        port=args.port,
        baud=resolve_baud(args, args.decode),
        decoder=args.decode,
        theme=args.theme,
        buffer_len=args.buffer,
    )


# -- parser ------------------------------------------------------------------------------


def _positive_float(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _add_stream_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--decode",
        default="auto",
        choices=list(available_decoders()),
        help="decoder (default: auto-detect)",
    )
    p.add_argument("--log", metavar="FILE.sslog", help="record every byte to a raw log")
    p.add_argument(
        "--rotate-mb", type=_positive_float, metavar="MB", help="start a new log part every MB"
    )
    p.add_argument("--csv", metavar="FILE.csv", help="export decoded samples as CSV")
    p.add_argument(
        "--show",
        choices=SHOW_MODES,
        default="decoded",
        help="what to print: decoded events (default), text lines only, samples only, "
        "raw bytes, a hex dump, or nothing",
    )
    p.add_argument(
        "--trigger",
        action="append",
        default=[],
        metavar="SPEC",
        help="e.g. 'current_ma > 500 -> mark,snapshot', 'rise:v@3.3 -> beep', "
        "'/panic/ -> mark,stop:2' (repeatable)",
    )
    p.add_argument("--hysteresis", type=_positive_float, default=0.0, help="threshold re-arm band")
    p.add_argument(
        "--holdoff", type=_positive_float, default=0.0, help="min seconds between firings"
    )
    p.add_argument("--snapshot-dir", default=".", help="where snapshot CSVs go (default: .)")
    p.add_argument("--buffer", type=int, default=5000, help="points kept per channel")
    p.add_argument(
        "--time-base",
        choices=("host", "device"),
        default="host",
        help="x axis for snapshots: host receive time or device time",
    )
    p.add_argument("--duration", type=_positive_float, help="stop after this many seconds")
    p.add_argument("--abs-time", action="store_true", help="print wall-clock times")
    p.add_argument(
        "--max-errors",
        type=int,
        metavar="N",
        help="exit with status 3 if there are more than N decode errors",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="no summary on stderr")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serial-scope",
        description="Serial monitor, logger and live plotter for structured telemetry.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list serial ports with USB VID:PID")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("monitor", help="read a serial port or TCP socket")
    p.add_argument("source", help="COM5, /dev/ttyUSB0, tcp://192.168.4.1:3333 or a pyserial URL")
    p.add_argument(
        "-b",
        "--baud",
        type=int,
        default=None,
        help="baud rate (default: 921600 with --decode odometry, else 115200)",
    )
    p.add_argument("--no-reconnect", action="store_true", help="exit when the link drops")
    p.add_argument("--send", metavar="TEXT", help="send TEXT once connected (\\n etc. allowed)")
    _add_stream_options(p)
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("replay", help="play back a .sslog file")
    p.add_argument("source", metavar="LOG", help="log file (rotated parts are followed)")
    p.add_argument(
        "--speed",
        type=_positive_float,
        default=0.0,
        help="1 = original pace, 4 = four times faster, 0 = as fast as possible (default)",
    )
    _add_stream_options(p)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("stats", help="summarise a .sslog file")
    p.add_argument("log")
    p.add_argument("--decode", default="auto", choices=list(available_decoders()))
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-errors", type=int, metavar="N", help="exit 3 if more decode errors")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "send",
        help="send a velocity or parameter command to encoder-odometry-firmware",
        description="Send a command frame to encoder-odometry-firmware and print the reply. "
        "WARNING: vel moves the motors. One vel command drives them until the firmware's "
        "watchdog stops them (watchdog_ms, default 500 ms); --hold repeats it and then "
        "sends stop.",
    )
    p.add_argument("source", nargs="?", help="COM5, /dev/ttyUSB0, tcp://host:port, loop://")
    p.add_argument(
        "-b",
        "--baud",
        type=int,
        default=None,
        help="baud rate (default 921600, as on ESP32; NUCLEO-F446RE firmware needs -b 460800)",
    )
    p.add_argument(
        "--odometry",
        nargs="+",
        required=True,
        metavar="WORD",
        help="vel VX VY WZ (m/s, m/s, rad/s) | stop | get-param NAME | set-param NAME VALUE",
    )
    p.add_argument(
        "--hold",
        type=_positive_float,
        default=0.0,
        metavar="S",
        help=f"vel only: repeat the command at {REPEAT_HZ:g} Hz for S seconds, then send stop",
    )
    p.add_argument(
        "--wait",
        type=_positive_float,
        metavar="S",
        help="read replies for S seconds after the last frame (default 1 for parameters, "
        "0.2 otherwise)",
    )
    p.add_argument(
        "--seq", type=int, default=0, help="sequence number of the first frame (default 0)"
    )
    p.add_argument("--log", metavar="FILE.sslog", help="record sent and received bytes")
    p.add_argument("--abs-time", action="store_true", help="print wall-clock times")
    p.add_argument("--dry-run", action="store_true", help="print the frames, send nothing")
    p.add_argument(
        "--no-version-check",
        action="store_true",
        help="vel: do not ask for the firmware's protocol version first",
    )
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("decoders", help="list the available decoders")
    p.set_defaults(func=cmd_decoders)

    p = sub.add_parser("demo", help="write a synthetic demo log (no hardware needed)")
    p.add_argument("out", help="output .sslog file")
    p.add_argument("--kind", choices=DEMO_KINDS, default="plotter")
    p.add_argument("--seconds", type=_positive_float, default=20.0)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("gui", help="open the graphical monitor")
    p.add_argument("--port", help="connect to this port on start")
    p.add_argument(
        "-b",
        "--baud",
        type=int,
        default=None,
        help="baud rate (default: 921600 with --decode odometry, else 115200)",
    )
    p.add_argument("--replay", metavar="LOG", help="replay this log on start")
    p.add_argument("--speed", type=_positive_float, default=1.0, help="replay speed")
    p.add_argument("--decode", default="auto", choices=list(available_decoders()))
    p.add_argument("--theme", choices=("light", "dark"), default="light")
    p.add_argument("--buffer", type=int, default=5000, help="points kept per channel")
    p.set_defaults(func=cmd_gui)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except (TriggerSpecError, UsageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return result
