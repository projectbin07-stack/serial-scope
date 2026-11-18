"""JSON lines: one JSON object per line, as printed by ArduinoJson's ``serializeJson``.

Numbers and booleans become channels; nested objects are flattened with dots and arrays
with their index (``{"imu": {"acc": [0.1, 0.2]}}`` -> ``imu.acc.0``, ``imu.acc.1``).
Strings are ignored. A top-level device-clock key (``t_ms``, ``millis``, ...) becomes the
device time. Lines that do not start with ``{`` are text; lines that do but fail to parse
are ``json`` errors.
"""

from __future__ import annotations

import json
import math
from typing import Any

from serial_scope.decoders.base import DEVICE_TIME_KEYS, LineDecoder
from serial_scope.events import Event, Sample


def flatten_numbers(obj: Any, prefix: str = "") -> dict[str, float]:
    """Flatten the numeric leaves of a parsed JSON value."""
    out: dict[str, float] = {}
    if isinstance(obj, bool):
        out[prefix] = 1.0 if obj else 0.0
    elif isinstance(obj, (int, float)):
        value = float(obj)
        if math.isfinite(value):
            out[prefix] = value
    elif isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flatten_numbers(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.update(flatten_numbers(value, f"{prefix}.{index}" if prefix else str(index)))
    return out


class JsonLinesDecoder(LineDecoder):
    name = "jsonl"
    description = "one JSON object per line; numbers and booleans become channels"

    def decode_line(self, t: float, text: str, raw: bytes) -> list[Event]:
        stripped = text.strip()
        if not stripped.startswith("{"):
            return [self.text(t, text)]
        try:
            obj = json.loads(stripped)
        except ValueError as exc:
            return [self.text(t, text), self.error(t, "json", f"invalid JSON: {exc}", raw)]
        if not isinstance(obj, dict):  # pragma: no cover - '{' guarantees an object
            return [self.text(t, text)]
        device_t: float | None = None
        for key, scale in DEVICE_TIME_KEYS.items():
            value = obj.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                device_t = float(value) * scale
                obj = {k: v for k, v in obj.items() if k != key}
                break
        values = flatten_numbers(obj)
        if not values and device_t is None:
            return [self.text(t, text)]
        self.counts["samples"] += 1
        return [Sample(t=t, values=values, device_t=device_t, source="jsonl")]
