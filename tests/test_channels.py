from __future__ import annotations

import math
import statistics

import pytest

from serial_scope.channels import ChannelSet, RingBuffer, RunningStats
from serial_scope.events import Sample


def test_ring_buffer_fills_then_wraps() -> None:
    buf = RingBuffer(3)
    assert len(buf) == 0 and buf.last() is None
    for i in range(5):
        buf.append(float(i), i * 10.0)
    assert len(buf) == 3
    assert buf.times() == [2.0, 3.0, 4.0]
    assert buf.values() == [20.0, 30.0, 40.0]
    assert buf.last() == (4.0, 40.0)


def test_ring_buffer_since_and_clear() -> None:
    buf = RingBuffer(10)
    for i in range(10):
        buf.append(i * 0.5, float(i))
    assert buf.since(3.0) == ([3.0, 3.5, 4.0, 4.5], [6.0, 7.0, 8.0, 9.0])
    buf.clear()
    assert buf.times() == []


def test_ring_buffer_resize_keeps_newest() -> None:
    buf = RingBuffer(4)
    for i in range(6):
        buf.append(float(i), float(i))
    buf.resize(2)
    assert buf.times() == [4.0, 5.0]
    buf.resize(5)
    buf.append(6.0, 6.0)
    assert buf.values() == [4.0, 5.0, 6.0]
    with pytest.raises(ValueError):
        buf.resize(0)
    with pytest.raises(ValueError):
        RingBuffer(0)


def test_running_stats_match_statistics_module() -> None:
    values = [3.0, -1.5, 8.25, 0.0, 2.0, 2.0, 1e-3]
    stats = RunningStats()
    for v in values:
        stats.add(v)
    stats.add(math.nan)  # ignored
    assert stats.count == len(values)
    assert stats.min == -1.5
    assert stats.max == 8.25
    assert stats.mean == pytest.approx(statistics.fmean(values))


def test_channel_set_discovers_channels_in_order() -> None:
    cs = ChannelSet(capacity=100)
    assert cs.add_sample(Sample(1.0, {"b": 1.0, "a": 2.0})) == ["b", "a"]
    assert cs.add_sample(Sample(2.0, {"a": 3.0, "c": 4.0})) == ["c"]
    assert cs.names() == ["b", "a", "c"]
    assert "a" in cs and len(cs) == 3
    assert cs["a"].buffer.values() == [2.0, 3.0]
    assert cs.latest_time() == 2.0
    assert cs.samples == 2
    cs.clear()
    assert cs.latest_time() is None


def test_rate_over_recent_window() -> None:
    cs = ChannelSet(capacity=1000)
    # 50 Hz for 4 s, then 10 Hz for 3 s: the 2 s window sees only the 10 Hz part.
    t = 0.0
    for _ in range(200):
        cs.add_sample(Sample(t, {"v": 1.0}))
        t += 0.02
    for _ in range(30):
        cs.add_sample(Sample(t, {"v": 1.0}))
        t += 0.1
    ch = cs["v"]
    assert ch.rate() == pytest.approx(10.0)
    assert ch.overall_rate() == pytest.approx(229 / (200 * 0.02 + 29 * 0.1))
    summary = ch.summary()
    assert summary.count == 230 and summary.last == 1.0


def test_rate_with_too_few_points() -> None:
    cs = ChannelSet()
    cs.add_sample(Sample(1.0, {"v": 1.0}))
    assert cs["v"].rate() == 0.0
    assert cs["v"].overall_rate() == 0.0
    summary = cs["v"].summary()
    assert summary.min == 1.0 and summary.rate_hz == 0.0


def test_device_time_base() -> None:
    cs = ChannelSet(time_base="device")
    cs.add_sample(Sample(100.0, {"v": 1.0}, device_t=0.5))
    cs.add_sample(Sample(101.0, {"v": 2.0}))  # no device time: falls back to host
    assert cs["v"].buffer.times() == [0.5, 101.0]
    with pytest.raises(ValueError):
        ChannelSet(time_base="gps")


def test_set_capacity() -> None:
    cs = ChannelSet(capacity=10)
    for i in range(10):
        cs.add_sample(Sample(float(i), {"v": float(i)}))
    cs.set_capacity(3)
    assert cs["v"].buffer.values() == [7.0, 8.0, 9.0]
    assert cs["v"].stats.count == 10  # totals are kept
