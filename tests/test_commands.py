"""Odometry command encoder and ``serial-scope send``, against the firmware's vectors."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import pytest

from serial_scope.cli import build_parser, main, run_send
from serial_scope.commands import (
    CommandError,
    OdometryCommander,
    parse_action,
    resolve_param,
)
from serial_scope.decoders.framing import cobs_decode
from serial_scope.decoders.odometry import decode_packet, encode_frame
from serial_scope.events import Chunk
from serial_scope.logfile import LogReader
from serial_scope.transport import FakeTransport, TransportError

# -- byte-exact against encoder-odometry-firmware's published vectors -----------------------


def _command_cases(vectors: dict[str, Any], name: str) -> list[dict[str, Any]]:
    cases = [c for c in vectors["messages"] if c["message"] == name]
    assert cases, name
    return cases


def test_velocity_commands_match_the_firmware_vectors(vectors: dict[str, Any]) -> None:
    for case in _command_cases(vectors, "velocity_command"):
        f = case["fields"]
        command = OdometryCommander(case["seq"]).velocity(f["vx"], f["vy"], f["wz"])
        assert command.frame.hex() == case["frame"], case["name"]
        assert command.seq == case["seq"]


def test_param_get_and_set_match_the_firmware_vectors(vectors: dict[str, Any]) -> None:
    (get,) = _command_cases(vectors, "param_get")
    (put,) = _command_cases(vectors, "param_set")
    pid = get["fields"]["id"]
    # By number and by name (id 1 is kp; id 9 is max_wheel_speed).
    assert OdometryCommander(get["seq"]).param_get(pid).frame.hex() == get["frame"]
    assert OdometryCommander(get["seq"]).param_get("kp").frame.hex() == get["frame"]
    value = put["fields"]["value"]
    command = OdometryCommander(put["seq"]).param_set("max_wheel_speed", value)
    assert command.frame.hex() == put["frame"]
    assert command.param_id == 9
    assert OdometryCommander(put["seq"]).param_set("9", value).frame.hex() == put["frame"]


def test_cli_words_build_the_same_bytes(vectors: dict[str, Any]) -> None:
    strafe = next(c for c in vectors["messages"] if c["name"] == "velocity strafe and turn")
    command = parse_action(["vel", "-0.25", "0.125", "1.5"], OdometryCommander(7))
    assert command.frame.hex() == strafe["frame"]
    (put,) = _command_cases(vectors, "param_set")
    command = parse_action(["set-param", "max_wheel_speed", "12.5"], OdometryCommander(3))
    assert command.frame.hex() == put["frame"]


def test_stop_is_a_zero_velocity_command(vectors: dict[str, Any]) -> None:
    stop = OdometryCommander(0).stop()
    assert stop.frame == encode_frame("velocity_command", 0, {"vx": 0.0, "vy": 0.0, "wz": 0.0})
    msg = decode_packet(cobs_decode(stop.frame[:-1]))
    assert msg.fields == {"vx": 0.0, "vy": 0.0, "wz": 0.0}
    assert stop.text == "stop (zero velocity)"


def test_sequence_increments_and_wraps() -> None:
    commander = OdometryCommander(254)
    seqs = [commander.velocity(0, 0, 0).seq for _ in range(3)]
    assert seqs == [254, 255, 0]
    assert commander.param_get("kp").seq == 1


@pytest.mark.parametrize(
    ("words", "message"),
    [
        ([], "missing action"),
        (["drive", "1"], "unknown action"),
        (["vel", "1", "2"], "usage: --odometry vel VX VY WZ"),
        (["vel", "1", "x", "0"], "not a number"),
        (["vel", "nan", "0", "0"], "finite"),
        (["vel", "0", "inf", "0"], "finite"),
        (["get-param", "kq"], "unknown parameter 'kq'"),
        (["get-param", "300"], "outside 0..255"),
        (["set-param", "protocol_version", "2"], "read-only"),
        (["set-param", "control_rate_hz", "50"], "read-only"),
        (["set-param", "kp", "1e999"], "finite"),
        (["stop", "now"], "usage: --odometry stop"),
    ],
)
def test_bad_commands_are_refused(words: list[str], message: str) -> None:
    with pytest.raises(CommandError, match=message):
        parse_action(words, OdometryCommander())


def test_resolve_param() -> None:
    assert resolve_param("watchdog_ms") == 10
    assert resolve_param(" 22 ") == 22
    assert resolve_param(99) == 99  # unknown ids are the firmware's to reject (status 1)


# -- serial-scope send ----------------------------------------------------------------------


def _args(*words: str) -> argparse.Namespace:
    return build_parser().parse_args(["send", *words])


def _frames(data: bytes) -> list[Any]:
    return [decode_packet(cobs_decode(f)) for f in data.split(b"\x00") if f]


def test_dry_run_prints_the_frames(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["send", "--dry-run", "--seq", "2", "--odometry", "get-param", "kp"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "+  0.000s  > get kp  seq 2  [08 01 05 02 01 01 68 4a 00]"


def test_param_get_reads_the_reply() -> None:
    reply = encode_frame("param_value", 40, {"id": 1, "status": 0, "value": 0.02})
    telemetry = encode_frame("set_pose", 39, {"x": 0.0, "y": 0.0, "theta": 0.0})
    transport = FakeTransport([None, telemetry, reply[:5], reply[5:]])
    out, err = io.StringIO(), io.StringIO()
    code = run_send(transport, _args("x", "--odometry", "get-param", "kp"), out, err)
    assert code == 0, err.getvalue()
    assert bytes(transport.written) == OdometryCommander(0).param_get("kp").frame
    assert "PARAM_VALUE kp = 0.02 (ok)" in out.getvalue()
    assert not transport.is_open


def test_param_set_rejected_by_the_firmware() -> None:
    reply = encode_frame("param_value", 0, {"id": 14, "status": 2, "value": 45.0})
    transport = FakeTransport([reply])
    err = io.StringIO()
    args = _args("x", "--odometry", "set-param", "roller_angle_deg", "95")
    assert run_send(transport, args, io.StringIO(), err) == 1
    assert "firmware rejected the request: out of range" in err.getvalue()


def test_reply_for_another_parameter_is_not_the_answer() -> None:
    other = encode_frame("param_value", 0, {"id": 2, "status": 0, "value": 0.2})
    transport = FakeTransport([other])
    err = io.StringIO()
    args = _args("x", "--odometry", "get-param", "kp", "--wait", "0.05")
    assert run_send(transport, args, io.StringIO(), err) == 4
    assert "no PARAM_VALUE reply within 0.05 s" in err.getvalue()


def test_hold_repeats_the_command_then_stops(tmp_path: Path) -> None:
    transport = FakeTransport()
    log = tmp_path / "send.sslog"
    out, err = io.StringIO(), io.StringIO()
    args = _args(
        "x",
        "--odometry",
        "vel",
        "0.2",
        "0",
        "0.1",
        "--hold",
        "0.3",
        "--wait",
        "0",
        "--log",
        str(log),
    )
    assert run_send(transport, args, out, err) == 0
    frames = _frames(bytes(transport.written))
    assert [f.seq for f in frames] == [0, 1, 2, 3]
    assert [f.fields["vx"] for f in frames] == pytest.approx([0.2, 0.2, 0.2, 0.0])
    assert frames[-1].fields == {"vx": 0.0, "vy": 0.0, "wz": 0.0}
    text = out.getvalue()
    assert "(repeating every 0.1 s)" in text
    assert "> stop (zero velocity)  seq 3" in text
    assert "sent 4 frame(s)" in err.getvalue()
    sent = [c.data for c in LogReader(log) if c.direction == "tx"]
    assert b"".join(sent) == bytes(transport.written)


class _InterruptingTransport(FakeTransport):
    def read(self, timeout: float) -> Chunk | None:
        raise KeyboardInterrupt


def test_interrupt_during_vel_sends_stop() -> None:
    transport = _InterruptingTransport()
    err = io.StringIO()
    args = _args("x", "--odometry", "vel", "0.3", "0", "0", "--hold", "5")
    assert run_send(transport, args, io.StringIO(), err) == 130
    frames = _frames(bytes(transport.written))
    assert frames[0].fields["vx"] == pytest.approx(0.3)
    assert frames[-1].fields == {"vx": 0.0, "vy": 0.0, "wz": 0.0}
    assert [f.seq for f in frames] == [0, 1]


def test_port_failures_and_bad_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    transport = FakeTransport(open_failures=1)
    err = io.StringIO()
    assert run_send(transport, _args("x", "--odometry", "stop"), io.StringIO(), err) == 2
    assert "device not present" in err.getvalue()

    class _Unplugged(FakeTransport):
        def write(self, data: bytes) -> None:
            raise TransportError("unplugged")

    err = io.StringIO()
    unplugged = _Unplugged()
    assert run_send(unplugged, _args("x", "--odometry", "stop"), io.StringIO(), err) == 2
    assert "unplugged" in err.getvalue()
    assert main(["send", "--odometry", "stop"]) == 2
    assert "SOURCE is required" in capsys.readouterr().err
    assert main(["send", "--dry-run", "--odometry", "get-param", "kp", "--hold", "1"]) == 2
    assert "--hold only applies to vel" in capsys.readouterr().err
    assert main(["send", "tcp://nohost", "--odometry", "stop"]) == 2


def test_send_over_pyserial_loopback(capsys: pytest.CaptureFixture[str]) -> None:
    # loop:// echoes the request; the decoder shows it, and no reply arrives.
    code = main(["send", "loop://", "--odometry", "get-param", "kp", "--wait", "0.1"])
    assert code == 4
    captured = capsys.readouterr()
    assert "PARAM_GET kp" in captured.out
    assert "no PARAM_VALUE reply" in captured.err
    assert main(["send", "loop://", "--odometry", "stop"]) == 0
    assert "VELOCITY_COMMAND vx=0 vy=0 wz=0" in capsys.readouterr().out
