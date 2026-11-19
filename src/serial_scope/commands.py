"""Commands to encoder-odometry-firmware: velocity, stop and parameter get / set.

These build the host-to-firmware frames of protocol version 1 (VELOCITY_COMMAND 0x01,
PARAM_GET 0x05, PARAM_SET 0x06) with the same encoder the decoder is tested with, so the
bytes match the firmware's published vectors. Nothing here opens a port; the CLI
(``serial-scope send``) and the GUI command panel write the frames.

Safety: a velocity command makes the motors move. The firmware stops them by itself
when no valid command has arrived for ``watchdog_ms`` (default 500 ms), so a single
command moves the robot for about that long, and a host that wants continuous motion has
to repeat the command (the CLI's ``--hold``, the GUI's *Repeat*). :meth:`stop` sends an
explicit zero velocity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from serial_scope.decoders.odometry import PARAM_NAMES, encode_frame

PARAM_IDS = {name: pid for pid, name in PARAM_NAMES.items()}
READ_ONLY_PARAMS = frozenset({21, 22})
"""``control_rate_hz`` and ``protocol_version``: the firmware answers a set with
status 3 (read-only), so the encoder refuses to build one."""
REPEAT_HZ = 10.0
"""How often a held velocity command is repeated: five times per default watchdog
period, so one or two lost frames do not stop the motors."""


class CommandError(ValueError):
    """A command that cannot be encoded (bad number, unknown or read-only parameter)."""


def resolve_param(name: str | int) -> int:
    """Parameter id from a name (``kp``) or a number (``1``)."""
    if isinstance(name, int):
        pid = name
    elif name.strip().isdigit():
        pid = int(name)
    elif name.strip() in PARAM_IDS:
        return PARAM_IDS[name.strip()]
    else:
        raise CommandError(f"unknown parameter {name!r} (known: {', '.join(PARAM_IDS)})")
    if not 0 <= pid <= 255:
        raise CommandError(f"parameter id {pid} is outside 0..255")
    return pid


def _finite(label: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise CommandError(f"{label} must be a finite number, not {value}")
    return value


@dataclass(frozen=True)
class Command:
    """One frame to send, with a human-readable description."""

    name: str
    seq: int
    frame: bytes
    text: str
    param_id: int | None = None


class OdometryCommander:
    """Builds host-to-firmware frames with the host's own sequence counter.

    The protocol gives each direction its own counter, incremented per frame and
    wrapping 255 -> 0; ``seq`` is the value the next frame will carry.
    """

    def __init__(self, seq: int = 0) -> None:
        self.seq = seq & 0xFF

    def _build(self, name: str, fields: dict[str, float], text: str) -> Command:
        seq = self.seq
        self.seq = (seq + 1) & 0xFF
        pid = int(fields["id"]) if "id" in fields else None
        return Command(name, seq, encode_frame(name, seq, fields), text, pid)

    def velocity(self, vx: float, vy: float, wz: float) -> Command:
        """VELOCITY_COMMAND: vx, vy in m/s, wz in rad/s (REP-103 body frame)."""
        fields = {
            "vx": _finite("vx", vx),
            "vy": _finite("vy", vy),
            "wz": _finite("wz", wz),
        }
        text = f"velocity vx={fields['vx']:g} m/s vy={fields['vy']:g} m/s wz={fields['wz']:g} rad/s"
        return self._build("velocity_command", fields, text)

    def stop(self) -> Command:
        """A zero VELOCITY_COMMAND: targets go to zero at once, without waiting for the
        watchdog."""
        command = self.velocity(0.0, 0.0, 0.0)
        return Command(command.name, command.seq, command.frame, "stop (zero velocity)")

    def param_get(self, param: str | int) -> Command:
        pid = resolve_param(param)
        return self._build("param_get", {"id": pid}, f"get {_param_label(pid)}")

    def param_set(self, param: str | int, value: float) -> Command:
        pid = resolve_param(param)
        if pid in READ_ONLY_PARAMS:
            raise CommandError(f"{_param_label(pid)} is read-only")
        number = _finite("value", value)
        text = f"set {_param_label(pid)} = {number:g}"
        return self._build("param_set", {"id": pid, "value": number}, text)


def _param_label(pid: int) -> str:
    return PARAM_NAMES.get(pid, f"id{pid}")


def parse_action(tokens: list[str], commander: OdometryCommander) -> Command:
    """Build a command from CLI words: ``vel VX VY WZ``, ``stop``, ``get-param NAME`` or
    ``set-param NAME VALUE``."""
    if not tokens:
        raise CommandError("missing action: vel, stop, get-param or set-param")
    action, args = tokens[0], tokens[1:]
    expected = {"vel": 3, "stop": 0, "get-param": 1, "set-param": 2}
    if action not in expected:
        raise CommandError(f"unknown action {action!r} (use vel, stop, get-param or set-param)")
    if len(args) != expected[action]:
        usage = {
            "vel": "vel VX VY WZ",
            "stop": "stop",
            "get-param": "get-param NAME",
            "set-param": "set-param NAME VALUE",
        }[action]
        raise CommandError(f"usage: --odometry {usage}")
    try:
        numbers = [float(a) for a in args] if action == "vel" else []
        if action == "set-param":
            numbers = [float(args[1])]
    except ValueError as exc:
        raise CommandError(f"not a number: {exc}") from None
    if action == "vel":
        return commander.velocity(*numbers)
    if action == "stop":
        return commander.stop()
    if action == "get-param":
        return commander.param_get(args[0])
    return commander.param_set(args[0], numbers[0])
