"""Format auto-detection.

During a short detection window every candidate decoder is fed the same bytes in
parallel. The window closes as soon as one structured candidate has decoded
:data:`CONFIDENT_SAMPLES` records with few errors, or after :data:`MAX_DETECT_BYTES`
bytes or :data:`MAX_DETECT_SECONDS` of stream time, whichever comes first. The winner's
buffered events are then released, so nothing received during detection is lost, and
the winner decodes the rest of the stream on its own.

Candidates are ranked from most to least specific: a checksummed format beats JSON, JSON
beats CSV, CSV (which names its columns) beats the bare plotter format, and plain text is
the fallback.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from serial_scope.decoders.base import Decoder
from serial_scope.events import Event, Sample

CONFIDENT_SAMPLES = 5
MAX_DETECT_BYTES = 4096
MAX_DETECT_SECONDS = 3.0


class AutoDecoder(Decoder):
    name = "auto"
    description = "detect the format from the first records (default)"

    def __init__(self, candidates: list[Callable[[], Decoder]]) -> None:
        super().__init__()
        self._candidates = [factory() for factory in candidates]
        self._pending: dict[str, list[Event]] = {d.name: [] for d in self._candidates}
        self._samples: Counter[str] = Counter()
        self._bytes = 0
        self._t_first: float | None = None
        self.chosen: Decoder | None = None

    @property
    def detecting(self) -> bool:
        return self.chosen is None

    def feed(self, data: bytes, t: float) -> list[Event]:
        if self.chosen is not None:
            return self.chosen.feed(data, t)
        if self._t_first is None and data:
            self._t_first = t
        self._bytes += len(data)
        for decoder in self._candidates:
            events = decoder.feed(data, t)
            self._pending[decoder.name].extend(events)
            self._samples[decoder.name] += sum(isinstance(e, Sample) for e in events)
        confident = self._pick(CONFIDENT_SAMPLES, max_error_ratio=0.25)
        if confident is not None:
            return self._commit(confident)
        elapsed = t - self._t_first if self._t_first is not None else 0.0
        if self._bytes >= MAX_DETECT_BYTES or elapsed >= MAX_DETECT_SECONDS:
            return self._commit(self._pick(1) or self._fallback())
        return []

    def flush(self, t: float) -> list[Event]:
        if self.chosen is not None:
            return self.chosen.flush(t)
        for decoder in self._candidates:
            events = decoder.flush(t)
            self._pending[decoder.name].extend(events)
            self._samples[decoder.name] += sum(isinstance(e, Sample) for e in events)
        return self._commit(self._pick(1) or self._fallback())

    def _pick(self, min_samples: int, max_error_ratio: float = 1.0) -> Decoder | None:
        for decoder in self._candidates:
            samples = self._samples[decoder.name]
            errors = sum(decoder.errors.values())
            if samples >= min_samples and errors <= samples * max_error_ratio:
                return decoder
        return None

    def _fallback(self) -> Decoder:
        return self._candidates[-1]

    def _commit(self, decoder: Decoder) -> list[Event]:
        self.chosen = decoder
        self.errors = decoder.errors
        self.counts = decoder.counts
        events = self._pending[decoder.name]
        self._pending.clear()
        return events
