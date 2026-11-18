"""GUI logic without Tk: the controller behind the buttons."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from serial_scope.app import AppController, UiUpdate
from serial_scope.demo import write_demo_log
from serial_scope.logfile import LogReader
from serial_scope.transport import FakeTransport


def _poll_until_finished(ctl: AppController, timeout: float = 20.0) -> list[UiUpdate]:
    updates = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        update = ctl.poll()
        updates.append(update)
        if update.finished:
            return updates
        time.sleep(0.01)
    raise AssertionError("replay did not finish")


def test_replay_demo_fills_channels_and_tables(tmp_path: Path) -> None:
    log = tmp_path / "demo.sslog"
    write_demo_log(log, "ina219", seconds=5)
    ctl = AppController(snapshot_dir=tmp_path)
    ctl.add_trigger("current_ma > 140 -> mark,beep")
    ctl.open_replay(log, speed=0)
    updates = _poll_until_finished(ctl)
    assert ctl.is_replay
    new_channels = [n for u in updates for n in u.new_channels]
    assert new_channels == [
        "bus_v",
        "current_ma",
        "power_mw",
        "charge_mah",
        "energy_mwh",
        "soc_pct",
        "flags",
    ]
    terminal = "".join(text for u in updates for text, _ in u.terminal)
    assert "#ina219-battery-monitor 0.1.0\n" in terminal
    assert "$BM1,0," in terminal
    assert "[connected to replay:demo.sslog]" in terminal
    assert sum(u.beeps for u in updates) == 1
    assert any("trigger" in line for u in updates for line in u.events)
    rows = ctl.stats_rows()
    assert rows[0][0] == "bus_v" and rows[0][6] == "50"
    counters = dict(ctl.counter_rows())
    assert counters["decoder"] == "auto (ina219)"
    assert counters["markers"] == "1"
    assert counters["trigger: current_ma > 140"] == "fired 1"
    ctl.close()


def test_replay_shows_decode_errors_as_events(tmp_path: Path) -> None:
    log = tmp_path / "odo.sslog"
    write_demo_log(log, "odometry", seconds=6)
    ctl = AppController()
    ctl.hex_mode = True
    ctl.open_replay(log, speed=0)
    updates = _poll_until_finished(ctl)
    events = [line for u in updates for line in u.events]
    assert any("error  overlong" in line for line in events)
    assert any("error  crc" in line for line in events)
    terminal = "".join(text for u in updates for text, _ in u.terminal)
    assert terminal.splitlines()[1].startswith("00000000  65 74 73 20")  # "ets " in hex
    counters = dict(ctl.counter_rows())
    assert counters["error: crc"] == "1"
    assert counters["sequence: lost"] == "1"
    ctl.close()


def test_send_record_mark_and_snapshot_with_fake_device(tmp_path: Path) -> None:
    ctl = AppController(decoder="plotter", snapshot_dir=tmp_path)
    fake = FakeTransport([None, b"a:1\r\n", b"a:2\r\n"] + [None] * 1000, sleep=time.sleep)
    with pytest.raises(ValueError, match="not connected"):
        ctl.send("x")
    ctl.start(fake)
    deadline = time.monotonic() + 5
    while not ctl.connected and time.monotonic() < deadline:
        ctl.poll()
        time.sleep(0.01)
    assert ctl.connected
    path = ctl.start_recording(tmp_path / "rec.sslog", max_mb=1)
    assert ctl.recording
    sent = ctl.send("RATE 100", "CR+LF")
    assert sent == b"RATE 100\r\n"
    assert ctl.send("01 02 ff", hex_mode=True) == b"\x01\x02\xff"
    with pytest.raises(ValueError):
        ctl.send("zz", hex_mode=True)
    deadline = time.monotonic() + 5
    while "a" not in ctl.session.channels and time.monotonic() < deadline:
        ctl.poll()
        time.sleep(0.01)
    ctl.mark("button")
    snap = ctl.snapshot()
    assert snap.exists()
    ctl.stop_recording()
    assert not ctl.recording
    assert fake.written == b"RATE 100\r\n\x01\x02\xff"
    records = list(LogReader(path))
    kinds = [r.direction for r in records]
    assert "tx" in kinds and "mark" in kinds
    counters = dict(ctl.counter_rows())
    assert counters["bytes sent"] == "13"
    ctl.close()
    assert not ctl.running


def test_set_decoder_resets_session() -> None:
    ctl = AppController()
    old = ctl.session
    ctl.set_decoder("jsonl")
    assert ctl.session is not old
    assert ctl.session.decoder_name == "jsonl"
    assert ctl.poll() == UiUpdate()  # nothing running
    ctl.close()
