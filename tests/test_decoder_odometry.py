from __future__ import annotations

import random
import struct
from typing import Any

import pytest

from conftest import errors, feed_split, samples, texts
from serial_scope.decoders.framing import cobs_encode, crc16_ccitt_false
from serial_scope.decoders.odometry import (
    MAX_FRAME_BYTES,
    FrameError,
    OdometryDecoder,
    decode_packet,
    encode_frame,
    encode_packet,
)
from serial_scope.events import Sample, TextLine


def _approx_fields(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        if isinstance(value, list):
            assert actual[key] == pytest.approx(value), key
        else:
            assert actual[key] == pytest.approx(value), key


def test_message_vectors_decode(vectors: dict[str, Any]) -> None:
    for case in vectors["messages"]:
        msg = decode_packet(bytes.fromhex(case["packet"]))
        assert msg.name == case["message"], case["name"]
        assert msg.type == case["type"]
        assert msg.seq == case["seq"]
        _approx_fields(msg.fields, case["fields"])


def test_message_vectors_encode(vectors: dict[str, Any]) -> None:
    for case in vectors["messages"]:
        packet = encode_packet(case["message"], case["seq"], case["fields"])
        assert packet.hex() == case["packet"], case["name"]
        assert encode_frame(case["message"], case["seq"], case["fields"]).hex() == case["frame"]


def test_invalid_vectors_report_the_documented_kind(vectors: dict[str, Any]) -> None:
    for case in vectors["invalid"]:
        decoder = OdometryDecoder()
        events = decoder.feed(bytes.fromhex(case["frame"]), 1.0)
        assert samples(events) == []
        (err,) = errors(events)
        assert err.kind == case["error"], case["name"]
        assert decoder.errors[case["error"]] == 1


def test_packet_errors_directly() -> None:
    with pytest.raises(FrameError) as info:
        decode_packet(b"\x01\x02\x03")
    assert info.value.kind == "short"


def _all_vector_frames(vectors: dict[str, Any]) -> bytes:
    return b"".join(bytes.fromhex(c["frame"]) for c in vectors["messages"])


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64, 4096])
def test_stream_split_at_any_chunk_size(vectors: dict[str, Any], size: int) -> None:
    decoder = OdometryDecoder()
    events = feed_split(decoder, _all_vector_frames(vectors), size)
    assert errors(events) == []
    assert decoder.counts["frames"] == len(vectors["messages"])
    # wheel_state, odometry, diagnostics, 2 velocity commands, param_value x2, set_pose
    names = [s.source for s in samples(events)]
    assert names.count("wheel_state") == 1
    assert names.count("odometry") == 1
    assert names.count("diagnostics") == 1
    assert "PARAM_GET kp" in texts(events)
    assert "PARAM_SET max_wheel_speed = 12.5" in texts(events)
    assert "PARAM_VALUE roller_angle_deg = 45 (out of range)" in texts(events)


def test_wheel_state_channels(vectors: dict[str, Any]) -> None:
    case = next(c for c in vectors["messages"] if c["message"] == "wheel_state")
    events = OdometryDecoder().feed(bytes.fromhex(case["frame"]), 5.0)
    (sample,) = samples(events)
    assert sample.t == 5.0
    assert sample.device_t == pytest.approx(0.123456)
    assert sample.values["fl.count"] == 1000
    assert sample.values["rr.count"] == -2147483648
    assert sample.values["fr.velocity"] == -10.5
    assert sample.values["rr.target"] == 3.0
    assert sample.values["fl.duty"] == 0.5  # 5000 / 10000
    assert sample.values["rr.duty"] == 1.0
    assert len(sample.values) == 16


def test_odometry_and_diagnostics_channels(vectors: dict[str, Any]) -> None:
    frames = b"".join(
        bytes.fromhex(c["frame"])
        for c in vectors["messages"]
        if c["message"] in ("odometry", "diagnostics")
    )
    odo, diag = samples(OdometryDecoder().feed(frames, 1.0))
    assert odo.values == {
        "odom.x": 1.5,
        "odom.y": -2.25,
        "odom.theta": 3.0,
        "odom.vx": 0.25,
        "odom.vy": -0.125,
        "odom.wz": 0.5,
    }
    assert odo.device_t == pytest.approx(4294.967295)
    assert diag.values["diag.illegal_rr"] == 65536
    assert diag.values["diag.rx_frame_errors"] == 6
    assert diag.values["diag.flags"] == 5


def test_garbage_between_frames_is_counted_and_frames_survive(vectors: dict[str, Any]) -> None:
    good = [bytes.fromhex(c["frame"]) for c in vectors["messages"]]
    rng = random.Random(7)
    stream = bytearray(b"ets Jun  8 2016 00:22:57\r\n" * 4)  # ESP32 ROM text, no zero bytes
    stream += b"\x00"
    expected_errors = 1  # the boot text: overlong
    for frame in good:
        stream += frame
        noise = bytes(rng.randrange(1, 256) for _ in range(rng.randint(1, 20))) + b"\x00"
        stream += noise
        expected_errors += 1
    decoder = OdometryDecoder()
    events = feed_split(decoder, bytes(stream), 3)
    assert decoder.counts["frames"] == len(good)
    assert sum(decoder.errors.values()) == expected_errors
    assert decoder.errors["overlong"] == 1
    assert len(errors(events)) == expected_errors


def test_corrupted_crc_in_stream_is_reported_not_dropped(vectors: dict[str, Any]) -> None:
    case = vectors["messages"][0]
    packet = bytearray(bytes.fromhex(case["packet"]))
    packet[5] ^= 0x40  # flip a body bit, keep the old CRC
    bad = cobs_encode(bytes(packet)) + b"\x00"
    good = bytes.fromhex(case["frame"])
    decoder = OdometryDecoder()
    events = feed_split(decoder, good + bad + good, 4)
    assert decoder.errors == {"crc": 1}
    assert decoder.counts["frames"] == 2
    (err,) = errors(events)
    assert "CRC mismatch" in err.detail
    assert err.raw == bad[:-1]


def test_overlong_frame_is_one_error_and_bounded() -> None:
    decoder = OdometryDecoder()
    junk = bytes([0x55]) * (MAX_FRAME_BYTES * 50)
    events = feed_split(decoder, junk + b"\x00", 9)
    assert [e.kind for e in errors(events)] == ["overlong"]
    assert len(decoder._buf) == 0


def test_empty_frames_between_delimiters_are_ignored(vectors: dict[str, Any]) -> None:
    decoder = OdometryDecoder()
    frame = bytes.fromhex(vectors["messages"][0]["frame"])
    events = decoder.feed(b"\x00\x00" + frame + b"\x00\x00", 1.0)
    assert errors(events) == []
    assert decoder.counts["frames"] == 1


def test_sequence_gaps_are_counted_as_lost() -> None:
    decoder = OdometryDecoder()
    fields = {"x": 0.0, "y": 0.0, "theta": 0.0}
    stream = b"".join(encode_frame("set_pose", seq, fields) for seq in (250, 251, 254, 255, 2))
    decoder.feed(stream, 1.0)
    # 251 -> 254 loses 252, 253; 255 -> 2 wraps and loses 0, 1
    assert decoder.counts["lost"] == 4


def test_device_time_unwraps_32_bit_microseconds() -> None:
    decoder = OdometryDecoder()
    base = {"x": 0.0, "y": 0.0, "theta": 0.0, "vx": 0.0, "vy": 0.0, "wz": 0.0}
    times = [2**32 - 1_000_000, 2**32 - 1, 500_000, 1_500_000, 1000]
    stream = b"".join(encode_frame("odometry", i, dict(base, t_us=t)) for i, t in enumerate(times))
    device_times = [s.device_t for s in samples(decoder.feed(stream, 1.0))]
    assert device_times[0] == pytest.approx((2**32 - 1_000_000) / 1e6)
    assert device_times[2] == pytest.approx((2**32 + 500_000) / 1e6)
    assert device_times[3] == pytest.approx((2**32 + 1_500_000) / 1e6)
    # A small step backwards is a reboot, not a wrap: the offset resets.
    assert device_times[4] == pytest.approx(0.001)


def test_velocity_command_produces_text_and_sample() -> None:
    frame = encode_frame("velocity_command", 3, {"vx": 0.5, "vy": 0.0, "wz": -1.0})
    events = OdometryDecoder().feed(frame, 2.0)
    assert isinstance(events[0], TextLine)
    assert events[0].text == "VELOCITY_COMMAND vx=0.5 vy=0 wz=-1"
    assert isinstance(events[1], Sample)
    assert events[1].values == {"cmd.vx": 0.5, "cmd.vy": 0.0, "cmd.wz": -1.0}


def test_unknown_param_id_and_status_names() -> None:
    frame = encode_frame("param_value", 0, {"id": 99, "status": 1, "value": 0.0})
    events = OdometryDecoder().feed(frame, 2.0)
    assert texts(events) == ["PARAM_VALUE id99 = 0 (unknown id)"]
    assert samples(events)[0].values == {"param.id99": 0.0}


def test_length_field_checked_against_body() -> None:
    body = struct.pack("<3f", 1.0, 2.0, 3.0)
    payload = bytes([1, 0x08, 0, 12]) + body + b"\x00"  # one byte too many
    crc = crc16_ccitt_false(payload)
    packet = payload + bytes([crc & 0xFF, crc >> 8])
    with pytest.raises(FrameError) as info:
        decode_packet(packet)
    assert info.value.kind == "length"
