from __future__ import annotations

import pytest

from conftest import DATA, errors, feed_split, samples, texts
from serial_scope.decoders.framing import crc8
from serial_scope.decoders.ina219 import (
    Ina219Decoder,
    Ina219FrameError,
    decode_bm1,
    encode_bm1,
)

# Shared fixtures of ina219-battery-monitor (test/test_telemetry and tools/tests), with the
# integer fields they decode to. See tests/data/SOURCES.md.
SHARED_FIXTURES: list[tuple[str, list[int]]] = [
    ("$BM1,0,0,0,0,0,0,0,0,0*78", [0, 0, 0, 0, 0, 0, 0, 0, 0]),
    (
        "$BM1,42,21000,3876,100000,386000,583,2261,512,0*D1",
        [42, 21000, 3876, 100000, 386000, 583, 2261, 512, 0],
    ),
    (
        "$BM1,43,21500,3872,-250300,-969000,548,2127,513,0*3F",
        [43, 21500, 3872, -250300, -969000, 548, 2127, 513, 0],
    ),
    (
        "$BM1,4294967295,4294967295,2147483647,-2147483648,-2147483648,"
        "2147483647,-2147483648,1000,255*6F",
        [
            4294967295,
            4294967295,
            2147483647,
            -2147483648,
            -2147483648,
            2147483647,
            -2147483648,
            1000,
            255,
        ],
    ),
    (
        "$BM1,44,22000,2990,1200000,3588000,715,2625,12,8*69",
        [44, 22000, 2990, 1200000, 3588000, 715, 2625, 12, 8],
    ),
    ("$BM1,45,22500,0,0,0,715,2625,12,4*4E", [45, 22500, 0, 0, 0, 715, 2625, 12, 4]),
]


@pytest.mark.parametrize(("line", "fields"), SHARED_FIXTURES)
def test_shared_fixtures_decode(line: str, fields: list[int]) -> None:
    assert decode_bm1(line) == fields
    assert encode_bm1(fields) == line


def test_lower_case_checksum_and_line_endings_accepted() -> None:
    assert decode_bm1("$BM1,45,22500,0,0,0,715,2625,12,4*4e\r\n")[0] == 45


def test_typical_frame_units() -> None:
    # README example: frame 42 at 21.000 s: 3.876 V, 100.000 mA, 386.000 mW,
    # 0.583 mAh, 2.261 mWh, 51.2 %, no flags.
    events = Ina219Decoder().feed(b"$BM1,42,21000,3876,100000,386000,583,2261,512,0*D1\r\n", 7.0)
    (sample,) = samples(events)
    assert sample.device_t == 21.0
    assert sample.values == pytest.approx(
        {
            "bus_v": 3.876,
            "current_ma": 100.0,
            "power_mw": 386.0,
            "charge_mah": 0.583,
            "energy_mwh": 2.261,
            "soc_pct": 51.2,
            "flags": 0.0,
        }
    )


@pytest.mark.parametrize(
    ("line", "kind", "message"),
    [
        ("$BM1,42,21000,3876,100000,386000,583,2261,512,0*D2", "crc", "checksum mismatch"),
        ("$BM1,42,21000,3877,100000,386000,583,2261,512,0*D1", "crc", "checksum mismatch"),
        ("$BM1,42,21000,3876,100000,386000,583,2261,512,0", "format", "missing '\\*'"),
        ("$BM1,42,21000,3876,100000,386000,583,2261,512,0*D", "format", "malformed"),
        ("$BM1,42,21000,3876,100000,386000,583,2261,512,0*G1", "format", "malformed"),
        ("$BM1,42,21000,3876*D1", "crc", "checksum mismatch"),
    ],
)
def test_rejections(line: str, kind: str, message: str) -> None:
    with pytest.raises(Ina219FrameError, match=message) as info:
        decode_bm1(line)
    assert info.value.kind == kind


def _sealed(body: str) -> str:
    return f"${body}*{crc8(body.encode('ascii')):02X}"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("BM2,0,0,0,0,0,0,0,0,0", "unknown frame tag"),
        ("BM1,1,2,3", "expected 9 fields, got 3"),
        ("BM1,x,0,0,0,0,0,0,0,0", "not a decimal integer"),
        ("BM1,0,0,3.8,0,0,0,0,0,0", "not a decimal integer"),
        ("BM1,0,0,+5,0,0,0,0,0,0", "not a decimal integer"),
        ("BM1,-1,0,0,0,0,0,0,0,0", "'seq' out of range"),
        ("BM1,0,0,0,0,0,0,0,1001,0", "'soc_permille' out of range"),
        ("BM1,0,0,0,0,0,0,0,0,256", "'flags' out of range"),
    ],
)
def test_structure_errors_with_valid_checksum(body: str, message: str) -> None:
    with pytest.raises(Ina219FrameError, match=message) as info:
        decode_bm1(_sealed(body))
    assert info.value.kind == "format"


def test_every_single_bit_flip_is_rejected() -> None:
    good = "$BM1,42,21000,3876,100000,386000,583,2261,512,0*D1"
    raw = good.encode()
    for i in range(1, len(raw)):  # keep the '$'
        for bit in range(7):
            corrupted = bytearray(raw)
            corrupted[i] ^= 1 << bit
            if corrupted[i] in (0x0A, 0x0D):
                continue  # would split the line instead
            try:
                decode_bm1(corrupted.decode("latin-1"))
            except Ina219FrameError:
                continue
            # The only accepted change is the case of a checksum hex letter (D1 -> d1).
            assert i >= len(raw) - 2
            assert chr(corrupted[i]).upper() == chr(raw[i])


@pytest.mark.parametrize("size", [1, 2, 5, 11, 4096])
def test_sample_session_log(size: int) -> None:
    """Matches the summary of the upstream logger on the same file:
    5 frames, 2 bad lines, 3 comments, 1 dropped, 1 restarts."""
    data = (DATA / "ina219_sample_session.log").read_bytes()
    decoder = Ina219Decoder()
    events = feed_split(decoder, data, size)
    assert len(samples(events)) == 5
    assert decoder.counts["frames"] == 5
    assert sum(decoder.errors.values()) == 2
    assert decoder.errors == {"format": 1, "crc": 1}
    assert decoder.counts["dropped"] == 1
    assert decoder.counts["restarts"] == 1
    comments = [t for t in texts(events) if t.startswith("#")]
    assert len(comments) == 3
    kinds = [e.kind for e in errors(events)]
    assert kinds == ["format", "crc"]
    last = samples(events)[-1]
    assert last.device_t == 0.498
    assert last.values["soc_pct"] == pytest.approx(78.9)


def test_non_frame_lines_are_text() -> None:
    events = Ina219Decoder().feed(b"hello\r\n#OK RESET\r\n", 1.0)
    assert texts(events) == ["hello", "#OK RESET"]
    assert errors(events) == []
