from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from conftest import DATA
from serial_scope import __version__
from serial_scope.cli import format_event, log_stats, main
from serial_scope.demo import write_demo_log
from serial_scope.events import DecodeError
from serial_scope.logfile import LogReader, LogWriter, log_parts
from serial_scope.ports import PortInfo


@pytest.fixture
def plotter_log(tmp_path: Path) -> Path:
    path = tmp_path / "demo.sslog"
    write_demo_log(path, "plotter", seconds=12)
    return path


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"serial-scope {__version__}"


def test_demo_and_stats(plotter_log: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["stats", str(plotter_log)]) == 0
    out = capsys.readouterr().out
    assert "source      demo:plotter  (synthetic demo data)" in out
    assert "decoder     auto (plotter)" in out
    assert "errors      none" in out
    assert "setpoint" in out and "speed" in out and "duty" in out
    assert main(["stats", str(plotter_log), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["decoder"] == "auto (plotter)"
    assert info["samples"] == 601  # 50 Hz for 12 s, both ends included
    names = [c["name"] for c in info["channels"]]
    assert names == ["setpoint", "speed", "duty"]
    setpoint = info["channels"][0]
    assert (setpoint["min"], setpoint["max"]) == (0.0, 150.0)
    assert setpoint["rate_hz"] == pytest.approx(50.0, rel=0.01)


def test_demo_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "x.sslog"
    assert main(["demo", str(out), "--kind", "csv", "--seconds", "2"]) == 0
    assert "synthetic 'csv' session" in capsys.readouterr().out
    assert LogReader(out).info.meta["synthetic"] is True


def test_replay_with_csv_triggers_and_log(
    plotter_log: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = tmp_path / "out.csv"
    relog = tmp_path / "again.sslog"
    code = main(
        [
            "replay",
            str(plotter_log),
            "--csv",
            str(csv_path),
            "--log",
            str(relog),
            "--trigger",
            "speed > 160 -> mark,snapshot",
            "--trigger",
            "re:RSSI -> mark",
            "--snapshot-dir",
            str(tmp_path / "snaps"),
            "--show",
            "text",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "[wifi] RSSI" in captured.out
    assert "* trigger speed > 160: speed = " in captured.out
    assert "setpoint:" not in captured.out  # --show text hides samples
    assert "decoder auto (plotter)" in captured.err
    assert "3 markers" in captured.err
    with csv_path.open(newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["host_time", "device_time", "source", "setpoint", "speed", "duty"]
    assert len(rows) == 602
    # The re-recorded log contains the same received bytes as the original.
    original = b"".join(c.data for c in LogReader(plotter_log) if c.direction == "rx")
    again = b"".join(c.data for c in LogReader(relog) if c.direction == "rx")
    assert again == original
    assert len(list((tmp_path / "snaps").glob("snapshot-*.csv"))) == 1


def test_replay_raw_and_hex_output(plotter_log: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["replay", str(plotter_log), "--show", "raw", "-q", "--duration", "5"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ets Jun  8 2016")
    assert "setpoint:0,speed:" in out
    assert main(["replay", str(plotter_log), "--show", "hex", "-q"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("00000000  65 74 73")


def test_replay_samples_mode_and_absolute_time(
    plotter_log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["replay", str(plotter_log), "--show", "samples", "--abs-time", "-q"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 601
    assert ":" in lines[0][:8] and "setpoint=0" in lines[0]


def test_max_errors_exit_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "ina.sslog"
    write_demo_log(log, "ina219", seconds=8)
    assert main(["replay", str(log), "--show", "none", "--max-errors", "0"]) == 3
    assert "exceed --max-errors 0" in capsys.readouterr().err
    assert main(["replay", str(log), "--show", "none", "-q", "--max-errors", "1"]) == 0
    assert main(["stats", str(log), "--max-errors", "0"]) == 3
    out = capsys.readouterr().out
    assert "errors      crc 1" in out
    assert "sequence    dropped 1" in out


def test_stats_on_sample_session_wrapped_in_a_log(tmp_path: Path) -> None:
    data = (DATA / "ina219_sample_session.log").read_bytes()
    path = tmp_path / "s.sslog"
    with LogWriter(path, start_time=1000.0) as w:
        from serial_scope.events import Chunk

        for i in range(0, len(data), 10):
            w.write(Chunk(data[i : i + 10], 1000.0 + i / 1000))
    info = log_stats(str(path))
    assert info.decoder == "auto (ina219)"
    assert info.samples == 5
    assert info.errors == {"crc": 1, "format": 1}
    assert info.sequence == {"dropped": 1, "restarts": 1}
    assert info.decode_errors == 2


def test_rotated_logging_from_cli(plotter_log: Path, tmp_path: Path) -> None:
    out = tmp_path / "rot.sslog"
    args = ["replay", str(plotter_log), "--log", str(out), "--rotate-mb", "0.01", "-q"]
    assert main([*args, "--show", "none"]) == 0
    parts = log_parts(out)
    assert len(parts) >= 3
    original = b"".join(c.data for c in LogReader(plotter_log) if c.direction == "rx")
    again = b"".join(c.data for c in LogReader(out) if c.direction == "rx")
    assert again == original


def test_monitor_loopback_send(capsys: pytest.CaptureFixture[str]) -> None:
    """End to end through pyserial's loop:// device: what is sent comes back decoded."""
    code = main(
        [
            "monitor",
            "loop://",
            "--send",
            r"temp:21.5,hum:40\r\n",
            "--duration",
            "0.5",
            "--decode",
            "plotter",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "temp=21.5 hum=40" in captured.out
    assert "[connected to loop://@115200]" in captured.err


def test_monitor_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["monitor", "tcp://nohost"]) == 2
    assert "expected tcp://HOST:PORT" in capsys.readouterr().err
    code = main(["monitor", "no-such-port-xyz", "--no-reconnect", "--duration", "1", "-q"])
    assert code == 2
    assert "cannot open no-such-port-xyz" in capsys.readouterr().err
    assert main(["replay", "missing.sslog"]) == 2
    assert main(["stats", "missing.sslog"]) == 2
    assert main(["replay", "x.sslog", "--trigger", "v ~ 1"]) == 2


def test_list_and_decoders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = [
        PortInfo("COM5", "Silicon Labs CP210x USB to UART Bridge (COM5)", 0x10C4, 0xEA60),
        PortInfo("COM1", "Communications Port (COM1)"),
    ]
    monkeypatch.setattr("serial_scope.cli.list_ports", lambda: fake)
    assert main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("COM5  10C4:EA60  Silicon Labs CP210x")
    assert lines[0].endswith("[Silicon Labs CP210x (ESP32 DevKit, NodeMCU)]")
    assert lines[1].startswith("COM1  -")
    assert main(["list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["usb_id"] == "10C4:EA60" and data[1]["usb_id"] is None
    monkeypatch.setattr("serial_scope.cli.list_ports", lambda: [])
    assert main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "no serial ports found"
    assert main(["decoders"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("auto ")
    assert "odometry" in out and "ina219" in out


def test_format_event_error() -> None:
    from serial_scope.cli import _Clock

    clock = _Clock(absolute=False)
    assert format_event(DecodeError(5.0, "crc", "bad"), clock) == "+    0.000s  ! crc: bad"
