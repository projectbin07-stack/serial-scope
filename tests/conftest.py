from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from serial_scope.decoders.base import Decoder
from serial_scope.events import DecodeError, Event, Notice, Sample, TextLine

DATA = Path(__file__).parent / "data"


def _load_json(name: str) -> dict[str, Any]:
    with (DATA / name).open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def load_vectors() -> dict[str, Any]:
    return _load_json("odometry_protocol_vectors.json")


def load_nrf24_vectors() -> dict[str, Any]:
    return _load_json("nrf24_link_vectors.json")


def feed_split(decoder: Decoder, data: bytes, size: int, t0: float = 100.0) -> list[Event]:
    """Feed ``data`` in pieces of ``size`` bytes, then flush."""
    events: list[Event] = []
    t = t0
    for i in range(0, len(data), size):
        events += decoder.feed(data[i : i + size], t)
        t += 0.001
    events += decoder.flush(t)
    return events


def samples(events: list[Event]) -> list[Sample]:
    return [e for e in events if isinstance(e, Sample)]


def texts(events: list[Event]) -> list[str]:
    return [e.text for e in events if isinstance(e, TextLine)]


def errors(events: list[Event]) -> list[DecodeError]:
    return [e for e in events if isinstance(e, DecodeError)]


def notices(events: list[Event]) -> list[Notice]:
    return [e for e in events if isinstance(e, Notice)]


@pytest.fixture
def vectors() -> dict[str, Any]:
    return load_vectors()


@pytest.fixture
def nrf24_vectors() -> dict[str, Any]:
    return load_nrf24_vectors()
