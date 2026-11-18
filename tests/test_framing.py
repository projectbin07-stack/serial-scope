from __future__ import annotations

from typing import Any

import pytest

from serial_scope.decoders.framing import (
    CobsError,
    cobs_decode,
    cobs_encode,
    crc8,
    crc16_ccitt_false,
)


def test_crc8_check_value() -> None:
    # CRC-8/SMBUS check value from the CRC catalogue and the ina219 README.
    assert crc8(b"123456789") == 0xF4
    assert crc8(b"") == 0x00


def test_crc16_vectors(vectors: dict[str, Any]) -> None:
    for case in vectors["crc16"]:
        assert crc16_ccitt_false(case["ascii"].encode("ascii")) == case["crc"], case["name"]


def test_cobs_vectors(vectors: dict[str, Any]) -> None:
    for case in vectors["cobs"]:
        decoded = bytes.fromhex(case["decoded"])
        encoded = bytes.fromhex(case["encoded"])
        assert cobs_encode(decoded) == encoded, case["name"]
        assert cobs_decode(encoded) == decoded, case["name"]


@pytest.mark.parametrize("length", [0, 1, 253, 254, 255, 508, 509, 600])
def test_cobs_round_trip_with_zeros(length: int) -> None:
    data = bytes((i * 7) % 256 for i in range(length))
    encoded = cobs_encode(data)
    assert 0 not in encoded
    assert cobs_decode(encoded) == data


def test_cobs_254_bytes_then_zero() -> None:
    data = bytes(range(1, 255)) + b"\x00"
    assert cobs_encode(data) == b"\xff" + bytes(range(1, 255)) + b"\x01\x01"
    assert cobs_decode(cobs_encode(data)) == data


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        (b"", "empty"),
        (b"\x05\x01\x02", "runs past"),
        (b"\x02\x01\x00", "zero"),
        (b"\x03\x00\x01", "zero byte inside"),
    ],
)
def test_cobs_decode_errors(encoded: bytes, message: str) -> None:
    with pytest.raises(CobsError, match=message):
        cobs_decode(encoded)
