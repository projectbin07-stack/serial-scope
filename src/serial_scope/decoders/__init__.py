"""Decoder registry.

To add a format, subclass :class:`~serial_scope.decoders.base.Decoder` (or
:class:`~serial_scope.decoders.base.LineDecoder` for line-based formats) and call
:func:`register_decoder`. Pass ``auto_rank`` to take part in auto-detection; lower ranks
are tried first.
"""

from __future__ import annotations

from collections.abc import Callable

from serial_scope.decoders.auto import AutoDecoder
from serial_scope.decoders.base import Decoder, LineDecoder
from serial_scope.decoders.csvlines import CsvDecoder
from serial_scope.decoders.ina219 import Ina219Decoder
from serial_scope.decoders.jsonlines import JsonLinesDecoder
from serial_scope.decoders.nrf24 import Nrf24GatewayDecoder
from serial_scope.decoders.odometry import OdometryDecoder
from serial_scope.decoders.plotter import PlotterDecoder
from serial_scope.decoders.text import TextDecoder

__all__ = [
    "Decoder",
    "LineDecoder",
    "available_decoders",
    "create_decoder",
    "register_decoder",
]

_REGISTRY: dict[str, tuple[type[Decoder], int | None]] = {}


def register_decoder(cls: type[Decoder], auto_rank: int | None = None) -> None:
    """Make ``cls`` available by its ``name``; ``auto_rank`` enables auto-detection."""
    if not cls.name or cls.name == "auto":
        raise ValueError(f"invalid decoder name {cls.name!r}")
    _REGISTRY[cls.name] = (cls, auto_rank)


def available_decoders() -> dict[str, str]:
    """Return ``{name: description}`` for every decoder, ``auto`` first."""
    out = {"auto": AutoDecoder.description}
    out.update({name: cls.description for name, (cls, _) in _REGISTRY.items()})
    return out


def create_decoder(name: str = "auto") -> Decoder:
    """Instantiate a decoder by name. ``auto`` detects among the ranked decoders."""
    if name == "auto":
        ranked = sorted(
            ((rank, cls) for cls, rank in _REGISTRY.values() if rank is not None),
            key=lambda item: item[0],
        )
        factories: list[Callable[[], Decoder]] = [cls for _, cls in ranked]
        return AutoDecoder(factories)
    try:
        cls, _ = _REGISTRY[name]
    except KeyError:
        known = ", ".join(available_decoders())
        raise ValueError(f"unknown decoder {name!r} (known: {known})") from None
    return cls()


register_decoder(Ina219Decoder, auto_rank=10)
register_decoder(OdometryDecoder, auto_rank=20)
register_decoder(Nrf24GatewayDecoder, auto_rank=25)
register_decoder(JsonLinesDecoder, auto_rank=30)
register_decoder(CsvDecoder, auto_rank=40)
register_decoder(PlotterDecoder, auto_rank=50)
register_decoder(TextDecoder, auto_rank=1000)
