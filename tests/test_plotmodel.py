from __future__ import annotations

import math

import pytest

from serial_scope.channels import ChannelSet
from serial_scope.events import Sample
from serial_scope.plotmodel import (
    THEMES,
    PlotArea,
    Throttle,
    Viewport,
    channel_color,
    compute_viewport,
    cursor_readout,
    decimate,
    format_tick,
    format_value,
    nice_step,
    nice_ticks,
    padded_range,
    polyline,
)


@pytest.mark.parametrize(
    ("span", "step"),
    [(10.0, 2.0), (1.0, 0.2), (7.3, 2.0), (100.0, 20.0), (0.003, 0.0005), (6.0, 1.0)],
)
def test_nice_step(span: float, step: float) -> None:
    assert nice_step(span, 6) == pytest.approx(step)


def test_nice_step_degenerate() -> None:
    assert nice_step(0.0) == 1.0
    assert nice_step(math.inf) == 1.0


def test_nice_ticks() -> None:
    assert nice_ticks(-0.3, 1.1, 6) == pytest.approx([0.0, 0.5, 1.0])
    assert nice_ticks(-0.3, 1.1, 8) == pytest.approx([-0.2, 0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    assert nice_ticks(-10.0, 0.0, 8) == pytest.approx([-10.0, -8.0, -6.0, -4.0, -2.0, 0.0])
    assert 0.0 in nice_ticks(-1e-12, 1.0)


def test_format_tick() -> None:
    assert format_tick(0.2, 0.2) == "0.2"
    assert format_tick(150.0, 50.0) == "150"
    assert format_tick(-0.25, 0.05) == "-0.25"
    assert format_tick(2e6, 1e6) == "2e+06"
    assert format_tick(5e-5, 1e-5) == "5e-05"


def test_format_value() -> None:
    assert format_value(None) == "-"
    assert format_value(math.nan) == "-"
    assert format_value(0.0) == "0"
    assert format_value(3.14159265) == "3.14159"
    assert format_value(1234567.0) == "1.235e+06"


def test_padded_range() -> None:
    assert padded_range(0.0, 10.0) == (-0.5, 10.5)
    assert padded_range(10.0, 0.0) == (-0.5, 10.5)
    assert padded_range(5.0, 5.0) == (4.5, 5.5)
    assert padded_range(0.0, 0.0) == (-1.0, 1.0)
    assert padded_range(math.nan, 1.0) == (-1.0, 1.0)


def test_viewport_mapping_round_trip() -> None:
    area = PlotArea(400, 300)
    vp = Viewport(10.0, 20.0, -1.0, 1.0)
    assert vp.x(10.0, area) == area.x0
    assert vp.x(20.0, area) == area.x1
    assert vp.y(-1.0, area) == area.y1
    assert vp.y(1.0, area) == area.y0
    assert vp.t_at(vp.x(13.7, area), area) == pytest.approx(13.7)
    assert area.plot_width == 400 - 64 - 12


def test_decimate_keeps_extremes_and_bounds() -> None:
    n = 10_000
    times = [i * 0.001 for i in range(n)]
    values = [math.sin(i / 50) for i in range(n)]
    values[5000] = 50.0  # one spike must survive
    values[7000] = -50.0
    ts, vs = decimate(times, values, 0.0, 10.0, 100)
    assert len(ts) <= 4 * 100 + 2
    assert 50.0 in vs and -50.0 in vs
    assert ts == sorted(ts)
    # Few points: returned unchanged (plus neighbours just outside the window).
    small_t, _ = decimate(times[:50], values[:50], 0.01, 0.02, 100)
    assert small_t == times[9:22]


def _channels() -> ChannelSet:
    cs = ChannelSet()
    for i in range(101):
        cs.add_sample(Sample(i * 0.1, {"a": float(i), "b": -float(i)}))
    return cs


def test_compute_viewport_autoscale_and_fixed() -> None:
    cs = _channels()
    vp = compute_viewport(cs, ["a"], window_s=2.0)
    assert vp is not None
    assert (vp.t0, vp.t1) == pytest.approx((8.0, 10.0))
    assert (vp.v0, vp.v1) == pytest.approx((79.0, 101.0))  # 80..100 padded by 5 %
    both = compute_viewport(cs, ["a", "b"], window_s=1.0, t_end=5.0)
    assert both is not None
    assert both.v0 < -50 and both.v1 > 50
    fixed = compute_viewport(cs, ["a"], 2.0, fixed=(0.0, 1.0))
    assert fixed is not None and (fixed.v0, fixed.v1) == (0.0, 1.0)
    assert compute_viewport(cs, [], 2.0) is None
    assert compute_viewport(cs, ["missing"], 2.0) is None
    empty_window = compute_viewport(cs, ["a"], 1.0, t_end=100.0)
    assert empty_window is not None and (empty_window.v0, empty_window.v1) == (-1.0, 1.0)


def test_polyline_coordinates() -> None:
    area = PlotArea(200 + 76, 100 + 38)  # 200 x 100 plot area
    vp = Viewport(0.0, 2.0, 0.0, 10.0)
    coords = polyline([0.0, 1.0, 2.0], [0.0, 5.0, math.nan], vp, area)
    assert coords == [64.0, 112.0, 164.0, 62.0]


def test_cursor_readout_nearest_sample() -> None:
    cs = _channels()
    readout = cursor_readout(cs, ["a", "b", "missing"], 3.04)
    assert [(r.name, r.value) for r in readout] == [("a", 30.0), ("b", -30.0)]
    assert cursor_readout(cs, ["a"], -5.0)[0].value == 0.0
    assert cursor_readout(cs, ["a"], 99.0)[0].value == 100.0


def test_throttle() -> None:
    now = [0.0]
    throttle = Throttle(0.1, clock=lambda: now[0])
    assert throttle.ready()
    now[0] = 0.05
    assert not throttle.ready()
    now[0] = 0.1
    assert throttle.ready()


def test_themes_have_distinct_palettes() -> None:
    for theme in THEMES.values():
        assert len(set(theme.palette)) == len(theme.palette) == 8
        assert channel_color(theme, 9) == theme.palette[1]
