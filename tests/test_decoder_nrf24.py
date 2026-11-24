"""nrf24-telemetry-link gateway decoder, checked against that project's shared vectors."""

from __future__ import annotations

import random
from typing import Any

import pytest

from conftest import errors, feed_split, notices, samples, texts
from serial_scope.decoders.framing import cobs_decode, cobs_encode, crc16_ccitt_false
from serial_scope.decoders.nrf24 import (
    MAX_BODY,
    MAX_FRAME_BYTES,
    STATS_FIELDS,
    STATS_FORMAT,
    GatewayFrameError,
    MessageError,
    Nrf24GatewayDecoder,
    decode_message,
    decode_packet,
    encode_message,
    encode_packet,
    encode_wire,
)
from serial_scope.events import Notice, Sample
from serial_scope.session import Session

# -- vectors: shared checksum, stuffing, messages and serial packets ------------------------


def test_crc_and_cobs_vectors(nrf24_vectors: dict[str, Any]) -> None:
    for case in nrf24_vectors["crc16"]:
        assert crc16_ccitt_false(case["ascii"].encode()) == int(case["crc"], 16)
    for case in nrf24_vectors["cobs"]:
        raw = bytes.fromhex(case["raw"])
        assert cobs_encode(raw).hex() == case["encoded"]
        assert cobs_decode(bytes.fromhex(case["encoded"])) == raw


def _same_fields(actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _same_fields(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, e in zip(actual, expected, strict=True):
            _same_fields(a, e)
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected)
    else:
        assert actual == expected
        assert type(actual) is type(expected)


def test_message_vectors_decode_and_encode(nrf24_vectors: dict[str, Any]) -> None:
    for case in nrf24_vectors["messages"]:
        data = bytes.fromhex(case["hex"])
        name, fields = decode_message(data)
        assert name == case["type"], case["name"]
        _same_fields(fields, case["fields"])
        assert encode_message(case["type"], case["fields"]).hex() == case["hex"], case["name"]


def test_invalid_message_vectors_are_not_decoded_as_their_type(
    nrf24_vectors: dict[str, Any],
) -> None:
    # Same rule as the upstream host tests: each case either fails to decode or decodes
    # as a different type than the one it pretends to be.
    rejected = 0
    for case in nrf24_vectors["invalid_messages"]:
        try:
            name, _ = decode_message(bytes.fromhex(case["hex"]))
        except MessageError:
            rejected += 1
            continue
        assert name != case["type"], case["name"]
    assert (len(nrf24_vectors["invalid_messages"]), rejected) == (12, 11)


def test_invalid_messages_inside_valid_packets_are_message_errors(
    nrf24_vectors: dict[str, Any],
) -> None:
    decoder = Nrf24GatewayDecoder()
    stream = b"".join(
        encode_wire(0x01, seq, b"\x01\x01\x00" + bytes.fromhex(case["hex"]))
        for seq, case in enumerate(nrf24_vectors["invalid_messages"])
    )
    events = feed_split(decoder, stream, 5)
    # "odometry with imu type" is a well-formed (all-zero) IMU message.
    assert [s.source for s in samples(events)] == ["imu"]
    assert decoder.errors == {"message": 11}
    assert decoder.counts["frames"] == 12  # the serial layer was fine
    assert decoder.counts["lost"] == 0


def test_serial_frame_vectors(nrf24_vectors: dict[str, Any]) -> None:
    for case in nrf24_vectors["serial_frames"]:
        wire = bytes.fromhex(case["wire"])
        body = bytes.fromhex(case["body"])
        assert wire.endswith(b"\x00") and b"\x00" not in wire[:-1]
        packet = decode_packet(cobs_decode(wire[:-1]))
        assert (packet.kind, packet.seq, packet.body) == (case["kind"], case["seq"], body)
        assert encode_wire(case["kind"], case["seq"], body) == wire


def test_serial_frame_vectors_through_the_decoder(nrf24_vectors: dict[str, Any]) -> None:
    stream = b"".join(bytes.fromhex(c["wire"]) for c in nrf24_vectors["serial_frames"])
    decoder = Nrf24GatewayDecoder()
    events = decoder.feed(stream, 3.0)
    assert texts(events) == [
        "COMMAND set_telemetry_period_ms arg=100",  # a command reported as a received message
        "COMMAND_RESULT link_seq=7 delivered",
        "[gateway] gateway ready",
        "REQUEST_STATS",
    ]
    # The last vector is a 200-byte send_command body that only exercises the serial
    # layer; as an application message it is invalid and is reported, not dropped.
    (err,) = errors(events)
    assert err.kind == "message"
    assert "send_command" in err.detail
    # seq 0 -> 255 -> 3 on the gateway-to-host direction; host-to-gateway kinds have
    # their own counter and are not counted.
    assert decoder.counts["lost"] == 254 + 3


def test_gateway_stats_vector(nrf24_vectors: dict[str, Any]) -> None:
    case = nrf24_vectors["gateway_stats"]
    body = bytes.fromhex(case["hex"])
    assert len(body) == 56
    events = Nrf24GatewayDecoder().feed(encode_wire(0x02, 0, body), 9.0)
    (sample,) = samples(events)
    assert sample.source == "link_stats"
    assert sample.device_t == pytest.approx(123.456)
    expected = {f"link.{k}": float(v) for k, v in case["fields"].items() if k != "uptime_ms"}
    assert sample.values == expected
    assert list(case["fields"]) == list(STATS_FIELDS)
    assert notices(events) == []  # nothing to compare with yet


# -- channels -------------------------------------------------------------------------------


def _message_wire(nrf24_vectors: dict[str, Any], name: str, seq: int = 0) -> bytes:
    case = next(c for c in nrf24_vectors["messages"] if c["name"] == name)
    return encode_wire(0x01, seq, b"\x01\x01\x00" + bytes.fromhex(case["hex"]))


def test_battery_channels(nrf24_vectors: dict[str, Any]) -> None:
    events = Nrf24GatewayDecoder().feed(
        _message_wire(nrf24_vectors, "battery from ina219 example"), 1.0
    )
    (sample,) = samples(events)
    assert sample.values == pytest.approx(
        {
            "battery.bus_v": 12.034,
            "battery.current_ma": 1523.4,
            "battery.power_mw": 18333.0,
            "battery.charge_mah": -2.5,
            "battery.soc_pct": 87.3,
            "battery.flags": 0.0,
        }
    )
    assert sample.device_t is None


def test_battery_without_current_sensor_and_unknown_soc(nrf24_vectors: dict[str, Any]) -> None:
    events = Nrf24GatewayDecoder().feed(_message_wire(nrf24_vectors, "battery extremes"), 1.0)
    (sample,) = samples(events)
    # flags 0x81: bit 7 = no current sensor, so current/power/charge are not channels;
    # soc 65535 = unknown.
    assert sample.values == {"battery.bus_v": 65.535, "battery.flags": 129.0}


def test_odometry_and_imu_channels(nrf24_vectors: dict[str, Any]) -> None:
    stream = _message_wire(nrf24_vectors, "odometry", 0) + _message_wire(nrf24_vectors, "imu", 1)
    odo, imu = samples(Nrf24GatewayDecoder().feed(stream, 1.0))
    assert odo.values == pytest.approx(
        {"odom.x": -1.5, "odom.y": 2.5, "odom.theta": -3.141, "odom.v": 0.2, "odom.w": -0.5}
    )
    assert odo.device_t == pytest.approx(4294967.295)
    assert imu.values == pytest.approx(
        {
            "imu.ax": -1.0,
            "imu.ay": 0.016,
            "imu.az": 1.003,
            "imu.gx": 28.65,
            "imu.gy": -327.68,
            "imu.gz": 327.67,
            "imu.temp_c": 25.12,
        }
    )
    # 123456 ms after a node clock at 2^32 - 1 ms: the 32-bit counter wrapped.
    assert imu.device_t == pytest.approx((2**32 + 123456) / 1000)


def test_key_value_text_and_numeric_channels(nrf24_vectors: dict[str, Any]) -> None:
    events = Nrf24GatewayDecoder().feed(_message_wire(nrf24_vectors, "key/value mixed"), 1.0)
    assert texts(events) == ["KV rssi=-64 vbat=3.5 ok=true fw=0.1.0 bias=-0.25 empty="]
    (sample,) = samples(events)
    assert sample.values == {"kv.rssi": -64.0, "kv.vbat": 3.5, "kv.ok": 1.0, "kv.bias": -0.25}
    empty = Nrf24GatewayDecoder().feed(_message_wire(nrf24_vectors, "key/value empty"), 1.0)
    assert texts(empty) == ["KV"] and samples(empty) == []


# -- streams, chunking and corruption -------------------------------------------------------


def _stats_body(**changes: int) -> bytes:
    import struct

    fields = dict.fromkeys(STATS_FIELDS, 0)
    fields.update(changes)
    return struct.pack(STATS_FORMAT, *fields.values())


def _vector_stream(nrf24_vectors: dict[str, Any]) -> bytes:
    """Every valid vector message wrapped in gateway packets, plus stats and a log line."""
    out = bytearray()
    seq = 0
    for case in nrf24_vectors["messages"]:
        out += encode_wire(0x01, seq, b"\x01\x01\x00" + bytes.fromhex(case["hex"]))
        seq += 1
    out += encode_wire(0x04, seq, b"gateway ready\r\n")
    out += encode_wire(0x02, seq + 1, bytes.fromhex(nrf24_vectors["gateway_stats"]["hex"]))
    return bytes(out)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64, 4096])
def test_split_reads_give_the_same_events(nrf24_vectors: dict[str, Any], size: int) -> None:
    stream = _vector_stream(nrf24_vectors)
    whole = Nrf24GatewayDecoder().feed(stream, 1.0)
    decoder = Nrf24GatewayDecoder()
    events = feed_split(decoder, stream, size, t0=1.0)
    assert errors(events) == []
    assert decoder.counts["frames"] == len(nrf24_vectors["messages"]) + 2
    assert decoder.counts["lost"] == 0
    # Same events apart from the host time, which is the time of each frame's first byte.
    strip = [(type(e), getattr(e, "values", None), getattr(e, "text", None)) for e in events]
    assert strip == [(type(e), getattr(e, "values", None), getattr(e, "text", None)) for e in whole]
    assert [s.source for s in samples(events)] == [
        "battery",
        "battery",
        "odometry",
        "imu",
        "key_value",
        "link_stats",
    ]


def test_frame_time_is_the_time_of_its_first_byte(nrf24_vectors: dict[str, Any]) -> None:
    wire = _message_wire(nrf24_vectors, "odometry")
    decoder = Nrf24GatewayDecoder()
    assert decoder.feed(wire[:10], 5.0) == []
    (sample,) = samples(decoder.feed(wire[10:], 6.0))
    assert sample.t == 5.0


def test_corrupted_frames_are_counted_and_good_frames_survive(
    nrf24_vectors: dict[str, Any],
) -> None:
    good = _message_wire(nrf24_vectors, "odometry", 0)
    flipped = bytearray(_message_wire(nrf24_vectors, "odometry", 1))
    flipped[12] ^= 0x04  # one bit error on the USB link: CRC mismatch
    truncated = _message_wire(nrf24_vectors, "imu", 2)[:15] + b"\x00"  # bytes lost mid-frame
    bad_cobs = b"\x05\x01\x00"  # block runs past the end
    packet = bytearray(encode_packet(0x01, 3, b"\x01\x01\x00"))
    packet[0] = 2
    crc = crc16_ccitt_false(bytes(packet[:-2]))
    packet[-2:] = bytes((crc & 0xFF, crc >> 8))
    wrong_version = cobs_encode(bytes(packet)) + b"\x00"
    unknown_kind = encode_wire(0x09, 4, b"")
    short_stats = encode_wire(0x02, 5, b"\x00" * 10)
    short_result = encode_wire(0x03, 6, b"\x01")
    short_message = encode_wire(0x01, 7, b"\x01")
    tiny = cobs_encode(b"\x01\x02\x03") + b"\x00"
    overlong = b"\x55" * (MAX_FRAME_BYTES + 40) + b"\x00"
    last = _message_wire(nrf24_vectors, "odometry", 8)
    stream = (
        good
        + bytes(flipped)
        + truncated
        + bad_cobs
        + wrong_version
        + unknown_kind
        + short_stats
        + short_result
        + short_message
        + tiny
        + overlong
        + last
    )
    for size in (1, 4, 4096):
        decoder = Nrf24GatewayDecoder()
        events = feed_split(decoder, stream, size)
        assert decoder.errors == {
            "crc": 1,  # the flipped bit
            "cobs": 2,  # bad block; the truncated frame's last block runs past its end
            "version": 1,
            "kind": 1,
            "length": 3,  # stats, command_result and message bodies too short
            "short": 1,
            "overlong": 1,
        }, size
        assert len(errors(events)) == 10
        assert [s.values["odom.x"] for s in samples(events)] == [-1.5, -1.5]
        # Sequenced packets: 0, then 5, 6, 7 (valid packets with bad bodies) and 8.
        # The unknown kind is rejected before sequencing; 1..4 count as lost.
        assert decoder.counts["lost"] == 4


def test_largest_packet_fits_and_one_more_byte_is_overlong() -> None:
    body = bytes(range(1, 256))
    assert len(body) == MAX_BODY
    wire = encode_wire(0x04, 0, body)
    assert len(wire) - 1 == MAX_FRAME_BYTES
    decoder = Nrf24GatewayDecoder()
    (line,) = texts(decoder.feed(wire, 1.0))
    assert line.startswith("[gateway] ")
    assert decoder.errors == {}
    with pytest.raises(ValueError, match="longer than 255"):
        encode_packet(0x04, 0, body + b"!")
    events = decoder.feed(b"\x01" * (MAX_FRAME_BYTES + 1) + b"\x00", 2.0)
    assert [e.kind for e in errors(events)] == ["overlong"]


def test_packet_errors_directly() -> None:
    with pytest.raises(GatewayFrameError) as info:
        decode_packet(b"\x01\x02")
    assert info.value.kind == "short"
    packet = bytearray(encode_packet(0x04, 0, b"abc"))
    packet[3] = 4  # length field says 4, body is 3; fix the CRC so only length is wrong
    crc = crc16_ccitt_false(bytes(packet[:-2]))
    packet[-2:] = bytes((crc & 0xFF, crc >> 8))
    with pytest.raises(GatewayFrameError) as info:
        decode_packet(bytes(packet))
    assert info.value.kind == "length"


def test_garbage_and_boot_text_before_the_first_delimiter(nrf24_vectors: dict[str, Any]) -> None:
    rng = random.Random(11)
    noise = bytes(rng.randrange(1, 256) for _ in range(500))  # no zero byte: one overlong
    stream = noise + b"\x00" + _vector_stream(nrf24_vectors)
    decoder = Nrf24GatewayDecoder()
    events = feed_split(decoder, stream, 7)
    assert decoder.errors == {"overlong": 1}
    assert len(samples(events)) == 6


def test_command_result_not_delivered_is_a_notice() -> None:
    decoder = Nrf24GatewayDecoder()
    events = decoder.feed(encode_wire(0x03, 0, b"\x2a\x00"), 1.0)
    assert texts(events) == ["COMMAND_RESULT link_seq=42 NOT delivered"]
    (notice,) = notices(events)
    assert notice.kind == "link"
    assert "42 not delivered" in notice.detail
    assert decoder.counts["commands_not_delivered"] == 1


def test_send_command_and_request_stats_are_shown() -> None:
    decoder = Nrf24GatewayDecoder()
    command = encode_message("command", {"opcode": 2, "arg": 3})
    events = decoder.feed(encode_wire(0x81, 0, command) + encode_wire(0x82, 1, b""), 1.0)
    assert texts(events) == ["SEND_COMMAND set_pa_level arg=3", "REQUEST_STATS"]
    events = decoder.feed(
        encode_wire(0x81, 2, encode_message("command", {"opcode": 77, "arg": 0})), 1.0
    )
    assert texts(events) == ["SEND_COMMAND op77 arg=0"]


# -- link quality ---------------------------------------------------------------------------


def test_link_counter_increases_become_notices_and_link_counts() -> None:
    decoder = Nrf24GatewayDecoder()
    first = _stats_body(uptime_ms=1000, frames_received=50, crc_errors=1, reliable_retries=2)
    second = _stats_body(
        uptime_ms=2000,
        frames_received=100,
        crc_errors=3,
        unreliable_lost=1,
        reliable_retries=2,
        duplicates=4,
    )
    third = _stats_body(uptime_ms=3000, frames_received=150, crc_errors=3, unreliable_lost=1)
    events = decoder.feed(encode_wire(0x02, 0, first), 1.0)
    assert notices(events) == []
    events = decoder.feed(encode_wire(0x02, 1, second), 2.0)
    (notice,) = notices(events)
    assert notice == Notice(
        2.0, "link", "radio: crc_errors +2 (total 3), unreliable_lost +1 (total 1)"
    )
    assert decoder.link_counts == {
        "frames_received": 100,
        "crc_errors": 3,
        "malformed": 0,
        "duplicates": 4,
        "unreliable_lost": 1,
        "reliable_retries": 2,
        "reliable_failed": 0,
        "resyncs": 0,
    }
    # Retries going down while uptime goes up: counters were reset, i.e. a restart.
    events = decoder.feed(encode_wire(0x02, 2, third), 3.0)
    assert [n.detail for n in notices(events)] == [
        "gateway restarted: its radio counters start again from zero"
    ]
    assert decoder.counts["gateway_restarts"] == 1
    (sample,) = samples(events)
    assert sample.values["link.crc_errors"] == 3.0


def test_session_surfaces_link_counts_and_notices() -> None:
    session = Session("nrf24")
    from serial_scope.events import Chunk

    session.process(Chunk(encode_wire(0x02, 0, _stats_body(uptime_ms=1, resyncs=1)), 1.0))
    session.process(Chunk(encode_wire(0x02, 1, _stats_body(uptime_ms=2, resyncs=2)), 2.0))
    assert session.link_counts()["resyncs"] == 2
    assert session.totals.notices == {"link": 1}
    assert session.totals.errors == {}
    assert "link.resyncs" in session.channels.names()


def test_auto_decoder_exposes_the_chosen_link_counts(nrf24_vectors: dict[str, Any]) -> None:
    session = Session("auto")
    from serial_scope.events import Chunk

    stream = _vector_stream(nrf24_vectors) * 2
    session.process(Chunk(stream, 1.0))
    session.finish()
    assert session.decoder_name == "auto (nrf24)"
    assert session.link_counts()["frames_received"] == 1000
    active = session.active_decoder
    assert active is not None and active.name == "nrf24" and active.binary


def test_samples_are_plain_floats(nrf24_vectors: dict[str, Any]) -> None:
    for event in Nrf24GatewayDecoder().feed(_vector_stream(nrf24_vectors), 1.0):
        if isinstance(event, Sample):
            assert all(type(v) is float for v in event.values.values())


def test_notices_reach_triggers_cli_output_and_summary() -> None:
    from serial_scope.cli import _Clock, format_event, summary_text
    from serial_scope.events import Chunk
    from serial_scope.triggers import TriggerSet, parse_trigger

    session = Session("nrf24", triggers=TriggerSet([parse_trigger("re:unreliable_lost")]))
    out = session.process(Chunk(encode_wire(0x02, 0, _stats_body(uptime_ms=1)), 1.0))
    out += session.process(
        Chunk(encode_wire(0x02, 1, _stats_body(uptime_ms=2, unreliable_lost=3)), 2.0)
    )
    lines = [format_event(e, _Clock(False)) for e in out]
    assert "~ link: radio: unreliable_lost +3 (total 3)" in lines[2]
    assert "* trigger re:unreliable_lost" in lines[3]
    assert len(session.markers) == 1
    summary = summary_text(session)
    assert "radio link (device totals): frames_received 0, crc_errors 0" in summary
    assert "unreliable_lost 3" in summary
    assert "notices: link 1" in summary


def test_millis_wrap_is_not_a_gateway_restart() -> None:
    # uptime_ms is the gateway's millis(); after 49.7 days it wraps to zero. Before 0.2.1
    # this counted as a restart and the counters looked reset.
    decoder = Nrf24GatewayDecoder()
    events = []
    for k, uptime in enumerate([2**32 - 1500, 2**32 - 500, 500, 1500]):
        body = _stats_body(uptime_ms=uptime, frames_received=1000 + 50 * k)
        events += decoder.feed(encode_wire(0x02, k, body), float(k))
    assert decoder.counts["gateway_restarts"] == 0
    assert not [n for n in notices(events) if "restarted" in n.detail]
    times = [s.device_t for s in samples(events)]
    assert times == pytest.approx([4294965.796, 4294966.796, 4294967.796, 4294968.796])
    # A small step back in uptime is still a restart.
    decoder.feed(encode_wire(0x02, 4, _stats_body(uptime_ms=100, frames_received=1300)), 5.0)
    assert decoder.counts["gateway_restarts"] == 1
