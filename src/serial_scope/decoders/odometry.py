"""COBS-framed binary protocol of the encoder-odometry-firmware (protocol version 1).

Wire format, as documented in docs/protocol.md of
https://github.com/projectbin07-stack/encoder-odometry-firmware::

    wire frame = COBS(packet) 0x00
    packet     = version:u8 type:u8 seq:u8 length:u8 body[length] crc:u16 (little-endian)

The CRC is CRC-16/CCITT-FALSE over ``version .. body``. Bodies are little-endian. This is
an independent re-implementation; it is tested against the protocol's shared vector file.

Every rejected frame is reported with one of these kinds: ``overlong`` (more than 72
bytes between delimiters), ``cobs``, ``short``, ``crc``, ``version``, ``type`` and
``length``. Gaps in the sequence number are counted as ``lost`` frames.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from serial_scope.decoders.cobsframes import ClockUnwrapper, CobsFrameDecoder
from serial_scope.decoders.framing import CobsError, cobs_decode, cobs_encode, crc16_ccitt_false
from serial_scope.events import Event, Sample, TextLine

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 72
"""Anything longer than this between two delimiters is a framing error."""
WHEELS = ("fl", "fr", "rl", "rr")

# type -> (name, struct format of the body)
MESSAGES: dict[int, tuple[str, str]] = {
    0x01: ("velocity_command", "<3f"),
    0x02: ("wheel_state", "<I" + "iffh" * 4),
    0x03: ("odometry", "<I6f"),
    0x04: ("diagnostics", "<I4I5IB"),
    0x05: ("param_get", "<B"),
    0x06: ("param_set", "<Bf"),
    0x07: ("param_value", "<BBf"),
    0x08: ("set_pose", "<3f"),
}
MESSAGE_TYPES = {name: code for code, (name, _) in MESSAGES.items()}

PARAM_NAMES: dict[int, str] = {
    1: "kp",
    2: "ki",
    3: "kd",
    4: "kff",
    5: "ks",
    6: "output_limit",
    7: "slew_rate",
    8: "d_cutoff_hz",
    9: "max_wheel_speed",
    10: "watchdog_ms",
    11: "wheel_radius",
    12: "half_length",
    13: "half_width",
    14: "roller_angle_deg",
    15: "ticks_per_wheel_rev",
    16: "drive_type",
    17: "telemetry_divider",
    18: "estimator_low_cps",
    19: "estimator_high_cps",
    20: "zero_timeout_ms",
    21: "control_rate_hz",
    22: "protocol_version",
}
PARAM_STATUS = {0: "ok", 1: "unknown id", 2: "out of range", 3: "read-only"}


class FrameError(ValueError):
    """A rejected packet; ``kind`` is one of the error kinds listed in the module doc."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Message:
    """A decoded packet with its fields named as in the protocol document."""

    type: int
    name: str
    seq: int
    fields: dict[str, Any]


def _body_fields(name: str, values: tuple[Any, ...]) -> dict[str, Any]:
    if name in ("velocity_command",):
        return dict(zip(("vx", "vy", "wz"), values, strict=True))
    if name == "set_pose":
        return dict(zip(("x", "y", "theta"), values, strict=True))
    if name == "wheel_state":
        rest = values[1:]
        return {
            "t_us": values[0],
            "count": [rest[4 * i] for i in range(4)],
            "velocity": [rest[4 * i + 1] for i in range(4)],
            "target": [rest[4 * i + 2] for i in range(4)],
            "duty": [rest[4 * i + 3] for i in range(4)],
        }
    if name == "odometry":
        return dict(zip(("t_us", "x", "y", "theta", "vx", "vy", "wz"), values, strict=True))
    if name == "diagnostics":
        return {
            "t_us": values[0],
            "illegal_transitions": list(values[1:5]),
            "loop_overruns": values[5],
            "watchdog_trips": values[6],
            "max_loop_us": values[7],
            "rx_crc_errors": values[8],
            "rx_frame_errors": values[9],
            "flags": values[10],
        }
    if name == "param_get":
        return {"id": values[0]}
    if name == "param_set":
        return {"id": values[0], "value": values[1]}
    return {"id": values[0], "status": values[1], "value": values[2]}  # param_value


def _body_values(name: str, fields: dict[str, Any]) -> tuple[Any, ...]:
    if name == "wheel_state":
        out: list[Any] = [fields["t_us"]]
        for i in range(4):
            out += [
                fields["count"][i],
                fields["velocity"][i],
                fields["target"][i],
                fields["duty"][i],
            ]
        return tuple(out)
    if name == "diagnostics":
        return (
            fields["t_us"],
            *fields["illegal_transitions"],
            fields["loop_overruns"],
            fields["watchdog_trips"],
            fields["max_loop_us"],
            fields["rx_crc_errors"],
            fields["rx_frame_errors"],
            fields["flags"],
        )
    order = {
        "velocity_command": ("vx", "vy", "wz"),
        "set_pose": ("x", "y", "theta"),
        "odometry": ("t_us", "x", "y", "theta", "vx", "vy", "wz"),
        "param_get": ("id",),
        "param_set": ("id", "value"),
        "param_value": ("id", "status", "value"),
    }[name]
    return tuple(fields[key] for key in order)


def decode_packet(packet: bytes) -> Message:
    """Validate and decode an unstuffed packet. Raises :class:`FrameError`."""
    if len(packet) < 6:
        raise FrameError("short", f"packet of {len(packet)} bytes is shorter than 6")
    payload, crc_bytes = packet[:-2], packet[-2:]
    crc = crc_bytes[0] | (crc_bytes[1] << 8)
    computed = crc16_ccitt_false(payload)
    if crc != computed:
        raise FrameError("crc", f"CRC mismatch: frame has {crc:04X}, computed {computed:04X}")
    version, mtype, seq, length = payload[0], payload[1], payload[2], payload[3]
    if version != PROTOCOL_VERSION:
        raise FrameError("version", f"unsupported protocol version {version}")
    if mtype not in MESSAGES:
        raise FrameError("type", f"unknown message type 0x{mtype:02X}")
    name, fmt = MESSAGES[mtype]
    size = struct.calcsize(fmt)
    body = payload[4:]
    if length != size or len(body) != size:
        raise FrameError(
            "length", f"{name}: length field {length}, body {len(body)}, expected {size}"
        )
    return Message(mtype, name, seq, _body_fields(name, struct.unpack(fmt, body)))


def encode_packet(name: str, seq: int, fields: dict[str, Any]) -> bytes:
    """Build the unstuffed packet for message ``name`` (inverse of :func:`decode_packet`)."""
    mtype = MESSAGE_TYPES[name]
    fmt = MESSAGES[mtype][1]
    body = struct.pack(fmt, *_body_values(name, fields))
    payload = bytes([PROTOCOL_VERSION, mtype, seq & 0xFF, len(body)]) + body
    crc = crc16_ccitt_false(payload)
    return payload + bytes([crc & 0xFF, crc >> 8])


def encode_frame(name: str, seq: int, fields: dict[str, Any]) -> bytes:
    """Build a complete wire frame, delimiter included."""
    return cobs_encode(encode_packet(name, seq, fields)) + b"\x00"


def message_to_events(msg: Message, t: float) -> list[Event]:
    """Convert a decoded message into plot samples and/or terminal text."""
    f = msg.fields
    name = msg.name
    if name == "wheel_state":
        values: dict[str, float] = {}
        for i, wheel in enumerate(WHEELS):
            values[f"{wheel}.count"] = float(f["count"][i])
            values[f"{wheel}.velocity"] = float(f["velocity"][i])
            values[f"{wheel}.target"] = float(f["target"][i])
            values[f"{wheel}.duty"] = f["duty"][i] / 10000.0
        return [Sample(t, values, f["t_us"] / 1e6, name)]
    if name == "odometry":
        keys = ("x", "y", "theta", "vx", "vy", "wz")
        values = {f"odom.{k}": float(f[k]) for k in keys}
        return [Sample(t, values, f["t_us"] / 1e6, name)]
    if name == "diagnostics":
        illegal = f["illegal_transitions"]
        values = {f"diag.illegal_{w}": float(v) for w, v in zip(WHEELS, illegal, strict=True)}
        for key in (
            "loop_overruns",
            "watchdog_trips",
            "max_loop_us",
            "rx_crc_errors",
            "rx_frame_errors",
            "flags",
        ):
            values[f"diag.{key}"] = float(f[key])
        return [Sample(t, values, f["t_us"] / 1e6, name)]
    if name in ("velocity_command", "set_pose"):
        prefix = "cmd" if name == "velocity_command" else "set_pose"
        values = {f"{prefix}.{k}": float(v) for k, v in f.items()}
        text = name.upper() + " " + " ".join(f"{k}={v:g}" for k, v in f.items())
        return [TextLine(t, text), Sample(t, values, None, name)]
    param = PARAM_NAMES.get(f["id"], f"id{f['id']}")
    if name == "param_get":
        return [TextLine(t, f"PARAM_GET {param}")]
    if name == "param_set":
        return [TextLine(t, f"PARAM_SET {param} = {f['value']:g}")]
    status = PARAM_STATUS.get(f["status"], f"status {f['status']}")
    return [
        TextLine(t, f"PARAM_VALUE {param} = {f['value']:g} ({status})"),
        Sample(t, {f"param.{param}": float(f["value"])}, None, name),
    ]


class OdometryDecoder(CobsFrameDecoder):
    name = "odometry"
    description = "encoder-odometry-firmware binary frames: COBS + CRC-16/CCITT-FALSE"
    max_frame = MAX_FRAME_BYTES

    def __init__(self) -> None:
        super().__init__()
        self.last_seq: int | None = None
        self._clock = ClockUnwrapper(32, 1e-6)

    def decode_frame(self, t: float, raw: bytes) -> list[Event]:
        try:
            msg = decode_packet(cobs_decode(raw))
        except CobsError as exc:
            return [self.error(t, "cobs", str(exc), raw)]
        except FrameError as exc:
            return [self.error(t, exc.kind, str(exc), raw)]
        if self.last_seq is not None:
            self.counts["lost"] += (msg.seq - self.last_seq - 1) % 256
        self.last_seq = msg.seq
        self.counts["frames"] += 1
        self.counts[msg.name] += 1
        events = message_to_events(msg, t)
        if "t_us" in msg.fields:
            # The 32-bit microsecond clock wraps every ~71.6 min.
            device_t = self._clock(int(msg.fields["t_us"]))
            events = [
                Sample(e.t, e.values, device_t, e.source) if isinstance(e, Sample) else e
                for e in events
            ]
        return events
