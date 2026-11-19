"""GUI logic without Tk: the controller behind the buttons."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from serial_scope.app import AppController, UiUpdate
from serial_scope.demo import write_demo_log
from serial_scope.logfile import LogReader
from serial_scope.transport import FakeTransport, TransportError


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


# -- odometry command panel -------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _connected(
    script: list[bytes | None], decoder: str = "odometry"
) -> tuple[AppController, FakeTransport, _Clock]:
    clock = _Clock()
    ctl = AppController(decoder=decoder, clock=clock)
    fake = FakeTransport([None, *script] + [None] * 2000, sleep=time.sleep)
    ctl.start(fake)
    deadline = time.monotonic() + 5
    while not ctl.connected and time.monotonic() < deadline:
        ctl.poll()
        time.sleep(0.01)
    assert ctl.connected
    return ctl, fake, clock


def _sent_frames(fake: FakeTransport) -> list[tuple[str, int, dict[str, object]]]:
    from serial_scope.decoders.framing import cobs_decode
    from serial_scope.decoders.odometry import decode_packet

    out = []
    for raw in bytes(fake.written).split(b"\x00"):
        if raw:
            msg = decode_packet(cobs_decode(raw))
            out.append((msg.name, msg.seq, msg.fields))
    return out


def test_command_target_follows_the_decoder(tmp_path: Path) -> None:
    assert AppController(decoder="odometry").command_target == "odometry"
    ctl = AppController()
    assert ctl.command_target is None  # auto, still detecting
    log = tmp_path / "odo.sslog"
    write_demo_log(log, "odometry", seconds=3)
    ctl.open_replay(log, speed=0)
    _poll_until_finished(ctl)
    assert ctl.command_target == "odometry"
    with pytest.raises(ValueError, match="not connected"):
        ctl.send_velocity(0.1, 0.0, 0.0)  # a replay cannot be commanded
    ctl.set_decoder("plotter")
    assert ctl.command_target is None
    ctl.close()


def test_velocity_repeat_stop_and_parameters() -> None:
    from serial_scope.decoders.odometry import encode_frame

    reply = encode_frame("param_value", 9, {"id": 10, "status": 0, "value": 500.0})
    ctl, fake, clock = _connected([])
    command = ctl.send_velocity(0.2, 0.0, 0.1, repeat=True)
    assert command.text.startswith("velocity vx=0.2")
    assert ctl.repeat_velocity == (0.2, 0.0, 0.1)
    ctl.poll()  # less than 0.1 s later: nothing repeated
    clock.now += 0.1
    ctl.poll()
    clock.now += 0.25
    ctl.poll()
    ctl.stop_motors()
    clock.now += 1.0
    ctl.poll()  # stopped: no more repeats
    ctl.send_param_get("watchdog_ms")
    ctl.send_param_set("kp", 0.05)
    frames = _sent_frames(fake)
    assert [(name, seq) for name, seq, _ in frames] == [
        ("velocity_command", 0),
        ("velocity_command", 1),
        ("velocity_command", 2),
        ("velocity_command", 3),
        ("param_get", 4),
        ("param_set", 5),
    ]
    assert frames[3][2] == {"vx": 0.0, "vy": 0.0, "wz": 0.0}
    assert frames[4][2] == {"id": 10}
    counters = dict(ctl.counter_rows())
    assert int(counters["bytes sent"]) == len(fake.written)
    # The reply arrives as a decoded frame and is reported to the panel.
    fake.script.insert(0, reply)
    deadline = time.monotonic() + 5
    replies: list[str] = []
    while not replies and time.monotonic() < deadline:
        update = ctl.poll()
        replies += update.replies
        time.sleep(0.01)
    assert replies == ["PARAM_VALUE watchdog_ms = 500 (ok)"]
    ctl.close()
    assert ctl.repeat_velocity is None


def test_repeat_stops_when_the_link_fails() -> None:
    class _Flaky(FakeTransport):
        writes = 0

        def write(self, data: bytes) -> None:
            self.writes += 1
            if self.writes > 1:
                raise TransportError("unplugged")
            super().write(data)

    clock = _Clock()
    ctl = AppController(decoder="odometry", clock=clock)
    fake = _Flaky([None] * 2000, sleep=time.sleep)
    ctl.start(fake)
    deadline = time.monotonic() + 5
    while not ctl.connected and time.monotonic() < deadline:
        ctl.poll()
        time.sleep(0.01)
    ctl.send_velocity(0.3, 0.0, 0.0, repeat=True)
    clock.now += 0.2
    update = ctl.poll()
    assert update.repeat_stopped
    assert ctl.repeat_velocity is None
    assert any("velocity repeat stopped" in s for s in update.status)
    with pytest.raises(ValueError):
        ctl.stop_motors()
    ctl.close()
