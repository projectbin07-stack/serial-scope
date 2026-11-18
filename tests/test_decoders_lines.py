"""Line splitting, plain text, plotter, CSV and JSON-lines decoders, and auto-detection."""

from __future__ import annotations

import math

import pytest

from conftest import DATA, errors, feed_split, samples, texts
from serial_scope.decoders import available_decoders, create_decoder, register_decoder
from serial_scope.decoders.auto import MAX_DETECT_BYTES, AutoDecoder
from serial_scope.decoders.base import MAX_LINE_BYTES, LineSplitter, parse_number
from serial_scope.decoders.csvlines import CsvDecoder, detect_header
from serial_scope.decoders.jsonlines import JsonLinesDecoder, flatten_numbers
from serial_scope.decoders.plotter import PlotterDecoder, parse_plotter_line
from serial_scope.decoders.text import TextDecoder
from serial_scope.demo import GENERATORS, chunk_records

# -- line splitter -----------------------------------------------------------------------


def test_splitter_handles_crlf_split_across_reads() -> None:
    s = LineSplitter()
    assert s.feed(b"abc\r", 1.0) == [(1.0, b"abc")]
    assert s.feed(b"\ndef\n", 2.0) == [(2.0, b"def")]


def test_splitter_lone_cr_lf_and_time_of_first_byte() -> None:
    s = LineSplitter()
    assert s.feed(b"one\rtwo", 1.0) == [(1.0, b"one")]
    assert s.feed(b"\n\nthr", 2.0) == [(1.0, b"two"), (2.0, b"")]
    assert s.feed(b"ee", 3.0) == []
    assert s.flush() == (2.0, b"three")
    assert s.flush() is None


def test_splitter_overlong_line_is_reported_once_and_recovers() -> None:
    s = LineSplitter(max_line=10)
    out = s.feed(b"x" * 25, 1.0) + s.feed(b"yyy\nok\n", 2.0)
    assert out == [(1.0, None), (2.0, b"ok")]


def test_line_decoder_counts_overlong() -> None:
    d = TextDecoder()
    events = d.feed(b"a" * (MAX_LINE_BYTES + 5) + b"\nnext\n", 1.0)
    assert [e.kind for e in errors(events)] == ["overlong"]
    assert texts(events) == ["next"]


def test_text_decoder_invalid_utf8_is_replaced_not_dropped() -> None:
    events = TextDecoder().feed(b"temp \xff\xfe ok\n", 1.0)
    assert texts(events) == ["temp �� ok"]


# -- numbers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "value"),
    [("1", 1.0), ("-2.5", -2.5), (".5", 0.5), ("1e3", 1000.0), ("+7.", 7.0), ("1E-2", 0.01)],
)
def test_parse_number(token: str, value: float) -> None:
    assert parse_number(token) == value


@pytest.mark.parametrize("token", ["", "abc", "1.2.3", "0x10", "1_000", "--1", "e5"])
def test_parse_number_rejects(token: str) -> None:
    assert parse_number(token) is None


def test_parse_number_non_finite() -> None:
    assert math.isnan(parse_number("nan") or 0.0)
    assert parse_number("inf") == math.inf
    assert parse_number("-inf") == -math.inf
    assert parse_number("ovf") == math.inf


# -- plotter -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("1.5 2.25 -3", {"ch1": 1.5, "ch2": 2.25, "ch3": -3.0}),
        ("1\t2", {"ch1": 1.0, "ch2": 2.0}),
        ("1,2,3", {"ch1": 1.0, "ch2": 2.0, "ch3": 3.0}),
        ("temp:21.5,hum:40", {"temp": 21.5, "hum": 40.0}),
        ("temp: 21.5  hum: 40", {"temp": 21.5, "hum": 40.0}),
        ("a:1, 2", {"a": 1.0, "ch2": 2.0}),
        ("Temp:21.5", {"Temp": 21.5}),
        ("v:nan,w:2", {"w": 2.0}),
        ("x[0]:1 rate%:2", {"x[0]": 1.0, "rate%": 2.0}),
    ],
)
def test_plotter_lines(line: str, expected: dict[str, float]) -> None:
    assert parse_plotter_line(line) == expected


@pytest.mark.parametrize(
    "line", ["", "   ", "Connecting to WiFi...", "value is 3", '{"a":1}', "1 2 x", "9:30"]
)
def test_plotter_non_data_lines(line: str) -> None:
    assert parse_plotter_line(line) is None


def test_plotter_decoder_mixes_text_and_samples() -> None:
    events = PlotterDecoder().feed(b"boot ok\r\na:1,b:2\r\n3 4\r\n", 1.0)
    assert texts(events) == ["boot ok"]
    assert [s.values for s in samples(events)] == [{"a": 1.0, "b": 2.0}, {"ch1": 3.0, "ch2": 4.0}]


# -- CSV ---------------------------------------------------------------------------------


def test_detect_header() -> None:
    assert detect_header("millis,adc0,adc1") == (",", ["millis", "adc0", "adc1"])
    assert detect_header("t;v") == (";", ["t", "v"])
    assert detect_header("a\tb") == ("\t", ["a", "b"])
    assert detect_header("1,2,3") is None
    assert detect_header("a,1") is None
    assert detect_header("a,a") is None
    assert detect_header("single") is None
    assert detect_header("a,,b") is None
    assert detect_header("$BM1,x") is None


def test_csv_decoder_with_device_time() -> None:
    data = b"booting\r\nmillis,adc0,vref_mv\r\n50,2048,3300\r\n100,2050,3301\r\n"
    d = CsvDecoder()
    events = feed_split(d, data, 3)
    assert texts(events) == ["booting", "millis,adc0,vref_mv"]
    s1, s2 = samples(events)
    assert s1.device_t == pytest.approx(0.05)
    assert s1.values == {"adc0": 2048.0, "vref_mv": 3300.0}
    assert s2.device_t == pytest.approx(0.1)


def test_csv_decoder_errors_and_header_change() -> None:
    data = (
        b"t_us,a,b\n"
        b"1000,1,2\n"
        b"2000,1\n"  # missing column
        b"3000,x,2\n"  # not a number
        b"note without delimiter\n"
        b"4000,5,\n"  # empty field is a gap
        b"time,c\n"  # reset printed a new header
        b"7,9\n"
    )
    d = CsvDecoder()
    events = d.feed(data, 1.0)
    assert d.errors == {"columns": 1, "value": 1}
    vals = [(s.device_t, s.values) for s in samples(events)]
    assert vals == [(0.001, {"a": 1.0, "b": 2.0}), (0.004, {"a": 5.0}), (7.0, {"c": 9.0})]
    assert "note without delimiter" in texts(events)


def test_csv_rows_before_header_are_text() -> None:
    events = CsvDecoder().feed(b"1,2\n", 1.0)
    assert texts(events) == ["1,2"]


# -- JSON lines --------------------------------------------------------------------------


def test_flatten_numbers() -> None:
    obj = {"a": 1, "b": {"c": [1.5, True, "s"], "d": None}, "e": False, "f": float("inf")}
    assert flatten_numbers(obj) == {"a": 1.0, "b.c.0": 1.5, "b.c.1": 1.0, "e": 0.0}


def test_jsonl_decoder() -> None:
    data = (
        b'{"t_ms":1500,"temp":21.5,"imu":{"ax":0.1}}\n'
        b"plain text\n"
        b'{"temp": 21.5,\n'
        b'{"msg":"no numbers"}\n'
        b'{"millis":20}\n'
    )
    d = JsonLinesDecoder()
    events = feed_split(d, data, 4)
    s1, s2 = samples(events)
    assert s1.device_t == 1.5
    assert s1.values == {"temp": 21.5, "imu.ax": 0.1}
    assert s2.values == {}
    assert s2.device_t == pytest.approx(0.02)
    assert d.errors == {"json": 1}
    assert texts(events)[:2] == ["plain text", '{"temp": 21.5,']


# -- registry and auto-detection ---------------------------------------------------------


def test_registry() -> None:
    names = list(available_decoders())
    assert names[0] == "auto"
    assert {"text", "plotter", "csv", "jsonl", "ina219", "odometry"} <= set(names)
    assert create_decoder("csv").name == "csv"
    with pytest.raises(ValueError, match="unknown decoder"):
        create_decoder("nope")
    with pytest.raises(ValueError):
        register_decoder(AutoDecoder)


@pytest.mark.parametrize("kind", sorted(GENERATORS))
def test_auto_detects_every_demo_format(kind: str) -> None:
    records = GENERATORS[kind](6.0)
    chunks = chunk_records(records, start=1000.0, seed=3)
    auto = create_decoder("auto")
    reference = create_decoder(kind)
    got, want = [], []
    for c in chunks:
        got += auto.feed(c.data, c.t)
        want += reference.feed(c.data, c.t)
    got += auto.flush(chunks[-1].t)
    want += reference.flush(chunks[-1].t)
    assert isinstance(auto, AutoDecoder)
    assert auto.chosen is not None and auto.chosen.name == kind
    # Nothing received during detection is lost: same events as the explicit decoder.
    assert got == want
    assert auto.errors == reference.errors


def test_auto_detects_ina219_sample_session_on_flush() -> None:
    data = (DATA / "ina219_sample_session.log").read_bytes()
    auto = AutoDecoder([lambda: create_decoder("ina219"), TextDecoder])
    events = feed_split(auto, data, 7)
    assert auto.chosen is not None and auto.chosen.name == "ina219"
    assert len(samples(events)) == 5
    assert sum(auto.errors.values()) == 2


def test_auto_falls_back_to_text_after_byte_limit() -> None:
    auto = create_decoder("auto")
    assert isinstance(auto, AutoDecoder)
    line = b"just some log output, nothing numeric here\r\n"
    events = []
    while auto.detecting:
        events += auto.feed(line, 1.0)
    assert auto.chosen is not None and auto.chosen.name == "text"
    assert len(texts(events)) * len(line) >= MAX_DETECT_BYTES
    assert auto.feed(line, 1.0) != []


def test_auto_decides_after_time_limit_on_slow_stream() -> None:
    auto = create_decoder("auto")
    assert isinstance(auto, AutoDecoder)
    assert auto.feed(b"v:1\n", 0.0) == []
    assert auto.feed(b"v:2\n", 1.0) == []
    events = auto.feed(b"v:3\n", 3.5)
    assert auto.chosen is not None and auto.chosen.name == "plotter"
    assert len(samples(events)) == 3
