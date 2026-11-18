from __future__ import annotations

from types import SimpleNamespace

import pytest

from serial_scope import ports
from serial_scope.ports import PortInfo, list_ports, usb_hint
from serial_scope.terminal import TextRenderer, encode_input, hexdump, hexdump_line


def test_text_renderer_reassembles_utf8_and_crlf() -> None:
    r = TextRenderer()
    degree = "°C".encode()
    out = r.feed(b"21.5 " + degree[:1]) + r.feed(degree[1:] + b"\r") + r.feed(b"\nok\rnext")
    assert out == "21.5 °C\nok\nnext"


def test_text_renderer_shows_control_characters() -> None:
    assert (
        TextRenderer().feed(b"a\x00b\x07\x1b[0m\x7f\x02\tz") == "a<NUL>b<BEL><ESC>[0m<DEL><x02>\tz"
    )


def test_hexdump() -> None:
    assert hexdump_line(b"AB\x00", 16) == "00000010  41 42 00" + " " * 39 + "  AB."
    lines = hexdump(bytes(range(20)))
    assert len(lines) == 2 and lines[1].startswith("00000010  10 11 12 13")


@pytest.mark.parametrize(
    ("text", "ending", "expected"),
    [
        ("RESET", "LF", b"RESET\n"),
        ("RESET", "CR+LF", b"RESET\r\n"),
        ("RESET", "None", b"RESET"),
        ("RESET", "CR", b"RESET\r"),
        (r"a\tb\\n\x41\0", "None", b"a\tb\\nA\x00"),
        (r"bad \x4", "None", b"bad \\x4"),
        ("°", "None", "°".encode()),
    ],
)
def test_encode_input_text(text: str, ending: str, expected: bytes) -> None:
    assert encode_input(text, ending) == expected


def test_encode_input_hex() -> None:
    assert encode_input("01 02,ff:7E", hex_mode=True) == b"\x01\x02\xff\x7e"
    for bad in ("0", "zz", "123"):
        with pytest.raises(ValueError):
            encode_input(bad, hex_mode=True)
    with pytest.raises(ValueError):
        encode_input("x", "CRLF")


def test_usb_hints() -> None:
    assert "CP210x" in usb_hint(0x10C4, 0xEA60)
    assert "CH340" in usb_hint(0x1A86, 0x7523)
    assert "Espressif" in usb_hint(0x303A, 0x9999)  # vendor-only match
    assert "ST-LINK" in usb_hint(0x0483, 0x374B)
    assert usb_hint(0x1234, 0x5678) == ""
    assert usb_hint(None, None) == ""


def test_port_label() -> None:
    p = PortInfo("COM5", "Silicon Labs CP210x USB to UART Bridge (COM5)", 0x10C4, 0xEA60)
    assert p.usb_id == "10C4:EA60"
    assert p.label().startswith("COM5  10C4:EA60  Silicon Labs CP210x")
    assert p.label().endswith("[Silicon Labs CP210x (ESP32 DevKit, NodeMCU)]")
    assert PortInfo("/dev/ttyS0", "n/a").label() == "/dev/ttyS0"


def test_list_ports_sorts_usb_first_and_numerically(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = [
        SimpleNamespace(
            device="COM1",
            description="Communications Port (COM1)",
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
        ),
        SimpleNamespace(
            device="COM10",
            description="USB-SERIAL CH340 (COM10)",
            vid=0x1A86,
            pid=0x7523,
            serial_number=None,
            manufacturer="wch.cn",
        ),
        SimpleNamespace(
            device="COM9",
            description="USB Serial Device (COM9)",
            vid=0x2341,
            pid=0x0043,
            serial_number="7563",
            manufacturer="Arduino",
        ),
    ]
    import serial.tools.list_ports as lp

    monkeypatch.setattr(lp, "comports", lambda: fake)
    result = list_ports()
    assert [p.device for p in result] == ["COM9", "COM10", "COM1"]
    assert result[0].hint == "Arduino (genuine board)"
    assert ports._port_sort_key(PortInfo("ttyACM")) == ("ttyACM", -1, "ttyACM")
