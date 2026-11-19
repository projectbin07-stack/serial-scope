"""Data types that flow between transports, decoders and the rest of the program."""

from __future__ import annotations

from dataclasses import dataclass, field

RX = "rx"
"""Bytes received from the device."""
TX = "tx"
"""Bytes sent to the device."""
MARK = "mark"
"""A user or trigger marker (UTF-8 text payload)."""
INFO = "info"
"""Connection status and similar notes (UTF-8 text payload)."""

DIRECTIONS = (RX, TX, MARK, INFO)


@dataclass(frozen=True)
class Chunk:
    """A block of bytes as delivered by a transport, with the host time it arrived.

    ``t`` is seconds since the Unix epoch on the host clock. ``direction`` is one of
    :data:`RX`, :data:`TX`, :data:`MARK` or :data:`INFO`.
    """

    data: bytes
    t: float
    direction: str = RX


@dataclass(frozen=True)
class TextLine:
    """A line of text that is not (or not only) numeric telemetry."""

    t: float
    text: str


@dataclass(frozen=True)
class Sample:
    """One decoded record: a set of channel values observed at the same moment.

    ``t`` is the host receive time; ``device_t`` is the device's own timestamp in seconds
    when the format carries one. ``source`` names the record type (for example the
    message name of a binary frame).
    """

    t: float
    values: dict[str, float]
    device_t: float | None = None
    source: str = ""


@dataclass(frozen=True)
class DecodeError:
    """Something in the stream that could not be decoded. Never dropped silently."""

    t: float
    kind: str
    detail: str
    raw: bytes = b""


@dataclass(frozen=True)
class Notice:
    """Something the device reported about itself that deserves attention.

    For example a radio gateway's statistics showing new CRC errors or lost packets on
    its radio link. The bytes on the serial line decoded fine, so this is not a
    :class:`DecodeError` and does not count towards ``--max-errors``.
    """

    t: float
    kind: str
    detail: str


@dataclass(frozen=True)
class TriggerFired:
    """A trigger condition became true."""

    t: float
    trigger: str
    detail: str
    actions: tuple[str, ...] = field(default_factory=tuple)


Event = TextLine | Sample | DecodeError | Notice | TriggerFired
