"""Synthetic demo logs, so the tool can be tried (and tested) without a board.

Each generator produces what a board running a typical sketch would print, then
:func:`write_demo_log` cuts the byte stream into irregular chunks the way a USB-serial
bridge delivers it and stores it as an ``.sslog`` file. Nothing here is a recording from
real hardware; the data is computed from simple models and seeded noise.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from serial_scope.decoders import nrf24
from serial_scope.decoders.ina219 import encode_bm1
from serial_scope.decoders.odometry import encode_frame
from serial_scope.events import RX, Chunk
from serial_scope.logfile import LogWriter

DEMO_KINDS = ("plotter", "ina219", "odometry", "nrf24", "jsonl", "csv")
DEMO_START = 1_763_550_000.0  # fixed epoch time so demo output is reproducible

Record = tuple[float, bytes]  # (seconds since start, bytes the device wrote)

ESP32_BOOT = (
    b"ets Jun  8 2016 00:22:57\r\n\r\nrst:0x1 (POWERON_RESET),boot:0x13 "
    b"(SPI_FAST_FLASH_BOOT)\r\nconfigsip: 0, SPIWP:0xee\r\nmode:DIO, clock div:2\r\n"
    b"entry 0x400805e4\r\n"
)


def _motor(seconds: float, rate: float, rng: random.Random) -> Iterator[tuple[float, ...]]:
    """Speed loop step response: a damped second-order plant under a PI controller."""
    dt = 1.0 / rate
    speed = accel = integral = 0.0
    wn, zeta = 7.0, 0.35
    steps = [(0.0, 0.0), (1.0, 120.0), (6.0, 60.0), (11.0, 150.0), (16.0, 0.0)]
    t = 0.0
    while t < seconds:
        setpoint = next(sp for start, sp in reversed(steps) if t >= start)
        error = setpoint - speed
        integral += error * dt
        duty = max(-100.0, min(100.0, 0.45 * error + 0.6 * integral + 0.5 * setpoint))
        # second-order response of speed toward the setpoint
        accel += (wn * wn * (setpoint - speed) - 2 * zeta * wn * accel) * dt
        speed += accel * dt
        yield t, setpoint, speed + rng.gauss(0, 0.8), duty
        t += dt


def plotter_records(seconds: float = 20.0, seed: int = 1) -> list[Record]:
    """An ESP32 motor-speed sketch printing ``setpoint:..,speed:..,duty:..`` at 50 Hz."""
    rng = random.Random(seed)
    records: list[Record] = [(0.0, ESP32_BOOT), (0.25, b"[app] speed loop demo, 50 Hz\r\n")]
    next_status = 5.0
    for t, setpoint, speed, duty in _motor(seconds, 50.0, rng):
        line = f"setpoint:{setpoint:.0f},speed:{speed:.2f},duty:{duty:.1f}\r\n"
        records.append((0.4 + t, line.encode()))
        if t >= next_status:
            rssi = -60 - rng.randint(0, 8)
            records.append((0.4 + t + 0.001, f"[wifi] RSSI {rssi} dBm\r\n".encode()))
            next_status += 5.0
    return records


def ina219_records(seconds: float = 20.0, seed: int = 2) -> list[Record]:
    """ina219-battery-monitor at RATE 100, with one corrupted and one lost frame."""
    rng = random.Random(seed)
    records: list[Record] = [
        (0.0, b"#ina219-battery-monitor 0.1.0\r\n"),
        (0.05, b"#OK RATE 100\r\n"),
    ]
    charge_uah = 0.0
    energy_uwh = 0.0
    seq = 0
    t = 0.1
    while t < seconds:
        current_ua = int(150_000 + (180_000 if 6 <= t < 12 else 0) + rng.gauss(0, 1500))
        bus_mv = int(4012 - 9 * t - current_ua * 0.0004 + rng.gauss(0, 2)) // 4 * 4
        power_uw = bus_mv * current_ua // 1000
        charge_uah += current_ua * 0.1 / 3600
        energy_uwh += power_uw * 0.1 / 3600
        soc = max(0, 790 - int(t * 2))
        fields = [
            seq,
            int(t * 1000),
            bus_mv,
            current_ua,
            power_uw,
            int(charge_uah),
            int(energy_uwh),
            soc,
            0,
        ]
        line = encode_bm1(fields)
        if seq == 57:  # a corrupted digit on the wire: the checksum no longer matches
            line = line.replace(f",{bus_mv},", f",{bus_mv + 1},", 1)
        if seq != 120:  # frame lost entirely
            records.append((t, (line + "\r\n").encode()))
        seq += 1
        t += 0.1
    return records


def odometry_records(seconds: float = 20.0, seed: int = 3) -> list[Record]:
    """encoder-odometry-firmware telemetry: a robot driving a circle at 20 Hz."""
    rng = random.Random(seed)
    records: list[Record] = [(0.0, ESP32_BOOT)]
    seq = 0
    x = y = theta = 0.0
    counts = [0, 0, 0, 0]
    t = 0.2
    dt = 0.05
    while t < seconds:
        ramp = min(1.0, t / 2.0)
        vx, wz = 0.3 * ramp, 0.4 * ramp
        theta = math.remainder(theta + wz * dt, 2 * math.pi)
        x += vx * math.cos(theta) * dt
        y += vx * math.sin(theta) * dt
        left = (vx - wz * 0.17) / 0.05
        right = (vx + wz * 0.17) / 0.05
        targets = [left, right, left, right]
        vel = [v + rng.gauss(0, 0.15) for v in targets]
        for i in range(4):
            counts[i] += int(vel[i] * dt * 38000 / (2 * math.pi))
        t_us = int(t * 1e6)
        wheel = {
            "t_us": t_us,
            "count": list(counts),
            "velocity": vel,
            "target": targets,
            "duty": [int(max(-10000, min(10000, v * 300))) for v in vel],
        }
        frame = encode_frame("wheel_state", seq, wheel)
        if seq == 101:
            frame = frame[:20] + bytes([frame[20] ^ 0x10]) + frame[21:]  # corrupt one byte
        records.append((t, frame))
        seq += 1
        odom = {"t_us": t_us, "x": x, "y": y, "theta": theta, "vx": vx, "vy": 0.0, "wz": wz}
        records.append((t + 0.0004, encode_frame("odometry", seq, odom)))
        seq += 1
        if int(t / dt) % 10 == 0:
            diag = {
                "t_us": t_us,
                "illegal_transitions": [0, 0, 0, 0],
                "loop_overruns": 0,
                "watchdog_trips": 0,
                "max_loop_us": 410 + rng.randint(0, 60),
                "rx_crc_errors": 0,
                "rx_frame_errors": 1,
                "flags": 0,
            }
            records.append((t + 0.0008, encode_frame("diagnostics", seq, diag)))
            seq += 1
        t += dt
    return records


def nrf24_records(seconds: float = 20.0, seed: int = 6) -> list[Record]:
    """nrf24-telemetry-link gateway on USB: a robot node's telemetry relayed over radio.

    The node sends odometry and IMU messages at 10 Hz, battery at 1 Hz and a key/value
    status every 5 s on the best-effort channel; the gateway forwards each message it
    receives and its link statistics once a second. About 2 % of radio messages are
    lost (and counted by the gateway), a few radio frames fail their CRC, a reliable
    command is acknowledged after one retry at 8 s and fails after all its attempts at
    14 s. On the USB side one packet has a flipped bit and one is missing. All of it is
    computed, not recorded.
    """
    rng = random.Random(seed)
    records: list[Record] = []
    serial_seq = 0
    hops = (54, 36, 18, 2, 38, 74, 44, 42)  # the protocol's default hop sequence
    stats = dict.fromkeys(nrf24.STATS_FIELDS, 0)
    stats.update(frames_received=8200, delivered_unreliable=8105, hop_locked=1)

    def packet(t: float, kind: int, body: bytes, corrupt: bool = False, drop: bool = False) -> None:
        nonlocal serial_seq
        wire = nrf24.encode_wire(kind, serial_seq, body)
        serial_seq = (serial_seq + 1) & 0xFF
        if corrupt:
            wire = wire[:9] + bytes([wire[9] ^ 0x20]) + wire[10:]
        if not drop:
            records.append((t, wire))

    def relay(t: float, name: str, fields: dict[str, Any], **kw: bool) -> None:
        stats["frames_received"] += 1
        if rng.random() < 0.02:  # lost on the radio: the gateway sees a sequence gap
            stats["unreliable_lost"] += 1
            return
        if rng.random() < 0.01:  # a radio frame that failed its CRC, then the next one
            stats["crc_errors"] += 1
            stats["frames_received"] += 1
        stats["delivered_unreliable"] += 1
        flags = 1 if rng.random() < 0.9 else 0  # RPD: received power above -64 dBm
        body = bytes((1, 1, flags)) + nrf24.encode_message(name, fields)
        packet(t, 0x01, body, **kw)

    packet(0.0, 0x04, b"gateway ready")
    x = y = theta = 0.0
    charge_uah = 0.0
    t = 0.1
    step = 0
    while t < seconds:
        ramp = min(1.0, t / 2.0)
        v, w = 0.25 * ramp, 0.35 * ramp
        theta = math.remainder(theta + w * 0.1, 2 * math.pi)
        x += v * math.cos(theta) * 0.1
        y += v * math.sin(theta) * 0.1
        t_ms = 5000 + int(t * 1000)
        odo = {
            "t_ms": t_ms,
            "x_mm": round(x * 1000),
            "y_mm": round(y * 1000),
            "theta_mrad": round(theta * 1000),
            "v_mm_s": round(v * 1000 + rng.gauss(0, 4)),
            "w_mrad_s": round(w * 1000 + rng.gauss(0, 6)),
        }
        relay(t, "odometry", odo, corrupt=step == 63)
        imu = {
            "t_ms": t_ms + 2,
            "accel_mg": [
                round(rng.gauss(0, 12)),
                round(v * w / 9.81 * 1000 + rng.gauss(0, 12)),
                round(1000 + rng.gauss(0, 8)),
            ],
            "gyro_cdps": [
                round(rng.gauss(0, 20)),
                round(rng.gauss(0, 20)),
                round(math.degrees(w) * 100 + rng.gauss(0, 30)),
            ],
            "temp_cc": round(3150 + 8 * t + rng.gauss(0, 3)),
        }
        relay(t + 0.004, "imu", imu, drop=step == 121)
        if step % 10 == 5:
            current_ua = round(620_000 + 450_000 * ramp + rng.gauss(0, 8000))
            bus_mv = round(7820 - 6 * t - current_ua * 0.00012 + rng.gauss(0, 3))
            charge_uah += current_ua / 3600
            battery = {
                "bus_mv": bus_mv,
                "current_ua": current_ua,
                "power_uw": bus_mv * current_ua // 1000,
                "charge_uah": round(charge_uah),
                "soc_permille": 812 - round(t / 2),
                "flags": 0,
            }
            relay(t + 0.008, "battery", battery)
        if step % 50 == 7:
            entries = [
                {"key": "period_ms", "type": "int", "value": 100},
                {"key": "pa_dbm", "type": "int", "value": -6},
                {"key": "imu_ok", "type": "bool", "value": True},
                {"key": "fw", "type": "string", "value": "demo"},
            ]
            relay(t + 0.012, "key_value", {"entries": entries})
        if step == 80:  # a reliable command: delivered after one retry
            stats["reliable_sent"] += 1
            stats["reliable_retries"] += 1
            stats["reliable_acked"] += 1
            packet(t + 0.03, 0x03, bytes((12, 1)))
        if step == 140:  # a reliable command that used up its attempts
            stats["reliable_sent"] += 1
            stats["reliable_retries"] += 7
            stats["reliable_failed"] += 1
            stats["resyncs"] += 1
            packet(t + 0.03, 0x03, bytes((13, 0)))
        if step % 10 == 9:
            second = step // 10
            stats["uptime_ms"] = 30_000 + int(t * 1000)
            stats["hop_index"] = second % len(hops)
            stats["channel"] = hops[second % len(hops)]
            stats["rpd_percent"] = 88 + rng.randint(-4, 4)
            body = bytes(nrf24.encode_stats(stats))
            packet(t + 0.05, 0x02, body)
        t += 0.1
        step += 1
    return records


def jsonl_records(seconds: float = 20.0, seed: int = 4) -> list[Record]:
    """An environment sensor printing ArduinoJson objects at 10 Hz."""
    rng = random.Random(seed)
    records: list[Record] = [(0.0, b"[env] BME280 found at 0x76\r\n")]
    t = 0.1
    while t < seconds:
        obj = {
            "t_ms": int(t * 1000),
            "temp_c": round(23.0 + 0.8 * math.sin(t / 3) + rng.gauss(0, 0.05), 2),
            "rh": round(41.0 + 2 * math.cos(t / 5) + rng.gauss(0, 0.2), 1),
            "imu": {"ax": round(rng.gauss(0, 0.02), 3), "az": round(9.81 + rng.gauss(0, 0.02), 3)},
            "fan": t > 10,
        }
        records.append((t, (json.dumps(obj, separators=(",", ":")) + "\n").encode()))
        t += 0.1
    return records


def csv_records(seconds: float = 20.0, seed: int = 5) -> list[Record]:
    """An STM32 ADC logger printing a CSV header and 20 rows per second."""
    rng = random.Random(seed)
    records: list[Record] = [(0.0, b"millis,adc0,adc1,vref_mv\r\n")]
    t = 0.05
    while t < seconds:
        adc0 = int(2048 + 1500 * math.sin(2 * math.pi * 0.25 * t) + rng.gauss(0, 6))
        adc1 = int(1000 + 4 * t * 10 + rng.gauss(0, 4))
        line = f"{int(t * 1000)},{adc0},{adc1},{3300 + rng.randint(-3, 3)}\r\n"
        records.append((t, line.encode()))
        t += 0.05
    return records


GENERATORS = {
    "plotter": plotter_records,
    "ina219": ina219_records,
    "odometry": odometry_records,
    "nrf24": nrf24_records,
    "jsonl": jsonl_records,
    "csv": csv_records,
}


def chunk_records(records: list[Record], start: float = DEMO_START, seed: int = 0) -> list[Chunk]:
    """Split the byte stream into 1..64 byte reads with sub-millisecond spacing."""
    rng = random.Random(seed)
    chunks: list[Chunk] = []
    last_t = start
    for rel_t, data in records:
        t = max(last_t, start + rel_t)
        i = 0
        while i < len(data):
            size = rng.randint(1, 64)
            chunks.append(Chunk(data[i : i + size], round(t, 6), RX))
            i += size
            t += rng.uniform(0.00005, 0.0006)
        last_t = t
    return chunks


def write_demo_log(
    path: str | Path,
    kind: str = "plotter",
    seconds: float = 20.0,
    seed: int | None = None,
    start: float = DEMO_START,
) -> int:
    """Write a synthetic session to ``path``; returns the number of chunks."""
    if kind not in GENERATORS:
        raise ValueError(f"unknown demo kind {kind!r} (choose from {', '.join(DEMO_KINDS)})")
    generator = GENERATORS[kind]
    records = generator(seconds) if seed is None else generator(seconds, seed)
    chunks = chunk_records(records, start)
    meta = {"source": f"demo:{kind}", "synthetic": True, "baudrate": 115200}
    with LogWriter(path, meta, start_time=start) as writer:
        for chunk in chunks:
            writer.write(chunk)
    return len(chunks)
