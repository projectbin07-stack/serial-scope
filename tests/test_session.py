from __future__ import annotations

import csv
from pathlib import Path

import pytest

from serial_scope.csvexport import CsvExporter, format_value
from serial_scope.decoders.ina219 import encode_bm1
from serial_scope.events import INFO, MARK, RX, TX, Chunk, Sample, TriggerFired
from serial_scope.logfile import LogReader
from serial_scope.session import Session
from serial_scope.triggers import TriggerSet, parse_trigger

T0 = 1_700_000_000.0


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_session_decodes_logs_and_exports(tmp_path: Path) -> None:
    s = Session("plotter")
    s.start_logging(tmp_path / "run.sslog", {"source": "test"}, start_time=T0)
    s.start_csv(tmp_path / "run.csv")
    data = [b"hello\r\na:1,", b"b:2\r\n", b"a:3\r\n", b"a:4,c:5\r\n"]
    events = []
    for i, d in enumerate(data):
        events += s.process(Chunk(d, T0 + i))
    s.record_tx(b"RESET\n", T0 + 10)
    s.close()
    assert s.totals.samples == 3
    assert s.totals.text_lines == 1
    assert s.totals.rx_bytes == sum(map(len, data))
    assert s.totals.tx_bytes == 6
    assert s.channels.names() == ["a", "b", "c"]
    # The raw log holds exactly what came in, plus what went out.
    back = list(LogReader(tmp_path / "run.sslog"))
    assert b"".join(c.data for c in back if c.direction == RX) == b"".join(data)
    assert [c.data for c in back if c.direction == TX] == [b"RESET\n"]
    assert LogReader(tmp_path / "run.sslog").info.meta["decoder"] == "plotter"
    rows = _read_csv(tmp_path / "run.csv")
    assert rows[0] == ["host_time", "device_time", "source", "a", "b", "c"]
    assert rows[1] == [f"{T0:.6f}", "", "plotter", "1", "2", ""]
    assert rows[3] == [f"{T0 + 3:.6f}", "", "plotter", "4", "", "5"]


def test_csv_exporter_rewrites_once_per_new_channel(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    with CsvExporter(path) as exp:
        exp.write(Sample(1.0, {"a": 1.5}, device_t=0.25, source="s"))
        exp.write(Sample(2.0, {"a": 2.0}))
        exp.write(Sample(3.0, {"b": -1.0}))
        exp.write(Sample(4.0, {"a": 1.0, "b": 2.0}))
    assert exp.rewrites == 1
    assert _read_csv(path) == [
        ["host_time", "device_time", "source", "a", "b"],
        ["1.000000", "0.250000", "s", "1.5", ""],
        ["2.000000", "", "", "2", ""],
        ["3.000000", "", "", "", "-1"],
        ["4.000000", "", "", "1", "2"],
    ]
    with pytest.raises(ValueError):
        exp.write(Sample(5.0, {"a": 1.0}))


def test_format_value() -> None:
    assert format_value(None) == ""
    assert format_value(float("nan")) == ""
    assert format_value(3.0) == "3"
    assert format_value(0.1) == "0.1"
    assert format_value(1e20) == "1e+20"


def test_trigger_actions(tmp_path: Path) -> None:
    triggers = TriggerSet(
        [
            parse_trigger("v > 5 -> mark,beep,snapshot"),
            parse_trigger("re:FATAL -> stop:1.5"),
        ]
    )
    s = Session("plotter", triggers=triggers, snapshot_dir=tmp_path / "snaps")
    s.start_logging(tmp_path / "t.sslog", start_time=T0)
    events = []
    events += s.process(Chunk(b"v:1\nv:7\n", T0))
    events += s.process(Chunk(b"FATAL: out of memory\n", T0 + 1))
    events += s.process(Chunk(b"v:2\n", T0 + 2))  # still logged (1 s after FATAL)
    events += s.process(Chunk(b"v:9\n", T0 + 3))  # logging stopped before this one
    fired = [e for e in events if isinstance(e, TriggerFired)]
    assert [f.trigger for f in fired] == ["v > 5", "re:FATAL", "v > 5"]
    assert s.pending_beeps == 2
    assert s.logger is None
    assert len(s.snapshots) == 2
    snap = _read_csv(s.snapshots[0])
    assert snap == [
        ["channel", "time", "value"],
        ["v", f"{T0:.6f}", "1"],
        ["v", f"{T0:.6f}", "7"],
    ]
    back = list(LogReader(tmp_path / "t.sslog"))
    rx = b"".join(c.data for c in back if c.direction == RX)
    assert rx == b"v:1\nv:7\nFATAL: out of memory\nv:2\n"
    marks = [c.data.decode() for c in back if c.direction == MARK]
    assert marks == ["v > 5: v = 7 > 5"]
    assert [c.data for c in back if c.direction == INFO] == [b"logging stopped by trigger"]
    assert len(s.markers) == 2  # the second v > 5 happened after logging stopped


def test_replayed_markers_are_collected() -> None:
    s = Session("text")
    s.process(Chunk(b"user mark", T0, MARK))
    s.process(Chunk(b"connected", T0, INFO))
    assert [m.label for m in s.markers] == ["user mark"]
    assert s.totals.rx_bytes == 0


def test_decoder_name_and_counts() -> None:
    s = Session()
    assert s.decoder_name == "auto (detecting)"

    def frame(seq: int) -> bytes:
        return (encode_bm1([seq, seq * 100, 4000, 0, 0, 0, 0, 500, 0]) + "\r\n").encode()

    s.process(Chunk(b"".join(frame(i) for i in (0, 1, 2, 3)), T0))
    s.process(Chunk(b"$BM1,4,0,0,0,0,0,0,0,0*00\r\n" + frame(9) + frame(0), T0 + 1))
    s.finish()
    assert s.decoder_name == "auto (ina219)"
    assert s.error_counts() == {"crc": 1}
    assert s.sequence_counts() == {"dropped": 5, "restarts": 1}


def test_mark_and_info_without_logger() -> None:
    s = Session("text")
    marker = s.mark("hello", T0)
    s.record_info("nothing to log to")
    assert marker.label == "hello"
    assert s.finish() == []
