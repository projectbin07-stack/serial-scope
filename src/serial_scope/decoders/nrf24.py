"""USB serial packets of the nrf24-telemetry-link gateway, and the messages inside them.

Wire format, as documented in docs/protocol.md of
https://github.com/projectbin07-stack/nrf24-telemetry-link (section 3)::

    wire   = COBS(packet) 0x00
    packet = version:u8 (=1) kind:u8 seq:u8 length:u8 body[length] crc:u16 (little-endian)

The CRC is CRC-16/CCITT-FALSE over ``version .. body``: the same packet layout as the
encoder-odometry-firmware protocol, with a different kind table and bodies of up to 255
bytes. ``message`` packets carry an application message (battery, odometry, IMU,
key/value or command, section 2 of that document) received from the radio node;
``link_stats`` packets carry the gateway's own radio link counters once a second.

This is an independent re-implementation; it is tested against the project's shared
vector file. Rejected packets are reported with one of these kinds: ``overlong``,
``cobs``, ``short``, ``crc``, ``version``, ``length``, ``kind`` (unknown packet kind) and
``message`` (an invalid application message inside a valid packet). Gaps in the
gateway-to-host sequence number are counted as ``lost``.

New radio-side errors in ``link_stats`` (CRC errors, malformed frames, lost best-effort
messages, retries, failed commands, resyncs) are reported as :class:`Notice` events and
the latest totals are kept in :attr:`Nrf24GatewayDecoder.link_counts`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from serial_scope.decoders.cobsframes import ClockUnwrapper, CobsFrameDecoder
from serial_scope.decoders.framing import CobsError, cobs_decode, cobs_encode, crc16_ccitt_false
from serial_scope.events import Event, Notice, Sample, TextLine

SERIAL_VERSION = 1
SCHEMA_VERSION = 1
MAX_BODY = 255
MAX_FRAME_BYTES = 263
"""COBS encoding of the largest packet (6 + 255 bytes) is 263 bytes."""

KINDS: dict[int, str] = {
    0x01: "message",
    0x02: "link_stats",
    0x03: "command_result",
    0x04: "log",
    0x81: "send_command",
    0x82: "request_stats",
}
KIND_CODES = {name: code for code, name in KINDS.items()}

MESSAGE_TYPES: dict[int, str] = {
    0x01: "battery",
    0x02: "odometry",
    0x03: "imu",
    0x04: "key_value",
    0x10: "command",
}
MESSAGE_CODES = {name: code for code, name in MESSAGE_TYPES.items()}
_FIXED_BODY = {
    "battery": "<HiiiHB",
    "odometry": "<Iiihhh",
    "imu": "<I3h3hh",
    "command": "<Bi",
}
_FIXED_FIELDS = {
    "battery": ("bus_mv", "current_ua", "power_uw", "charge_uah", "soc_permille", "flags"),
    "odometry": ("t_ms", "x_mm", "y_mm", "theta_mrad", "v_mm_s", "w_mrad_s"),
    "command": ("opcode", "arg"),
}
COMMAND_OPS = {
    1: "set_telemetry_period_ms",
    2: "set_pa_level",
    3: "request_status",
    4: "identify",
}
KV_TAGS = {0: "int", 1: "float", 2: "bool", 3: "string"}
KV_TAG_CODES = {name: code for code, name in KV_TAGS.items()}
KV_MAX_KEY = 15
KV_MAX_STRING = 32
KV_MAX_ENTRIES = 15

BATTERY_FLAG_NO_CURRENT_SENSOR = 0x80
SOC_UNKNOWN = 0xFFFF

STATS_FORMAT = "<13I4B"
STATS_FIELDS = (
    "uptime_ms",
    "frames_received",
    "malformed",
    "crc_errors",
    "duplicates",
    "delivered_reliable",
    "delivered_unreliable",
    "unreliable_lost",
    "reliable_sent",
    "reliable_acked",
    "reliable_failed",
    "reliable_retries",
    "resyncs",
    "hop_index",
    "hop_locked",
    "channel",
    "rpd_percent",
)
STATS_SIZE = struct.calcsize(STATS_FORMAT)  # 56
QUALITY_FIELDS = (
    "crc_errors",
    "malformed",
    "duplicates",
    "unreliable_lost",
    "reliable_retries",
    "reliable_failed",
    "resyncs",
)
"""Radio counters kept in ``link_counts``; increases are reported as notices, except
``duplicates``, which a working reliable channel produces whenever an ack is lost."""
_NOTICE_FIELDS = tuple(f for f in QUALITY_FIELDS if f != "duplicates")


class GatewayFrameError(ValueError):
    """A rejected serial packet; ``kind`` is one of the error kinds in the module doc."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class MessageError(ValueError):
    """Bytes that are not a valid application message."""


@dataclass(frozen=True)
class GatewayPacket:
    kind: int
    seq: int
    body: bytes

    @property
    def name(self) -> str:
        return KINDS.get(self.kind, f"kind 0x{self.kind:02X}")


# -- serial packets ------------------------------------------------------------------------


def encode_packet(kind: int, seq: int, body: bytes) -> bytes:
    """Build an unstuffed serial packet."""
    if len(body) > MAX_BODY:
        raise ValueError(f"body of {len(body)} bytes is longer than {MAX_BODY}")
    head = bytes((SERIAL_VERSION, kind & 0xFF, seq & 0xFF, len(body))) + body
    crc = crc16_ccitt_false(head)
    return head + bytes((crc & 0xFF, crc >> 8))


def encode_wire(kind: int, seq: int, body: bytes) -> bytes:
    """Build a complete wire frame, delimiter included."""
    return cobs_encode(encode_packet(kind, seq, body)) + b"\x00"


def decode_packet(packet: bytes) -> GatewayPacket:
    """Validate an unstuffed packet. Raises :class:`GatewayFrameError`."""
    if len(packet) < 6:
        raise GatewayFrameError("short", f"packet of {len(packet)} bytes is shorter than 6")
    crc = packet[-2] | (packet[-1] << 8)
    computed = crc16_ccitt_false(packet[:-2])
    if crc != computed:
        raise GatewayFrameError(
            "crc", f"CRC mismatch: frame has {crc:04X}, computed {computed:04X}"
        )
    if packet[0] != SERIAL_VERSION:
        raise GatewayFrameError("version", f"unsupported serial version {packet[0]}")
    if packet[3] + 6 != len(packet):
        raise GatewayFrameError(
            "length", f"length field {packet[3]} does not match a body of {len(packet) - 6}"
        )
    return GatewayPacket(packet[1], packet[2], bytes(packet[4:-2]))


def encode_stats(stats: dict[str, int]) -> bytes:
    """Build a ``link_stats`` body from a dict with every name in :data:`STATS_FIELDS`."""
    return struct.pack(STATS_FORMAT, *(stats[name] for name in STATS_FIELDS))


# -- application messages ------------------------------------------------------------------


def _kv_key(key: str) -> bytes:
    raw = key.encode("ascii")
    if not 1 <= len(raw) <= KV_MAX_KEY or any(c < 0x21 or c > 0x7E for c in raw):
        raise MessageError(f"invalid key {key!r}")
    return raw


def encode_message(name: str, fields: dict[str, Any]) -> bytes:
    """Build an application message from fields named as in the shared vectors."""
    head = bytes((SCHEMA_VERSION, MESSAGE_CODES[name]))
    if name == "imu":
        a, g = fields["accel_mg"], fields["gyro_cdps"]
        return head + struct.pack(_FIXED_BODY["imu"], fields["t_ms"], *a, *g, fields["temp_cc"])
    if name != "key_value":
        values = [fields[k] for k in _FIXED_FIELDS[name]]
        return head + struct.pack(_FIXED_BODY[name], *values)
    entries = fields["entries"]
    if len(entries) > KV_MAX_ENTRIES:
        raise MessageError("too many key/value entries")
    out = bytearray(head)
    out.append(len(entries))
    for entry in entries:
        key = _kv_key(entry["key"])
        tag = KV_TAG_CODES[entry["type"]]
        out += bytes((len(key),)) + key + bytes((tag,))
        value = entry["value"]
        if tag == 0:
            out += struct.pack("<i", int(value))
        elif tag == 1:
            out += struct.pack("<f", float(value))
        elif tag == 2:
            out.append(1 if value else 0)
        else:
            raw = str(value).encode("utf-8")
            if len(raw) > KV_MAX_STRING:
                raise MessageError("string value longer than 32 bytes")
            out += bytes((len(raw),)) + raw
    return bytes(out)


def _decode_kv(data: bytes) -> dict[str, Any]:
    n = len(data)
    if n < 3 or data[2] > KV_MAX_ENTRIES:
        raise MessageError("bad key/value count")
    pos = 3
    entries: list[dict[str, Any]] = []
    for _ in range(data[2]):
        if pos >= n:
            raise MessageError("key/value message truncated")
        klen = data[pos]
        pos += 1
        if not 1 <= klen <= KV_MAX_KEY or pos + klen + 1 > n:
            raise MessageError("bad key length")
        raw_key = data[pos : pos + klen]
        if any(c < 0x21 or c > 0x7E for c in raw_key):
            raise MessageError("non-printable key")
        pos += klen
        tag = data[pos]
        pos += 1
        value: Any
        if tag in (0, 1):
            if pos + 4 > n:
                raise MessageError("value truncated")
            value = struct.unpack_from("<i" if tag == 0 else "<f", data, pos)[0]
            pos += 4
        elif tag == 2:
            if pos >= n or data[pos] > 1:
                raise MessageError("bad bool value")
            value = data[pos] == 1
            pos += 1
        elif tag == 3:
            if pos >= n:
                raise MessageError("value truncated")
            slen = data[pos]
            pos += 1
            if slen > KV_MAX_STRING or pos + slen > n:
                raise MessageError("bad string length")
            value = data[pos : pos + slen].decode("utf-8", errors="replace")
            pos += slen
        else:
            raise MessageError(f"unknown value tag {tag}")
        entries.append({"key": raw_key.decode("ascii"), "type": KV_TAGS[tag], "value": value})
    if pos != n:
        raise MessageError("trailing bytes after key/value entries")
    return {"entries": entries}


def decode_message(data: bytes) -> tuple[str, dict[str, Any]]:
    """Validate an application message; return its name and fields. Raises MessageError."""
    if len(data) < 2:
        raise MessageError("message shorter than its 2-byte header")
    if data[0] != SCHEMA_VERSION:
        raise MessageError(f"unsupported schema version {data[0]}")
    name = MESSAGE_TYPES.get(data[1])
    if name is None:
        raise MessageError(f"unknown message type 0x{data[1]:02X}")
    if name == "key_value":
        return name, _decode_kv(data)
    fmt = _FIXED_BODY[name]
    size = 2 + struct.calcsize(fmt)
    if len(data) != size:
        raise MessageError(f"{name} message is {len(data)} bytes, expected {size}")
    values = struct.unpack_from(fmt, data, 2)
    if name == "imu":
        return name, {
            "t_ms": values[0],
            "accel_mg": list(values[1:4]),
            "gyro_cdps": list(values[4:7]),
            "temp_cc": values[7],
        }
    return name, dict(zip(_FIXED_FIELDS[name], values, strict=True))


def describe_command(fields: dict[str, Any]) -> str:
    op = COMMAND_OPS.get(fields["opcode"], f"op{fields['opcode']}")
    return f"{op} arg={fields['arg']}"


def _kv_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def message_values(name: str, fields: dict[str, Any]) -> dict[str, float]:
    """Channel values of a telemetry message, in SI-ish units (see the README table)."""
    if name == "battery":
        values = {"battery.bus_v": fields["bus_mv"] / 1000.0}
        if not fields["flags"] & BATTERY_FLAG_NO_CURRENT_SENSOR:
            values["battery.current_ma"] = fields["current_ua"] / 1000.0
            values["battery.power_mw"] = fields["power_uw"] / 1000.0
            values["battery.charge_mah"] = fields["charge_uah"] / 1000.0
        if fields["soc_permille"] != SOC_UNKNOWN:
            values["battery.soc_pct"] = fields["soc_permille"] / 10.0
        values["battery.flags"] = float(fields["flags"])
        return values
    if name == "odometry":
        return {
            "odom.x": fields["x_mm"] / 1000.0,
            "odom.y": fields["y_mm"] / 1000.0,
            "odom.theta": fields["theta_mrad"] / 1000.0,
            "odom.v": fields["v_mm_s"] / 1000.0,
            "odom.w": fields["w_mrad_s"] / 1000.0,
        }
    if name == "imu":
        values = {}
        for axis, mg in zip("xyz", fields["accel_mg"], strict=True):
            values[f"imu.a{axis}"] = mg / 1000.0
        for axis, cdps in zip("xyz", fields["gyro_cdps"], strict=True):
            values[f"imu.g{axis}"] = cdps / 100.0
        values["imu.temp_c"] = fields["temp_cc"] / 100.0
        return values
    if name == "key_value":
        return {
            f"kv.{e['key']}": float(e["value"]) for e in fields["entries"] if e["type"] != "string"
        }
    return {}


# -- decoder -------------------------------------------------------------------------------


class Nrf24GatewayDecoder(CobsFrameDecoder):
    name = "nrf24"
    description = "nrf24-telemetry-link gateway USB packets: COBS + CRC-16, radio link stats"
    max_frame = MAX_FRAME_BYTES

    def __init__(self) -> None:
        super().__init__()
        self.last_seq: int | None = None
        self.last_stats: dict[str, int] | None = None
        self._node_clock = ClockUnwrapper(32, 1e-3)
        self._gateway_clock = ClockUnwrapper(32, 1e-3)

    def decode_frame(self, t: float, raw: bytes) -> list[Event]:
        try:
            packet = decode_packet(cobs_decode(raw))
        except CobsError as exc:
            return [self.error(t, "cobs", str(exc), raw)]
        except GatewayFrameError as exc:
            return [self.error(t, exc.kind, str(exc), raw)]
        if packet.kind not in KINDS:
            return [self.error(t, "kind", f"unknown packet kind 0x{packet.kind:02X}", raw)]
        if packet.kind < 0x80:  # each direction has its own sequence counter
            if self.last_seq is not None:
                self.counts["lost"] += (packet.seq - self.last_seq - 1) % 256
            self.last_seq = packet.seq
        self.counts["frames"] += 1
        self.counts[packet.name] += 1
        try:
            return self._packet(t, packet)
        except MessageError as exc:
            return [self.error(t, "message", f"{packet.name}: {exc}", raw)]
        except GatewayFrameError as exc:
            return [self.error(t, exc.kind, str(exc), raw)]

    def _packet(self, t: float, packet: GatewayPacket) -> list[Event]:
        body = packet.body
        name = packet.name
        if name == "message":
            if len(body) < 3:
                raise GatewayFrameError("length", "message body shorter than 3 bytes")
            return self._message(t, body[3:])
        if name == "link_stats":
            if len(body) != STATS_SIZE:
                raise GatewayFrameError(
                    "length", f"link_stats body is {len(body)} bytes, expected {STATS_SIZE}"
                )
            stats = dict(zip(STATS_FIELDS, struct.unpack(STATS_FORMAT, body), strict=True))
            return self._stats(t, stats)
        if name == "command_result":
            if len(body) != 2:
                raise GatewayFrameError(
                    "length", f"command_result body is {len(body)} bytes, expected 2"
                )
            delivered = body[1] == 1
            state = "delivered" if delivered else "NOT delivered"
            events: list[Event] = [TextLine(t, f"COMMAND_RESULT link_seq={body[0]} {state}")]
            if not delivered:
                self.counts["commands_not_delivered"] += 1
                events.append(
                    Notice(t, "link", f"gateway: command with link seq {body[0]} not delivered")
                )
            return events
        if name == "log":
            text = body.decode("utf-8", errors="replace").rstrip("\r\n")
            return [TextLine(t, f"[gateway] {text}")]
        if name == "send_command":
            msg_name, fields = decode_message(body)
            detail = describe_command(fields) if msg_name == "command" else msg_name
            return [TextLine(t, f"SEND_COMMAND {detail}")]
        return [TextLine(t, "REQUEST_STATS")]

    def _message(self, t: float, data: bytes) -> list[Event]:
        name, fields = decode_message(data)
        self.counts[name] += 1
        if name == "command":
            return [TextLine(t, f"COMMAND {describe_command(fields)}")]
        device_t = self._node_clock(fields["t_ms"]) if "t_ms" in fields else None
        events: list[Event] = []
        if name == "key_value":
            text = " ".join(f"{e['key']}={_kv_text(e['value'])}" for e in fields["entries"])
            events.append(TextLine(t, f"KV {text}".rstrip()))
        values = message_values(name, fields)
        if values:
            events.append(Sample(t, values, device_t, name))
        return events

    def _stats(self, t: float, stats: dict[str, int]) -> list[Event]:
        events: list[Event] = []
        previous = self.last_stats
        restarted = previous is not None and (
            stats["uptime_ms"] < previous["uptime_ms"]
            or any(stats[f] < previous[f] for f in QUALITY_FIELDS)
        )
        if restarted:
            self.counts["gateway_restarts"] += 1
            events.append(
                Notice(t, "link", "gateway restarted: its radio counters start again from zero")
            )
        elif previous is not None:
            increases = [
                f"{f} +{stats[f] - previous[f]} (total {stats[f]})"
                for f in _NOTICE_FIELDS
                if stats[f] > previous[f]
            ]
            if increases:
                events.append(Notice(t, "link", "radio: " + ", ".join(increases)))
        self.last_stats = stats
        self.link_counts.clear()
        self.link_counts.update({f: stats[f] for f in ("frames_received", *QUALITY_FIELDS)})
        values = {f"link.{k}": float(v) for k, v in stats.items() if k != "uptime_ms"}
        device_t = self._gateway_clock(stats["uptime_ms"])
        events.insert(0, Sample(t, values, device_t, "link_stats"))
        return events
