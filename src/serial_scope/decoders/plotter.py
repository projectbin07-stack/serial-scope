"""Arduino serial-plotter style lines.

Accepted, one record per line, separated by commas, spaces or tabs::

    1.5 2.25 -3                 -> ch1, ch2, ch3
    temp:21.5,hum:40            -> temp, hum
    temp: 21.5  hum: 40         -> temp, hum (a space after ':' is allowed)
    a:1, 2                      -> a, ch2 (unlabelled values are named by position)

A line in which any token is neither a number nor ``label:number`` is passed through
as text, so log messages mixed with data still show up in the terminal.
"""

from __future__ import annotations

import math
import re

from serial_scope.decoders.base import LineDecoder, parse_number
from serial_scope.events import Event, Sample

_SEPARATORS = re.compile(r"[,\s]+")
_LABEL_SPACE = re.compile(r":\s+")
_LABEL = re.compile(r"^[A-Za-z_][\w.\-\[\]/%]*$")


def parse_plotter_line(text: str) -> dict[str, float] | None:
    """Return the channel values in ``text``, or ``None`` if it is not a plotter line.

    Non-finite values (``nan``, ``inf``) are recognised but left out of the result.
    """
    stripped = _LABEL_SPACE.sub(":", text.strip())
    if not stripped:
        return None
    tokens = [tok for tok in _SEPARATORS.split(stripped) if tok]
    values: dict[str, float] = {}
    for index, token in enumerate(tokens, start=1):
        label, colon, number = token.rpartition(":")
        if colon:
            if not _LABEL.match(label):
                return None
            name = label
        else:
            name = f"ch{index}"
        value = parse_number(number)
        if value is None:
            return None
        if math.isfinite(value):
            values[name] = value
    return values


class PlotterDecoder(LineDecoder):
    name = "plotter"
    description = "Arduino plotter: 'a:1.5,b:2' or space/tab/comma separated numbers"

    def decode_line(self, t: float, text: str, raw: bytes) -> list[Event]:
        values = parse_plotter_line(text)
        if values is None:
            return [self.text(t, text)]
        self.counts["samples"] += 1
        return [Sample(t=t, values=values, source="plotter")]
