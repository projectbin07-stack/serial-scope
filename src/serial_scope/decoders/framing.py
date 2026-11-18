"""Checksums and byte stuffing shared by the framed formats."""

from __future__ import annotations


class CobsError(ValueError):
    """Raised for input that is not valid COBS."""


def crc8(data: bytes, crc: int = 0x00) -> int:
    """CRC-8, polynomial 0x07, initial 0x00, MSB first, no reflection, no final XOR.

    ``crc8(b"123456789") == 0xF4``.
    """
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc16_ccitt_false(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: polynomial 0x1021, initial 0xFFFF, no reflection, no final XOR.

    ``crc16_ccitt_false(b"123456789") == 0x29B1``.
    """
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    """Consistent Overhead Byte Stuffing. The result contains no zero bytes."""
    out = bytearray()
    block = bytearray()
    after_full_block = False
    for byte in data:
        after_full_block = False
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(byte)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
                after_full_block = True
    # A maximal block that ends the data carries no implicit zero, so nothing follows it.
    if not after_full_block:
        out.append(len(block) + 1)
        out += block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """Inverse of :func:`cobs_encode`. Raises :class:`CobsError` on malformed input."""
    out = bytearray()
    i = 0
    n = len(data)
    if n == 0:
        raise CobsError("empty frame")
    while i < n:
        code = data[i]
        if code == 0:
            raise CobsError(f"zero byte at offset {i}")
        end = i + code
        if end > n:
            raise CobsError(f"block at offset {i} runs past the end of the frame")
        block = data[i + 1 : end]
        if 0 in block:
            raise CobsError("zero byte inside a block")
        out += block
        i = end
        if code != 255 and i < n:
            out.append(0)
    return bytes(out)
