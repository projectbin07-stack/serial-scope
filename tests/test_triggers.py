from __future__ import annotations

from collections.abc import Sequence

import pytest

from serial_scope.events import Sample, TextLine
from serial_scope.triggers import (
    Action,
    EdgeTrigger,
    RegexTrigger,
    ThresholdTrigger,
    TriggerSet,
    TriggerSpecError,
    parse_actions,
    parse_trigger,
)


def _run(trigger_spec: str, values: Sequence[float], **kw: float) -> list[int]:
    trigger = parse_trigger(trigger_spec, **kw)
    fired = []
    for i, v in enumerate(values):
        if trigger.evaluate(Sample(float(i), {"v": v})) is not None:
            fired.append(i)
    return fired


def test_threshold_fires_once_per_excursion() -> None:
    values = [1, 2, 6, 7, 6, 2, 1, 8, 9]
    assert _run("v > 5", values) == [2, 7]


def test_threshold_hysteresis_prevents_chatter() -> None:
    noisy = [4, 6, 4.9, 5.2, 4.8, 5.1, 3.9, 5.5]
    assert _run("v > 5", noisy) == [1, 3, 5, 7]
    assert _run("v > 5", noisy, hysteresis=1.0) == [1, 7]


def test_threshold_below_and_inclusive() -> None:
    assert _run("v < 3.3", [3.4, 3.3, 3.2, 3.5, 3.0]) == [2, 4]
    assert _run("v <= 3.3", [3.4, 3.3, 3.2]) == [1]
    assert _run("v >= 5", [4, 5, 5, 4, 5]) == [1, 4]


def test_edges() -> None:
    values = [0, 2, 4, 2, 4, 4, 1]
    assert _run("rise:v@3", values) == [2, 4]
    assert _run("fall:v@3", values) == [3, 6]
    assert _run("rise:v@3", [5, 5]) == []  # never below: no edge


def test_holdoff() -> None:
    assert _run("rise:v@1", [0, 2, 0, 2, 0, 2, 0, 2], holdoff=3.0) == [1, 5]


def test_trigger_ignores_other_channels_and_events() -> None:
    t = parse_trigger("v > 1")
    assert t.evaluate(Sample(0.0, {"w": 5.0})) is None
    assert t.evaluate(TextLine(0.0, "v > 1")) is None
    r = parse_trigger("/err/")
    assert r.evaluate(Sample(0.0, {"v": 1.0})) is None


def test_regex_trigger() -> None:
    t = parse_trigger(r"/Guru Meditation|panic\(/ -> mark,beep")
    assert isinstance(t, RegexTrigger)
    fired = t.evaluate(TextLine(3.0, "Guru Meditation Error: Core 1 panic'ed"))
    assert fired is not None
    assert fired.actions == ("mark", "beep")
    assert "Guru Meditation" in fired.detail
    assert t.evaluate(TextLine(4.0, "all good")) is None
    assert isinstance(parse_trigger("re:WARN -> mark"), RegexTrigger)


def test_parse_specs() -> None:
    t = parse_trigger("current_ma >= 500 -> mark, snapshot , stop:2.5")
    assert isinstance(t, ThresholdTrigger)
    assert (t.channel, t.op, t.level) == ("current_ma", ">=", 500.0)
    assert [str(a) for a in t.actions] == ["mark", "snapshot", "stop:2.5"]
    e = parse_trigger("fall: bus_v @ 3.0")
    assert isinstance(e, EdgeTrigger) and not e.rising and e.channel == "bus_v"
    assert [a.kind for a in e.actions] == ["mark"]
    assert parse_actions("stop") == [Action("stop", 0.0)]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("v ~ 3", "cannot parse"),
        ("v > abc", "not a number"),
        ("rise:v@x", "not a number"),
        ("/(/", "bad regular expression"),
        ("v > 1 -> explode", "unknown action"),
        ("v > 1 -> stop:soon", "seconds"),
        ("v > 1 -> stop:-1", ">= 0"),
        ("v > 1 -> beep:2", "takes no argument"),
        ("v > 1 -> ", "no actions"),
    ],
)
def test_parse_errors(spec: str, message: str) -> None:
    with pytest.raises(TriggerSpecError, match=message):
        parse_trigger(spec)


def test_trigger_set() -> None:
    ts = TriggerSet([parse_trigger("v > 1")])
    ts.add(parse_trigger("v > 2 -> beep"))
    fired = ts.evaluate(Sample(1.0, {"v": 3.0}))
    assert [f.trigger for f in fired] == ["v > 1", "v > 2"]
    assert [t.fired for t in ts.triggers] == [1, 1]
