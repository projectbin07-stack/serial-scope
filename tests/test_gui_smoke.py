"""Builds the real Tk window and replays a demo log through it.

Skipped when Tk cannot open a display (for example on a headless Linux machine without
Xvfb). CI runs it under ``xvfb-run`` on Ubuntu and natively on Windows.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from serial_scope.demo import write_demo_log

if TYPE_CHECKING:
    from serial_scope.app import AppController
    from serial_scope.gui import MainWindow

tk = pytest.importorskip("tkinter")


def _make_root() -> object:
    try:
        return tk.Tk()
    except tk.TclError as exc:  # no display
        if os.environ.get("SERIAL_SCOPE_REQUIRE_GUI"):
            raise  # CI sets this so the test cannot silently skip there
        pytest.skip(f"Tk cannot open a display: {exc}")


def test_main_window_replays_demo(tmp_path: Path) -> None:
    from serial_scope.app import AppController
    from serial_scope.gui import MainWindow

    log = tmp_path / "demo.sslog"
    write_demo_log(log, "plotter", seconds=4)
    root = _make_root()
    assert isinstance(root, tk.Tk)
    root.geometry("1000x700")
    controller = AppController(snapshot_dir=tmp_path)
    window = MainWindow(root, controller, "dark")
    try:
        window.speed_var.set("max")
        window.trigger_var.set("speed > 100 -> mark")
        window.add_trigger()
        window.start_replay(log)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            root.update()
            reader = controller.reader
            if reader is not None and not reader.is_alive() and reader.queue.empty():
                break
            time.sleep(0.01)
        window.apply_update(controller.poll())  # anything left after the last timer tick
        window.draw_plot()
        root.update()
        window.refresh_tables()
        assert set(window.channel_widgets) == {"setpoint", "speed", "duty"}
        assert len(window.stats_tree.get_children()) == 3
        assert "setpoint:" in window.text.get("1.0", "end")
        assert window.canvas.find_all(), "plot has items"
        # Exercise the view controls once.
        window.toggle_pause()
        window.cursor_x = 300.0
        window.draw_plot()
        assert "t = " in window.readout_var.get()
        window.toggle_pause()
        window.autoscale_var.set(False)
        window._autoscale()
        window.toggle_theme()
        assert window.theme.name == "light"
        window.hex_view_var.set(True)
        window._hex_view()
        assert controller.hex_mode
        window.mark()
        window.snapshot()
        window.clear_terminal()
        window.decoder_var.set("plotter")
        window.change_decoder()
        assert window.channel_widgets == {}
        window.refresh_ports()
    finally:
        window.close()


def _replay_to_end(root: Any, window: MainWindow, controller: AppController, log: Path) -> None:
    window.start_replay(log)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        root.update()
        reader = controller.reader
        if reader is not None and not reader.is_alive() and reader.queue.empty():
            break
        time.sleep(0.01)
    window.apply_update(controller.poll())
    window.refresh_tables()
    window.sync_command_panel()
    root.update()


def test_odometry_command_panel_appears_and_refuses_replays(tmp_path: Path) -> None:
    from serial_scope.app import AppController
    from serial_scope.gui import MainWindow

    log = tmp_path / "odo.sslog"
    write_demo_log(log, "odometry", seconds=3)
    root = _make_root()
    assert isinstance(root, tk.Tk)
    controller = AppController(snapshot_dir=tmp_path)
    window = MainWindow(root, controller, "light")
    try:
        assert not window.command_panel_shown
        window.speed_var.set("max")
        _replay_to_end(root, window, controller, log)
        assert window.command_panel_shown
        assert window.command_panel.winfo_ismapped()
        window.vel_vars[0].set("0.2")
        window.send_velocity()
        assert window.status_var.get() == "not sent: not connected"
        window.stop_motors()
        assert window.status_var.get() == "stop not sent: not connected"
        window.param_var.set("kp")
        window.param_get()
        window.param_value_var.set("abc")
        window.param_set()
        assert "not sent" in window.status_var.get()
        assert controller.session.totals.tx_bytes == 0
        window.decoder_var.set("plotter")
        window.change_decoder()
        assert not window.command_panel_shown
    finally:
        window.close()


def test_nrf24_gateway_replay_fills_link_counters(tmp_path: Path) -> None:
    from serial_scope.app import AppController
    from serial_scope.gui import MainWindow

    log = tmp_path / "nrf24.sslog"
    write_demo_log(log, "nrf24", seconds=6)
    root = _make_root()
    assert isinstance(root, tk.Tk)
    controller = AppController(snapshot_dir=tmp_path)
    window = MainWindow(root, controller, "dark")
    try:
        window.speed_var.set("max")
        _replay_to_end(root, window, controller, log)
        assert not window.command_panel_shown
        assert "odom.x" in window.channel_widgets
        assert "link.crc_errors" in window.channel_widgets
        counters = {
            window.counter_tree.item(i, "text"): window.counter_tree.item(i, "values")[0]
            for i in window.counter_tree.get_children()
        }
        assert counters["decoder"] == "auto (nrf24)"
        assert "radio link: crc_errors" in counters
        assert "[gateway]" in window.text.get("1.0", "end")
        window.draw_plot()
    finally:
        window.close()
