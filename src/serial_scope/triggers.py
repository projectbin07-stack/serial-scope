"""Triggers on channel values and text lines, with actions.

Trigger kinds:

* threshold ``CH > LEVEL`` / ``CH < LEVEL`` (also ``>=``, ``<=``): fires when the
  condition becomes true, and re-arms once the value has moved back past the level by
  ``hysteresis``.
* edge ``rise:CH@LEVEL`` / ``fall:CH@LEVEL``: fires when consecutive samples cross the
  level upwards (from ``< LEVEL`` to ``>= LEVEL``) or downwards.
* regex ``/PATTERN/`` or ``re:PATTERN``: fires on every text line (or device notice,
  such as a radio link report) that matches (``re.search``). The ``re:`` form avoids
  Git Bash on Windows rewriting a leading ``/`` as a path.

Actions: ``mark`` (add a marker to the log), ``beep`` (ring the bell in the GUI, BEL in
the terminal), ``stop:N`` (stop logging N seconds later), ``snapshot`` (write the channel
buffers to a CSV file).

Command-line syntax is ``CONDITION -> ACTION[,ACTION...]``, for example
``"current_ma > 500 -> mark,snapshot"``, ``"rise:bus_v@3.3 -> beep"`` or
``"/Guru Meditation|panic/ -> mark,stop:2"``. Without ``->`` the action is ``mark``.
A per-trigger ``holdoff`` (seconds) suppresses repeated firings.
"""

from __future__ import annotations

import operator
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from serial_scope.events import Event, Notice, Sample, TextLine, TriggerFired

ACTIONS = ("mark", "beep", "stop", "snapshot")
_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


@dataclass
class Action:
    kind: str
    seconds: float = 0.0

    def __str__(self) -> str:
        return f"stop:{self.seconds:g}" if self.kind == "stop" else self.kind


class TriggerSpecError(ValueError):
    """A trigger description could not be parsed."""


@dataclass
class Trigger(ABC):
    label: str
    actions: list[Action] = field(default_factory=lambda: [Action("mark")])
    holdoff: float = 0.0
    fired: int = 0
    _last_fire: float | None = field(default=None, repr=False)

    @abstractmethod
    def check(self, event: Event) -> str | None:
        """Return a description if ``event`` satisfies the condition."""

    def evaluate(self, event: Event) -> TriggerFired | None:
        detail = self.check(event)
        if detail is None:
            return None
        t = event.t
        if self._last_fire is not None and t - self._last_fire < self.holdoff:
            return None
        self._last_fire = t
        self.fired += 1
        return TriggerFired(t, self.label, detail, tuple(str(a) for a in self.actions))


@dataclass
class ThresholdTrigger(Trigger):
    channel: str = ""
    op: str = ">"
    level: float = 0.0
    hysteresis: float = 0.0
    _armed: bool = field(default=True, repr=False)

    def check(self, event: Event) -> str | None:
        if not isinstance(event, Sample) or self.channel not in event.values:
            return None
        value = event.values[self.channel]
        active = _OPS[self.op](value, self.level)
        if active and self._armed:
            self._armed = False
            return f"{self.channel} = {value:g} {self.op} {self.level:g}"
        if not active and not self._armed:
            rearm = (
                value < self.level - self.hysteresis
                if self.op.startswith(">")
                else value > self.level + self.hysteresis
            )
            if rearm:
                self._armed = True
        return None


@dataclass
class EdgeTrigger(Trigger):
    channel: str = ""
    level: float = 0.0
    rising: bool = True
    _prev: float | None = field(default=None, repr=False)

    def check(self, event: Event) -> str | None:
        if not isinstance(event, Sample) or self.channel not in event.values:
            return None
        value = event.values[self.channel]
        prev, self._prev = self._prev, value
        if prev is None:
            return None
        if self.rising and prev < self.level <= value:
            return f"{self.channel} rose through {self.level:g} ({prev:g} -> {value:g})"
        if not self.rising and prev >= self.level > value:
            return f"{self.channel} fell through {self.level:g} ({prev:g} -> {value:g})"
        return None


@dataclass
class RegexTrigger(Trigger):
    pattern: str = ""
    _regex: re.Pattern[str] | None = field(default=None, repr=False)

    def check(self, event: Event) -> str | None:
        if isinstance(event, TextLine):
            text = event.text
        elif isinstance(event, Notice):
            text = f"{event.kind}: {event.detail}"
        else:
            return None
        if self._regex is None:
            self._regex = re.compile(self.pattern)
        if self._regex.search(text):
            return f"line matched /{self.pattern}/: {text.strip()[:120]}"
        return None


def parse_actions(text: str) -> list[Action]:
    actions: list[Action] = []
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        kind, _, arg = item.partition(":")
        kind = kind.strip().lower()
        if kind not in ACTIONS:
            raise TriggerSpecError(f"unknown action {kind!r} (use {', '.join(ACTIONS)})")
        if kind == "stop":
            try:
                seconds = float(arg) if arg else 0.0
            except ValueError:
                raise TriggerSpecError(f"stop needs a number of seconds: {item!r}") from None
            if seconds < 0:
                raise TriggerSpecError("stop seconds must be >= 0")
            actions.append(Action("stop", seconds))
        elif arg:
            raise TriggerSpecError(f"action {kind!r} takes no argument")
        else:
            actions.append(Action(kind))
    if not actions:
        raise TriggerSpecError("no actions given")
    return actions


_THRESHOLD = re.compile(r"^\s*(?P<ch>[^<>=\s]+)\s*(?P<op>>=|<=|>|<)\s*(?P<level>\S+)\s*$")
_EDGE = re.compile(r"^\s*(?P<dir>rise|fall)\s*:\s*(?P<ch>[^@\s]+)\s*@\s*(?P<level>\S+)\s*$")


def _level(text: str, spec: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise TriggerSpecError(f"level is not a number in {spec!r}") from None


def parse_trigger(spec: str, hysteresis: float = 0.0, holdoff: float = 0.0) -> Trigger:
    """Parse ``CONDITION [-> ACTIONS]`` (see the module documentation)."""
    condition, arrow, action_text = spec.partition("->")
    actions = parse_actions(action_text) if arrow else [Action("mark")]
    condition = condition.strip()
    label = condition
    pattern: str | None = None
    if condition.startswith("/") and condition.endswith("/") and len(condition) >= 2:
        pattern = condition[1:-1]
    elif condition.startswith("re:"):
        pattern = condition[3:].strip()
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise TriggerSpecError(f"bad regular expression {pattern!r}: {exc}") from None
        return RegexTrigger(label, actions, holdoff, pattern=pattern)
    m = _EDGE.match(condition)
    if m:
        return EdgeTrigger(
            label,
            actions,
            holdoff,
            channel=m["ch"],
            level=_level(m["level"], spec),
            rising=m["dir"] == "rise",
        )
    m = _THRESHOLD.match(condition)
    if m:
        return ThresholdTrigger(
            label,
            actions,
            holdoff,
            channel=m["ch"],
            op=m["op"],
            level=_level(m["level"], spec),
            hysteresis=hysteresis,
        )
    raise TriggerSpecError(
        f"cannot parse trigger {spec!r}; expected 'CH > LEVEL', 'rise:CH@LEVEL', "
        "'/REGEX/' or 're:REGEX'"
    )


class TriggerSet:
    """Evaluates a list of triggers against each event."""

    def __init__(self, triggers: list[Trigger] | None = None) -> None:
        self.triggers: list[Trigger] = list(triggers or [])

    def add(self, trigger: Trigger) -> None:
        self.triggers.append(trigger)

    def evaluate(self, event: Event) -> list[TriggerFired]:
        fired: list[TriggerFired] = []
        for trigger in self.triggers:
            result = trigger.evaluate(event)
            if result is not None:
                fired.append(result)
        return fired
