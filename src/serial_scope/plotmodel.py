"""Plot geometry, scaling, decimation, cursor readout and themes, independent of Tk.

The Tk view (``gui.py``) only turns the coordinate lists produced here into canvas
items, so everything that can be wrong about a plot is tested without a display.
"""

from __future__ import annotations

import math
import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from serial_scope.channels import ChannelSet


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    fg: str
    muted: str
    plot_bg: str
    grid: str
    axis: str
    text_bg: str
    text_fg: str
    select_bg: str
    error: str
    marker: str
    palette: tuple[str, ...]


THEMES: dict[str, Theme] = {
    "light": Theme(
        name="light",
        bg="#f3f4f6",
        fg="#1f2328",
        muted="#59636e",
        plot_bg="#ffffff",
        grid="#e3e6ea",
        axis="#8c959f",
        text_bg="#ffffff",
        text_fg="#1f2328",
        select_bg="#cfe3ff",
        error="#c62828",
        marker="#8250df",
        palette=(
            "#1f77b4",
            "#d62728",
            "#2ca02c",
            "#ff7f0e",
            "#9467bd",
            "#17becf",
            "#8c564b",
            "#e377c2",
        ),
    ),
    "dark": Theme(
        name="dark",
        bg="#1e2227",
        fg="#d7dae0",
        muted="#8b949e",
        plot_bg="#16191d",
        grid="#2b3036",
        axis="#5c6370",
        text_bg="#16191d",
        text_fg="#d7dae0",
        select_bg="#264f78",
        error="#ff6b6b",
        marker="#c792ea",
        palette=(
            "#61afef",
            "#e06c75",
            "#98c379",
            "#e5c07b",
            "#c678dd",
            "#56b6c2",
            "#d19a66",
            "#ff9ac1",
        ),
    ),
}


def channel_color(theme: Theme, index: int) -> str:
    return theme.palette[index % len(theme.palette)]


@dataclass(frozen=True)
class PlotArea:
    """Canvas size and the margins reserved for axis labels."""

    width: int
    height: int
    left: int = 64
    right: int = 12
    top: int = 12
    bottom: int = 26

    @property
    def x0(self) -> int:
        return self.left

    @property
    def x1(self) -> int:
        return max(self.left + 1, self.width - self.right)

    @property
    def y0(self) -> int:
        return self.top

    @property
    def y1(self) -> int:
        return max(self.top + 1, self.height - self.bottom)

    @property
    def plot_width(self) -> int:
        return self.x1 - self.x0


@dataclass(frozen=True)
class Viewport:
    """Data range shown: time ``t0..t1`` and value ``v0..v1``."""

    t0: float
    t1: float
    v0: float
    v1: float

    def x(self, t: float, area: PlotArea) -> float:
        return area.x0 + (t - self.t0) / (self.t1 - self.t0) * (area.x1 - area.x0)

    def y(self, v: float, area: PlotArea) -> float:
        return area.y1 - (v - self.v0) / (self.v1 - self.v0) * (area.y1 - area.y0)

    def t_at(self, x: float, area: PlotArea) -> float:
        return self.t0 + (x - area.x0) / (area.x1 - area.x0) * (self.t1 - self.t0)


def padded_range(lo: float, hi: float, margin: float = 0.05) -> tuple[float, float]:
    """Expand ``lo..hi`` by ``margin`` of its span; a flat range gets a +-1 (or +-10%) pad."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return -1.0, 1.0
    if hi < lo:
        lo, hi = hi, lo
    span = hi - lo
    if span == 0:
        pad = abs(lo) * 0.1 or 1.0
        return lo - pad, hi + pad
    return lo - span * margin, hi + span * margin


def nice_step(span: float, max_ticks: int = 6) -> float:
    """A 1-2-5 step that divides ``span`` into at most ``max_ticks`` intervals."""
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(1, max_ticks)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        step = factor * magnitude
        if step >= raw:
            return float(step)
    return float(10 * magnitude)  # pragma: no cover - loop always returns


def nice_ticks(lo: float, hi: float, max_ticks: int = 6) -> list[float]:
    """Tick positions on multiples of a 1-2-5 step inside ``lo..hi``."""
    step = nice_step(hi - lo, max_ticks)
    first = math.ceil(lo / step - 1e-9) * step
    ticks: list[float] = []
    v = first
    while v <= hi + step * 1e-9:
        ticks.append(0.0 if abs(v) < step * 1e-9 else round(v, 12))
        v += step
    return ticks


def format_tick(value: float, step: float) -> str:
    """Enough decimals to tell neighbouring ticks apart, no more."""
    if step >= 1 or step <= 0:
        decimals = 0
    else:
        decimals = min(9, max(0, -math.floor(math.log10(step) + 1e-9)))
    if abs(value) >= 1e6 or (value != 0 and abs(value) < 1e-4):
        return f"{value:.3g}"
    return f"{value:.{decimals}f}"


def format_value(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    if value == 0:
        return "0"
    if abs(value) >= 1e6 or abs(value) < 1e-3:
        return f"{value:.4g}"
    return f"{value:.6g}"


def decimate(
    times: list[float], values: list[float], t0: float, t1: float, columns: int
) -> tuple[list[float], list[float]]:
    """Reduce points in ``t0..t1`` to at most ~4 per pixel column, keeping min and max.

    Points just outside the window are kept so lines run to the edges. When there are
    fewer points than ``2 * columns`` the input slice is returned unchanged.
    """
    start = max(0, bisect_left(times, t0) - 1)
    end = min(len(times), bisect_right(times, t1) + 1)
    ts = times[start:end]
    vs = values[start:end]
    if len(ts) <= 2 * columns or columns <= 0 or t1 <= t0:
        return ts, vs
    out_t: list[float] = []
    out_v: list[float] = []
    width = (t1 - t0) / columns
    i = 0
    n = len(ts)
    while i < n:
        col = math.floor((ts[i] - t0) / width)
        j = i
        lo_i = hi_i = i
        while j < n and math.floor((ts[j] - t0) / width) == col:
            if vs[j] < vs[lo_i]:
                lo_i = j
            if vs[j] > vs[hi_i]:
                hi_i = j
            j += 1
        keep = sorted({i, lo_i, hi_i, j - 1})
        for k in keep:
            out_t.append(ts[k])
            out_v.append(vs[k])
        i = j
    return out_t, out_v


def compute_viewport(
    channels: ChannelSet,
    visible: Iterable[str],
    window_s: float,
    t_end: float | None = None,
    fixed: tuple[float, float] | None = None,
) -> Viewport | None:
    """Viewport showing the last ``window_s`` seconds up to ``t_end``.

    The value range autoscales to the visible channels' data in the window unless
    ``fixed`` is given. Returns ``None`` when there is nothing to show.
    """
    names = [n for n in visible if n in channels]
    if t_end is None:
        lasts = [channels[n].buffer.last() for n in names]
        ends = [x[0] for x in lasts if x is not None]
        if not ends:
            return None
        t_end = max(ends)
    t0 = t_end - window_s
    if fixed is not None:
        return Viewport(t0, t_end, *fixed)
    lo, hi = math.inf, -math.inf
    for name in names:
        _, vs = channels[name].buffer.since(t0)
        finite = [v for v in vs if math.isfinite(v)]
        if finite:
            lo = min(lo, min(finite))
            hi = max(hi, max(finite))
    if lo > hi:
        return Viewport(t0, t_end, -1.0, 1.0)
    return Viewport(t0, t_end, *padded_range(lo, hi))


def polyline(times: list[float], values: list[float], vp: Viewport, area: PlotArea) -> list[float]:
    """Flattened ``x, y`` canvas coordinates for one channel, decimated to the width."""
    ts, vs = decimate(times, values, vp.t0, vp.t1, area.plot_width)
    coords: list[float] = []
    for t, v in zip(ts, vs, strict=True):
        if math.isfinite(v):
            coords.append(round(vp.x(t, area), 1))
            coords.append(round(vp.y(v, area), 1))
    return coords


@dataclass(frozen=True)
class Readout:
    name: str
    t: float
    value: float


def cursor_readout(channels: ChannelSet, names: Iterable[str], t: float) -> list[Readout]:
    """The sample of each channel nearest to time ``t``."""
    out: list[Readout] = []
    for name in names:
        if name not in channels:
            continue
        buf = channels[name].buffer
        times = buf.times()
        if not times:
            continue
        i = bisect_left(times, t)
        candidates = [k for k in (i - 1, i) if 0 <= k < len(times)]
        k = min(candidates, key=lambda k: abs(times[k] - t))
        out.append(Readout(name, times[k], buf.values()[k]))
    return out


class Throttle:
    """Lets an action through at most once per ``interval`` seconds."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = interval
        self._clock = clock
        self._last: float | None = None

    def ready(self) -> bool:
        now = self._clock()
        if self._last is None or now - self._last >= self.interval:
            self._last = now
            return True
        return False
