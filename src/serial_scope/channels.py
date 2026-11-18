"""Numeric channels discovered from the decoded stream."""

from __future__ import annotations

import math
from array import array
from bisect import bisect_left
from dataclasses import dataclass

from serial_scope.events import Sample

DEFAULT_BUFFER = 5000
RATE_WINDOW_S = 2.0


class RingBuffer:
    """Fixed-capacity buffer of ``(time, value)`` pairs; the oldest pair is overwritten."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._t = array("d", bytes(8 * capacity))
        self._v = array("d", bytes(8 * capacity))
        self._head = 0  # next write position
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def append(self, t: float, value: float) -> None:
        self._t[self._head] = t
        self._v[self._head] = value
        self._head = (self._head + 1) % self.capacity
        if self._size < self.capacity:
            self._size += 1

    def _ordered(self, data: array[float]) -> list[float]:
        if self._size < self.capacity:
            return data[: self._size].tolist()
        return data[self._head :].tolist() + data[: self._head].tolist()

    def times(self) -> list[float]:
        """Timestamps, oldest first."""
        return self._ordered(self._t)

    def values(self) -> list[float]:
        """Values, oldest first."""
        return self._ordered(self._v)

    def last(self) -> tuple[float, float] | None:
        if self._size == 0:
            return None
        i = (self._head - 1) % self.capacity
        return self._t[i], self._v[i]

    def since(self, t_from: float) -> tuple[list[float], list[float]]:
        """Pairs with time >= ``t_from`` (times are assumed non-decreasing)."""
        times = self.times()
        start = bisect_left(times, t_from)
        return times[start:], self.values()[start:]

    def clear(self) -> None:
        self._head = 0
        self._size = 0

    def resize(self, capacity: int) -> None:
        """Change the capacity, keeping the newest pairs that fit."""
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        times, values = self.times()[-capacity:], self.values()[-capacity:]
        self.capacity = capacity
        self._t = array("d", bytes(8 * capacity))
        self._v = array("d", bytes(8 * capacity))
        self.clear()
        for t, v in zip(times, values, strict=True):
            self.append(t, v)


class RunningStats:
    """Count, min, max and mean over every finite value seen (Welford's update)."""

    def __init__(self) -> None:
        self.count = 0
        self.min = math.inf
        self.max = -math.inf
        self.mean = 0.0

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        self.min = min(self.min, value)
        self.max = max(self.max, value)
        self.mean += (value - self.mean) / self.count


@dataclass
class ChannelSummary:
    name: str
    count: int
    last: float | None
    min: float | None
    max: float | None
    mean: float | None
    rate_hz: float


class Channel:
    """One named numeric series."""

    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.buffer = RingBuffer(capacity)
        self.stats = RunningStats()
        self.first_t: float | None = None
        self.last_t: float | None = None

    def add(self, t: float, value: float) -> None:
        self.buffer.append(t, value)
        self.stats.add(value)
        if self.first_t is None:
            self.first_t = t
        self.last_t = t

    def overall_rate(self) -> float:
        """Average samples per second since the channel appeared."""
        if self.first_t is None or self.last_t is None or self.last_t <= self.first_t:
            return 0.0
        return (self.stats.count - 1) / (self.last_t - self.first_t)

    def rate(self, window: float = RATE_WINDOW_S) -> float:
        """Samples per second over the last ``window`` seconds of buffered data."""
        last = self.buffer.last()
        if last is None:
            return 0.0
        times, _ = self.buffer.since(last[0] - window)
        if len(times) < 2 or times[-1] <= times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def summary(self) -> ChannelSummary:
        s = self.stats
        last = self.buffer.last()
        have = s.count > 0
        return ChannelSummary(
            name=self.name,
            count=s.count,
            last=last[1] if last else None,
            min=s.min if have else None,
            max=s.max if have else None,
            mean=s.mean if have else None,
            rate_hz=self.rate(),
        )


class ChannelSet:
    """All channels of a session, in order of first appearance.

    ``time_base`` selects the x axis: ``"host"`` (receive time, always available) or
    ``"device"`` (the device's own clock when the format carries one, else host time).
    """

    def __init__(self, capacity: int = DEFAULT_BUFFER, time_base: str = "host") -> None:
        if time_base not in ("host", "device"):
            raise ValueError("time_base must be 'host' or 'device'")
        self.capacity = capacity
        self.time_base = time_base
        self.channels: dict[str, Channel] = {}
        self.samples = 0

    def __len__(self) -> int:
        return len(self.channels)

    def __contains__(self, name: object) -> bool:
        return name in self.channels

    def __getitem__(self, name: str) -> Channel:
        return self.channels[name]

    def names(self) -> list[str]:
        return list(self.channels)

    def add_sample(self, sample: Sample) -> list[str]:
        """Store a sample; return the names of channels it created."""
        t = sample.t
        if self.time_base == "device" and sample.device_t is not None:
            t = sample.device_t
        new: list[str] = []
        for name, value in sample.values.items():
            channel = self.channels.get(name)
            if channel is None:
                channel = self.channels[name] = Channel(name, self.capacity)
                new.append(name)
            channel.add(t, value)
        self.samples += 1
        return new

    def set_capacity(self, capacity: int) -> None:
        self.capacity = capacity
        for channel in self.channels.values():
            channel.buffer.resize(capacity)

    def latest_time(self) -> float | None:
        lasts = [c.buffer.last() for c in self.channels.values()]
        times = [x[0] for x in lasts if x is not None]
        return max(times) if times else None

    def clear(self) -> None:
        self.channels.clear()
        self.samples = 0
