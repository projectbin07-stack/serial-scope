"""Capture the README screenshots from replayed synthetic demo data (Windows only).

Usage::

    python scripts/screenshot.py docs/ [plotter] [ina219] [nrf24] [odometry]

It writes demo logs with ``serial_scope.demo``, opens the real GUI on each one in replay
mode, waits, and grabs the window with PowerShell's System.Drawing (no Pillow needed).
No hardware is involved; the data shown is synthetic.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

from serial_scope.app import AppController
from serial_scope.demo import write_demo_log
from serial_scope.gui import MainWindow

PS_CAPTURE = """
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap {w}, {h}
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen({x}, {y}, 0, 0, $bmp.Size)
$bmp.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png)
"""


def window_rect(root: tk.Tk) -> tuple[int, int, int, int]:
    hwnd = int(root.wm_frame(), 16)
    rect = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS excludes the invisible resize border.
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def capture(root: tk.Tk, out: Path) -> None:
    root.update()
    x, y, w, h = window_rect(root)
    script = PS_CAPTURE.format(x=x, y=y, w=w, h=h, out=str(out.resolve()))
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    print(f"wrote {out} ({w}x{h})")


def shoot(
    log: Path,
    out: Path,
    theme: str,
    seconds: float,
    tab: int,
    triggers: list[str],
    pause_cursor: float | None,
    window_s: str = "10",
    visible: tuple[str, ...] = (),
) -> None:
    root = tk.Tk()
    root.geometry("1280x800+40+40")
    controller = AppController(snapshot_dir=tempfile.gettempdir())
    window = MainWindow(root, controller, theme)
    window.window_var.set(window_s)
    for spec in triggers:
        window.trigger_var.set(spec)
        window.add_trigger()
    window.notebook.select(tab)
    root.attributes("-topmost", True)
    root.after(300, lambda: window.start_replay(log))

    def finish() -> None:
        if visible:
            for name, var in window.visible.items():
                var.set(name in visible)
            window.draw_plot()
        if pause_cursor is not None:
            window.toggle_pause()
            area_w = window.canvas.winfo_width()
            window.cursor_x = 64 + (area_w - 76) * pause_cursor
            window.draw_plot()
        window.refresh_tables()
        window.sync_command_panel()
        root.after(400, lambda: (capture(root, out), window.close()))

    root.after(int(300 + seconds * 1000), finish)
    root.mainloop()


def main() -> int:
    if sys.platform != "win32":
        print("this script uses Windows APIs to capture the window", file=sys.stderr)
        return 1
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "docs")
    wanted = set(sys.argv[2:]) or {"plotter", "ina219", "nrf24", "odometry"}
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        logs = {}
        for kind in wanted:
            logs[kind] = Path(tmp) / f"demo-{kind}.sslog"
            write_demo_log(logs[kind], kind)
        if "plotter" in wanted:
            shoot(
                logs["plotter"],
                out_dir / "screenshot-plotter-light.png",
                "light",
                13.0,
                0,
                ["speed > 160 -> mark", "re:RSSI -> mark"],
                pause_cursor=0.62,
            )
        if "ina219" in wanted:
            shoot(
                logs["ina219"],
                out_dir / "screenshot-ina219-dark.png",
                "dark",
                14.0,
                1,
                [],
                None,
                "30",
            )
        if "nrf24" in wanted:
            shoot(
                logs["nrf24"],
                out_dir / "screenshot-nrf24-light.png",
                "light",
                15.5,
                2,
                ["re:not delivered -> mark"],
                None,
                "30",
                visible=("odom.x", "odom.y", "imu.gz", "link.crc_errors", "link.unreliable_lost"),
            )
        if "odometry" in wanted:
            shoot(
                logs["odometry"],
                out_dir / "screenshot-odometry-commands-dark.png",
                "dark",
                8.0,
                1,
                [],
                None,
                "5",
                visible=("fl.velocity", "fr.velocity", "fl.target", "fr.target"),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
