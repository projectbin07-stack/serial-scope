"""Serial port discovery with USB VID:PID and a hint about the likely board."""

from __future__ import annotations

from dataclasses import dataclass

# USB vendor/product ids of bridges and native-USB chips commonly found on hobby boards.
# Matching is by VID:PID first, then by VID alone.
KNOWN_USB: dict[tuple[int, int | None], str] = {
    (0x10C4, 0xEA60): "Silicon Labs CP210x (ESP32 DevKit, NodeMCU)",
    (0x1A86, 0x7523): "WCH CH340 (Arduino Nano clones, ESP boards)",
    (0x1A86, 0x55D4): "WCH CH9102 (ESP32 DevKit v1.1, LilyGO)",
    (0x0403, 0x6001): "FTDI FT232R",
    (0x0403, 0x6015): "FTDI FT231X",
    (0x0403, 0x6010): "FTDI FT2232",
    (0x303A, 0x1001): "Espressif native USB-Serial/JTAG (ESP32-S3/C3/C6)",
    (0x303A, None): "Espressif native USB",
    (0x2341, None): "Arduino (genuine board)",
    (0x2A03, None): "Arduino (arduino.org board)",
    (0x0483, 0x5740): "STMicroelectronics virtual COM port (STM32 USB CDC)",
    (0x0483, 0x374B): "ST-LINK/V2-1 virtual COM port (Nucleo, Discovery)",
    (0x0483, 0x374E): "ST-LINK/V3 virtual COM port",
    (0x0483, 0x3752): "ST-LINK/V2-1 virtual COM port",
    (0x2E8A, None): "Raspberry Pi RP2040 / RP2350",
    (0x239A, None): "Adafruit board",
}


def usb_hint(vid: int | None, pid: int | None) -> str:
    """Name the chip or board family for a VID:PID, or return an empty string."""
    if vid is None:
        return ""
    return KNOWN_USB.get((vid, pid)) or KNOWN_USB.get((vid, None)) or ""


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str = ""
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None

    @property
    def usb_id(self) -> str:
        if self.vid is None or self.pid is None:
            return ""
        return f"{self.vid:04X}:{self.pid:04X}"

    @property
    def hint(self) -> str:
        return usb_hint(self.vid, self.pid)

    def label(self) -> str:
        """One-line description for the port picker."""
        parts = [self.device]
        if self.usb_id:
            parts.append(self.usb_id)
        if self.description and self.description not in ("n/a", self.device):
            parts.append(self.description)
        if self.hint:
            parts.append(f"[{self.hint}]")
        return "  ".join(parts)


def _port_sort_key(port: PortInfo) -> tuple[str, int, str]:
    # COM10 after COM9: split a trailing number off the device name.
    name = port.device
    stem = name.rstrip("0123456789")
    digits = name[len(stem) :]
    return (stem, int(digits) if digits else -1, name)


def list_ports() -> list[PortInfo]:
    """List serial ports known to the operating system, USB devices first."""
    from serial.tools import list_ports as lp

    ports = [
        PortInfo(
            device=p.device,
            description=p.description or "",
            vid=p.vid,
            pid=p.pid,
            serial_number=p.serial_number,
            manufacturer=p.manufacturer,
        )
        for p in lp.comports()
    ]
    return sorted(ports, key=lambda p: (p.vid is None, *_port_sort_key(p)))
