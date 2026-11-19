"""Tk view. Widgets and event wiring only; the logic lives in app.py and plotmodel.py.

The reader thread never touches Tk. A timer (``after``) drains its queue every
:data:`POLL_MS` milliseconds, at most :data:`MAX_ITEMS_PER_TICK` items at a time, and the
plot is redrawn at most :data:`PLOT_FPS` times per second, so a fast stream cannot make
the window unresponsive.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from serial_scope import __version__
from serial_scope.app import BAUD_RATES, MAX_TERMINAL_CHARS, AppController, UiUpdate
from serial_scope.commands import REPEAT_HZ
from serial_scope.decoders import available_decoders
from serial_scope.decoders.odometry import PARAM_NAMES
from serial_scope.plotmodel import (
    THEMES,
    PlotArea,
    Theme,
    Throttle,
    Viewport,
    channel_color,
    compute_viewport,
    cursor_readout,
    format_tick,
    format_value,
    nice_step,
    nice_ticks,
    polyline,
)
from serial_scope.ports import PortInfo, list_ports
from serial_scope.terminal import LINE_ENDINGS
from serial_scope.triggers import TriggerSpecError

POLL_MS = 30
MAX_ITEMS_PER_TICK = 2000
PLOT_FPS = 15
STATS_INTERVAL_S = 0.5
WINDOWS_S = ("2", "5", "10", "30", "60", "300")
SPEEDS = ("0.5", "1", "2", "4", "10", "max")


class MainWindow:
    def __init__(self, root: tk.Tk, controller: AppController, theme: str = "light") -> None:
        self.root = root
        self.ctl = controller
        self.theme: Theme = THEMES[theme]
        self.ports: dict[str, PortInfo] = {}
        self.visible: dict[str, tk.BooleanVar] = {}
        self.channel_widgets: dict[str, tk.Checkbutton] = {}
        self.paused_at: float | None = None
        self.fixed_range: tuple[float, float] | None = None
        self.last_viewport: Viewport | None = None
        self.cursor_x: float | None = None
        self.plot_throttle = Throttle(1.0 / PLOT_FPS)
        self.stats_throttle = Throttle(STATS_INTERVAL_S)
        self._after_id: str | None = None

        root.title(f"serial-scope {__version__}")
        root.minsize(900, 600)
        self.style = ttk.Style(root)
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self._build()
        self.apply_theme(theme)
        self.refresh_ports()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._schedule()

    # -- layout ------------------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        bar = ttk.Frame(root, padding=(8, 6))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Port").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(bar, textvariable=self.port_var, width=34)
        self.port_box.pack(side="left", padx=(4, 2))
        ttk.Button(bar, text="Refresh", command=self.refresh_ports, width=8).pack(side="left")
        ttk.Label(bar, text="Baud").pack(side="left", padx=(10, 0))
        self.baud_var = tk.StringVar(value="115200")
        ttk.Combobox(
            bar, textvariable=self.baud_var, values=[str(b) for b in BAUD_RATES], width=8
        ).pack(side="left", padx=4)
        ttk.Label(bar, text="Decode").pack(side="left", padx=(6, 0))
        self.decoder_var = tk.StringVar(value=self.ctl.decoder_name)
        decoder_box = ttk.Combobox(
            bar,
            textvariable=self.decoder_var,
            values=list(available_decoders()),
            width=9,
            state="readonly",
        )
        decoder_box.pack(side="left", padx=4)
        decoder_box.bind("<<ComboboxSelected>>", lambda _e: self.change_decoder())
        self.connect_btn = ttk.Button(bar, text="Connect", command=self.toggle_connect, width=11)
        self.connect_btn.pack(side="left", padx=(6, 12))
        self.record_btn = ttk.Button(bar, text="Record...", command=self.toggle_record, width=10)
        self.record_btn.pack(side="left")
        self.csv_btn = ttk.Button(bar, text="CSV...", command=self.toggle_csv, width=7)
        self.csv_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Replay...", command=self.open_replay, width=9).pack(
            side="left", padx=(8, 2)
        )
        self.speed_var = tk.StringVar(value="1")
        ttk.Combobox(
            bar, textvariable=self.speed_var, values=SPEEDS, width=4, state="readonly"
        ).pack(side="left")
        ttk.Label(bar, text="x").pack(side="left", padx=(2, 0))
        self.theme_btn = ttk.Button(bar, text="Dark", command=self.toggle_theme, width=6)
        self.theme_btn.pack(side="right")

        self._build_command_panel(root)

        panes = ttk.PanedWindow(root, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew", padx=8)

        plot_frame = ttk.Frame(panes)
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(plot_frame, height=320, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        side = ttk.Frame(plot_frame, padding=(8, 0, 0, 0))
        side.grid(row=0, column=1, sticky="ns")
        controls = ttk.Frame(side)
        controls.pack(fill="x")
        self.pause_btn = ttk.Button(controls, text="Pause", command=self.toggle_pause, width=8)
        self.pause_btn.grid(row=0, column=0, sticky="ew")
        self.autoscale_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls, text="Autoscale", variable=self.autoscale_var, command=self._autoscale
        ).grid(row=0, column=1, padx=(6, 0), sticky="w")
        ttk.Label(controls, text="Window s").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.window_var = tk.StringVar(value="10")
        ttk.Combobox(controls, textvariable=self.window_var, values=WINDOWS_S, width=5).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=(4, 0)
        )
        ttk.Button(controls, text="Mark", command=self.mark, width=8).grid(
            row=2, column=0, sticky="ew", pady=(4, 0)
        )
        ttk.Button(controls, text="Snapshot", command=self.snapshot, width=9).grid(
            row=2, column=1, sticky="ew", padx=(6, 0), pady=(4, 0)
        )
        ttk.Label(side, text="Channels").pack(anchor="w", pady=(10, 2))
        self.channel_frame = tk.Frame(side)
        self.channel_frame.pack(fill="both", expand=True)
        self.readout_var = tk.StringVar()
        self.readout = ttk.Label(side, textvariable=self.readout_var, justify="left", width=28)
        self.readout.pack(anchor="w", pady=(8, 0))
        panes.add(plot_frame, weight=3)

        notebook = ttk.Notebook(panes)
        panes.add(notebook, weight=2)

        term = ttk.Frame(notebook, padding=4)
        term.columnconfigure(0, weight=1)
        term.rowconfigure(0, weight=1)
        self.text = tk.Text(term, height=10, wrap="char", undo=False, font=("Consolas", 10))
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(term, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set, state="disabled")
        send_row = ttk.Frame(term)
        send_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        send_row.columnconfigure(0, weight=1)
        self.send_var = tk.StringVar()
        send_entry = ttk.Entry(send_row, textvariable=self.send_var)
        send_entry.grid(row=0, column=0, sticky="ew")
        send_entry.bind("<Return>", lambda _e: self.send())
        self.ending_var = tk.StringVar(value="LF")
        ttk.Combobox(
            send_row,
            textvariable=self.ending_var,
            values=list(LINE_ENDINGS),
            width=6,
            state="readonly",
        ).grid(row=0, column=1, padx=4)
        self.hex_input_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(send_row, text="Hex input", variable=self.hex_input_var).grid(
            row=0, column=2
        )
        ttk.Button(send_row, text="Send", command=self.send, width=7).grid(row=0, column=3, padx=4)
        self.hex_view_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            send_row, text="Hex view", variable=self.hex_view_var, command=self._hex_view
        ).grid(row=0, column=4, padx=(8, 0))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(send_row, text="Autoscroll", variable=self.autoscroll_var).grid(
            row=0, column=5, padx=4
        )
        ttk.Button(send_row, text="Clear", command=self.clear_terminal, width=6).grid(
            row=0, column=6
        )
        notebook.add(term, text="Terminal")

        stats = ttk.Frame(notebook, padding=4)
        stats.columnconfigure(0, weight=3)
        stats.columnconfigure(1, weight=2)
        stats.rowconfigure(0, weight=1)
        columns = ("last", "min", "max", "mean", "rate", "count")
        self.stats_tree = ttk.Treeview(stats, columns=columns, show="tree headings", height=8)
        self.stats_tree.heading("#0", text="channel")
        self.stats_tree.column("#0", width=150)
        titles = ("last", "min", "max", "mean", "rate Hz", "count")
        for col, title in zip(columns, titles, strict=True):
            self.stats_tree.heading(col, text=title)
            self.stats_tree.column(col, width=80, anchor="e")
        self.stats_tree.grid(row=0, column=0, sticky="nsew")
        self.counter_tree = ttk.Treeview(stats, columns=("value",), show="tree headings")
        self.counter_tree.heading("#0", text="counter")
        self.counter_tree.heading("value", text="value")
        self.counter_tree.column("#0", width=170)
        self.counter_tree.column("value", width=170, anchor="e")
        self.counter_tree.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        notebook.add(stats, text="Stats")

        events = ttk.Frame(notebook, padding=4)
        events.columnconfigure(0, weight=1)
        events.rowconfigure(0, weight=1)
        self.events_list = tk.Listbox(events, height=8, font=("Consolas", 9))
        self.events_list.grid(row=0, column=0, sticky="nsew")
        trig_row = ttk.Frame(events)
        trig_row.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        trig_row.columnconfigure(1, weight=1)
        ttk.Label(trig_row, text="Trigger").grid(row=0, column=0)
        self.trigger_var = tk.StringVar(value="speed > 160 -> mark,beep")
        trig_entry = ttk.Entry(trig_row, textvariable=self.trigger_var)
        trig_entry.grid(row=0, column=1, sticky="ew", padx=4)
        trig_entry.bind("<Return>", lambda _e: self.add_trigger())
        ttk.Button(trig_row, text="Add", command=self.add_trigger, width=6).grid(row=0, column=2)
        notebook.add(events, text="Errors & triggers")
        self.notebook = notebook

        self.status_var = tk.StringVar(value="not connected")
        status = ttk.Label(root, textvariable=self.status_var, anchor="w", padding=(8, 3))
        status.grid(row=3, column=0, sticky="ew")

    def _build_command_panel(self, root: tk.Tk) -> None:
        """Velocity and parameter commands for encoder-odometry-firmware.

        Shown only while the odometry decoder is in use (see :meth:`sync_command_panel`).
        """
        panel = ttk.Frame(root, padding=(8, 0, 8, 6))
        ttk.Label(panel, text="Odometry:").pack(side="left")
        self.vel_vars: list[tk.StringVar] = []
        for label in ("vx m/s", "vy m/s", "wz rad/s"):
            ttk.Label(panel, text=label).pack(side="left", padx=(8, 2))
            var = tk.StringVar(value="0")
            ttk.Entry(panel, textvariable=var, width=6).pack(side="left")
            self.vel_vars.append(var)
        ttk.Button(panel, text="Send vel", command=self.send_velocity, width=9).pack(
            side="left", padx=(8, 2)
        )
        self.repeat_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(panel, text=f"Repeat {REPEAT_HZ:g} Hz", variable=self.repeat_var).pack(
            side="left", padx=4
        )
        self.stop_btn = tk.Button(
            panel,
            text="STOP",
            command=self.stop_motors,
            width=7,
            font=("Segoe UI", 9, "bold"),
            foreground="white",
            background="#c0392b",
            activebackground="#e74c3c",
            activeforeground="white",
        )
        self.stop_btn.pack(side="left", padx=(4, 14))
        ttk.Label(panel, text="Param").pack(side="left")
        self.param_var = tk.StringVar(value="kp")
        ttk.Combobox(
            panel, textvariable=self.param_var, values=list(PARAM_NAMES.values()), width=18
        ).pack(side="left", padx=4)
        self.param_value_var = tk.StringVar()
        ttk.Entry(panel, textvariable=self.param_value_var, width=8).pack(side="left")
        ttk.Button(panel, text="Get", command=self.param_get, width=5).pack(
            side="left", padx=(4, 2)
        )
        ttk.Button(panel, text="Set", command=self.param_set, width=5).pack(side="left")
        self.reply_var = tk.StringVar(value="")
        ttk.Label(panel, textvariable=self.reply_var).pack(side="left", padx=(10, 0))
        self.command_panel = panel
        self.command_panel_shown = False

    def sync_command_panel(self) -> None:
        """Show the odometry command panel while the odometry decoder is in use."""
        want = self.ctl.command_target == "odometry"
        if want and not self.command_panel_shown:
            self.command_panel.grid(row=1, column=0, sticky="ew")
        elif not want and self.command_panel_shown:
            self.command_panel.grid_remove()
            self.ctl.repeat_velocity = None
        self.command_panel_shown = want

    # -- theme -------------------------------------------------------------------------

    def apply_theme(self, name: str) -> None:
        theme = self.theme = THEMES[name]
        s = self.style
        s.configure(".", background=theme.bg, foreground=theme.fg, fieldbackground=theme.text_bg)
        s.configure("TButton", padding=(6, 2))
        s.map("TButton", background=[("active", theme.select_bg)])
        s.configure(
            "Treeview",
            background=theme.text_bg,
            foreground=theme.text_fg,
            fieldbackground=theme.text_bg,
        )
        s.configure("Treeview.Heading", background=theme.bg, foreground=theme.fg)
        s.configure("TNotebook", background=theme.bg)
        s.configure("TNotebook.Tab", background=theme.bg, foreground=theme.fg, padding=(10, 3))
        s.map("TNotebook.Tab", background=[("selected", theme.text_bg)])
        s.configure(
            "TCombobox",
            fieldbackground=theme.text_bg,
            foreground=theme.text_fg,
            background=theme.bg,
            arrowcolor=theme.fg,
        )
        s.configure(
            "TScrollbar", background=theme.bg, troughcolor=theme.text_bg, arrowcolor=theme.fg
        )
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", theme.text_bg)],
            foreground=[("readonly", theme.text_fg)],
        )
        s.configure("TEntry", fieldbackground=theme.text_bg, foreground=theme.text_fg)
        self.root.configure(background=theme.bg)
        self.canvas.configure(background=theme.plot_bg)
        self.text.configure(
            background=theme.text_bg,
            foreground=theme.text_fg,
            insertbackground=theme.text_fg,
            selectbackground=theme.select_bg,
        )
        self.text.tag_configure("tx", foreground=theme.palette[2])
        self.text.tag_configure("info", foreground=theme.muted)
        self.events_list.configure(background=theme.text_bg, foreground=theme.text_fg)
        self.channel_frame.configure(background=theme.bg)
        for index, widget in enumerate(self.channel_widgets.values()):
            widget.configure(
                background=theme.bg,
                foreground=channel_color(theme, index),
                activebackground=theme.bg,
                selectcolor=theme.text_bg,
            )
        self.theme_btn.configure(text="Light" if name == "dark" else "Dark")
        self.draw_plot()

    def toggle_theme(self) -> None:
        self.apply_theme("light" if self.theme.name == "dark" else "dark")

    def change_decoder(self) -> None:
        """Switch decoder; channels and counters start again from the next bytes."""
        self.ctl.set_decoder(self.decoder_var.get())
        self._clear_view()
        self.sync_command_panel()

    # -- odometry commands -------------------------------------------------------------

    def _command_done(self, text: str) -> None:
        self._status(f"sent: {text}")
        self.events_list.insert("end", f"sent  {text}")

    def send_velocity(self) -> None:
        try:
            vx, vy, wz = (float(v.get()) for v in self.vel_vars)
            command = self.ctl.send_velocity(vx, vy, wz, repeat=self.repeat_var.get())
        except ValueError as exc:
            self._status(f"not sent: {exc}")
            return
        suffix = f" (repeating at {REPEAT_HZ:g} Hz)" if self.repeat_var.get() else ""
        self._command_done(command.text + suffix)

    def stop_motors(self) -> None:
        self.repeat_var.set(False)
        try:
            command = self.ctl.stop_motors()
        except ValueError as exc:
            self._status(f"stop not sent: {exc}")
            return
        self._command_done(command.text)

    def param_get(self) -> None:
        try:
            command = self.ctl.send_param_get(self.param_var.get())
        except ValueError as exc:
            self._status(f"not sent: {exc}")
            return
        self.reply_var.set("waiting for reply...")
        self._command_done(command.text)

    def param_set(self) -> None:
        try:
            value = float(self.param_value_var.get())
            command = self.ctl.send_param_set(self.param_var.get(), value)
        except ValueError as exc:
            self._status(f"not sent: {exc}")
            return
        self.reply_var.set("waiting for reply...")
        self._command_done(command.text)

    # -- connection --------------------------------------------------------------------

    def refresh_ports(self) -> None:
        try:
            ports = list_ports()
        except Exception as exc:  # pragma: no cover - platform specific
            self._status(f"cannot list ports: {exc}")
            ports = []
        self.ports = {p.label(): p for p in ports}
        self.port_box.configure(values=list(self.ports))
        if not self.port_var.get() and self.ports:
            self.port_var.set(next(iter(self.ports)))

    def toggle_connect(self) -> None:
        if self.ctl.running:
            self.ctl.disconnect()
            self.connect_btn.configure(text="Connect")
            self._status("disconnected")
            return
        choice = self.port_var.get().strip()
        if not choice:
            messagebox.showinfo("serial-scope", "Choose a port, or type tcp://host:port")
            return
        try:
            if choice.startswith("tcp://"):
                host, _, port = choice[len("tcp://") :].rpartition(":")
                self.ctl.connect_tcp(host, int(port))
            else:
                device = self.ports[choice].device if choice in self.ports else choice
                self.ctl.connect_serial(device, int(self.baud_var.get()))
        except ValueError as exc:
            messagebox.showerror("serial-scope", str(exc))
            return
        self.connect_btn.configure(text="Disconnect")

    def open_replay(self) -> None:
        path = filedialog.askopenfilename(
            title="Replay log", filetypes=[("serial-scope logs", "*.sslog"), ("All files", "*")]
        )
        if path:
            self.start_replay(path)

    def start_replay(self, path: str | Path) -> None:
        speed_text = self.speed_var.get()
        speed = 0.0 if speed_text == "max" else float(speed_text)
        self._clear_view()
        try:
            self.ctl.open_replay(path, speed)
        except (OSError, ValueError) as exc:
            messagebox.showerror("serial-scope", f"cannot replay {path}: {exc}")
            return
        self.connect_btn.configure(text="Stop")

    def toggle_record(self) -> None:
        if self.ctl.recording:
            self.ctl.stop_recording()
            self.record_btn.configure(text="Record...")
            return
        path = filedialog.asksaveasfilename(
            title="Record raw log",
            defaultextension=".sslog",
            filetypes=[("serial-scope logs", "*.sslog")],
        )
        if path:
            self.ctl.start_recording(path)
            self.record_btn.configure(text="Stop rec")

    def toggle_csv(self) -> None:
        if self.ctl.session.csv is not None:
            self.ctl.session.stop_csv()
            self.csv_btn.configure(text="CSV...")
            return
        path = filedialog.asksaveasfilename(
            title="Export decoded samples",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if path:
            self.ctl.session.start_csv(path)
            self.csv_btn.configure(text="Stop CSV")

    # -- terminal ----------------------------------------------------------------------

    def send(self) -> None:
        try:
            self.ctl.send(self.send_var.get(), self.ending_var.get(), self.hex_input_var.get())
        except ValueError as exc:
            self._status(f"not sent: {exc}")
            return
        self._append_terminal([(f"> {self.send_var.get()}\n", "tx")])
        self.send_var.set("")

    def _hex_view(self) -> None:
        self.ctl.hex_mode = self.hex_view_var.get()

    def clear_terminal(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _append_terminal(self, pieces: list[tuple[str, str]]) -> None:
        if not pieces:
            return
        self.text.configure(state="normal")
        for text, tag in pieces:
            self.text.insert("end", text, tag)
        counted = self.text.count("1.0", "end", "chars")
        size = int(counted[0]) if counted else 0
        if size > MAX_TERMINAL_CHARS:
            self.text.delete("1.0", f"1.0 + {size - MAX_TERMINAL_CHARS} chars")
        self.text.configure(state="disabled")
        if self.autoscroll_var.get():
            self.text.see("end")

    # -- plot --------------------------------------------------------------------------

    def _add_channel(self, name: str) -> None:
        index = len(self.channel_widgets)
        var = tk.BooleanVar(value=True)
        self.visible[name] = var
        widget = tk.Checkbutton(
            self.channel_frame,
            text=name,
            variable=var,
            anchor="w",
            command=self.draw_plot,
            background=self.theme.bg,
            foreground=channel_color(self.theme, index),
            activebackground=self.theme.bg,
            selectcolor=self.theme.text_bg,
            font=("Segoe UI", 9, "bold"),
        )
        widget.pack(anchor="w", fill="x")
        self.channel_widgets[name] = widget

    def _clear_view(self) -> None:
        for widget in self.channel_widgets.values():
            widget.destroy()
        self.channel_widgets.clear()
        self.visible.clear()
        self.events_list.delete(0, "end")
        self.clear_terminal()
        self.paused_at = None
        self.fixed_range = None
        self.pause_btn.configure(text="Pause")
        self.draw_plot()

    def toggle_pause(self) -> None:
        if self.paused_at is None:
            self.paused_at = self.ctl.session.channels.latest_time()
            self.pause_btn.configure(text="Resume")
        else:
            self.paused_at = None
            self.pause_btn.configure(text="Pause")
        self.draw_plot()

    def _autoscale(self) -> None:
        if not self.autoscale_var.get() and self.last_viewport is not None:
            self.fixed_range = (self.last_viewport.v0, self.last_viewport.v1)
        else:
            self.fixed_range = None
        self.draw_plot()

    def _window_s(self) -> float:
        try:
            return max(0.1, float(self.window_var.get()))
        except ValueError:
            return 10.0

    def draw_plot(self) -> None:
        canvas = self.canvas
        theme = self.theme
        canvas.delete("all")
        area = PlotArea(max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 120))
        channels = self.ctl.session.channels
        visible = [n for n, v in self.visible.items() if v.get()]
        vp = compute_viewport(channels, visible, self._window_s(), self.paused_at, self.fixed_range)
        canvas.create_rectangle(area.x0, area.y0, area.x1, area.y1, outline=theme.axis)
        if vp is None:
            canvas.create_text(
                (area.x0 + area.x1) / 2,
                (area.y0 + area.y1) / 2,
                fill=theme.muted,
                text="No numeric channels yet. Connect a port or open a replay.",
            )
            self.last_viewport = None
            return
        self.last_viewport = vp
        step = nice_step(vp.v1 - vp.v0, 6)
        for v in nice_ticks(vp.v0, vp.v1, 6):
            y = vp.y(v, area)
            canvas.create_line(area.x0, y, area.x1, y, fill=theme.grid)
            canvas.create_text(
                area.x0 - 6,
                y,
                text=format_tick(v, step),
                anchor="e",
                fill=theme.muted,
                font=("Segoe UI", 8),
            )
        rel0 = vp.t0 - vp.t1
        tstep = nice_step(-rel0, 8)
        for rel in nice_ticks(rel0, 0.0, 8):
            x = vp.x(vp.t1 + rel, area)
            canvas.create_line(x, area.y0, x, area.y1, fill=theme.grid)
            canvas.create_text(
                x,
                area.y1 + 4,
                text=f"{format_tick(rel, tstep)} s",
                anchor="n",
                fill=theme.muted,
                font=("Segoe UI", 8),
            )
        for marker in self.ctl.session.markers:
            if vp.t0 <= marker.t <= vp.t1:
                x = vp.x(marker.t, area)
                canvas.create_line(x, area.y0, x, area.y1, fill=theme.marker, dash=(4, 3))
        names = list(self.visible)
        for name in visible:
            channel = channels[name]
            coords = polyline(channel.buffer.times(), channel.buffer.values(), vp, area)
            color = channel_color(theme, names.index(name))
            if len(coords) >= 4:
                canvas.create_line(*coords, fill=color, width=1.5)
            elif len(coords) == 2:
                x, y = coords
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color)
        if self.paused_at is not None:
            canvas.create_text(
                area.x1 - 6,
                area.y0 + 4,
                text="PAUSED",
                anchor="ne",
                fill=theme.error,
                font=("Segoe UI", 9, "bold"),
            )
        self._draw_cursor(vp, area, visible)

    def _draw_cursor(self, vp: Viewport, area: PlotArea, visible: list[str]) -> None:
        if self.cursor_x is None or not area.x0 <= self.cursor_x <= area.x1:
            self.readout_var.set("")
            return
        t = vp.t_at(self.cursor_x, area)
        self.canvas.create_line(
            self.cursor_x, area.y0, self.cursor_x, area.y1, fill=self.theme.fg, dash=(2, 2)
        )
        lines = [f"t = {t - vp.t1:+.3f} s"]
        for r in cursor_readout(self.ctl.session.channels, visible, t):
            lines.append(f"{r.name} = {format_value(r.value)}")
        self.readout_var.set("\n".join(lines))

    def _on_motion(self, event: tk.Event) -> None:
        self.cursor_x = float(event.x)
        if self.paused_at is not None or not self.ctl.running:
            self.draw_plot()

    def _on_leave(self, _event: tk.Event) -> None:
        self.cursor_x = None
        self.readout_var.set("")

    # -- actions -----------------------------------------------------------------------

    def mark(self) -> None:
        self.ctl.mark("user mark")
        self.draw_plot()

    def snapshot(self) -> None:
        path = self.ctl.snapshot()
        self._status(f"snapshot written to {path}")

    def add_trigger(self) -> None:
        try:
            label = self.ctl.add_trigger(self.trigger_var.get())
        except TriggerSpecError as exc:
            messagebox.showerror("serial-scope", str(exc))
            return
        self.events_list.insert("end", f"trigger added: {label}")

    # -- timer -------------------------------------------------------------------------

    def _schedule(self) -> None:
        self._after_id = self.root.after(POLL_MS, self.tick)

    def tick(self) -> None:
        update = self.ctl.poll(MAX_ITEMS_PER_TICK)
        self.apply_update(update)
        if (
            self.paused_at is None
            and (update.chunks or update.new_channels)
            and self.plot_throttle.ready()
        ):
            self.draw_plot()
        if self.stats_throttle.ready():
            self.refresh_tables()
            self.sync_command_panel()
        self._schedule()

    def apply_update(self, update: UiUpdate) -> None:
        self._append_terminal(update.terminal)
        for name in update.new_channels:
            self._add_channel(name)
        for line in update.events:
            self.events_list.insert("end", line)
            if " error " in line:
                color = self.theme.error
            elif " notice " in line:
                color = self.theme.palette[1]
            else:
                color = self.theme.marker
            self.events_list.itemconfigure("end", foreground=color)
        if update.events:
            self.events_list.see("end")
        if update.replies:
            self.reply_var.set(update.replies[-1])
        if update.repeat_stopped:
            self.repeat_var.set(False)
        for _ in range(min(update.beeps, 3)):
            self.root.bell()
        if update.status:
            self._status(update.status[-1])
        if update.finished:
            self.connect_btn.configure(text="Connect")
        if update.connected is not None and not update.finished and self.ctl.running:
            self.connect_btn.configure(text="Stop" if self.ctl.is_replay else "Disconnect")

    def refresh_tables(self) -> None:
        tree = self.stats_tree
        rows = self.ctl.stats_rows()
        existing = set(tree.get_children())
        for row in rows:
            iid = row[0]
            if iid in existing:
                tree.item(iid, values=row[1:])
            else:
                tree.insert("", "end", iid=iid, text=row[0], values=row[1:])
        counters = self.counter_tree
        counters.delete(*counters.get_children())
        for key, value in self.ctl.counter_rows():
            counters.insert("", "end", text=key, values=(value,))
        s = self.ctl.session
        errors = sum(s.totals.errors.values())
        state = (
            "replay"
            if self.ctl.is_replay
            else ("connected" if self.ctl.connected else "not connected")
        )
        rec = "  REC" if self.ctl.recording else ""
        self.status_var.set(
            f"{state}  |  {s.decoder_name}  |  rx {s.totals.rx_bytes} B  |  "
            f"{s.totals.samples} samples  |  {errors} errors{rec}"
        )

    def _status(self, text: str) -> None:
        self.status_var.set(text)

    def close(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.ctl.close()
        self.root.destroy()


def _enable_dpi_awareness() -> None:  # pragma: no cover - Windows only
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def run_gui(
    replay: str | None = None,
    speed: float = 1.0,
    port: str | None = None,
    baud: int = 115200,
    decoder: str = "auto",
    theme: str = "light",
    buffer_len: int = 5000,
    geometry: str = "1280x820",
) -> int:
    _enable_dpi_awareness()
    root = tk.Tk()
    root.geometry(geometry)
    controller = AppController(decoder=decoder, buffer_len=buffer_len)
    window = MainWindow(root, controller, theme)
    window.speed_var.set("max" if speed == 0 else f"{speed:g}")
    window.baud_var.set(str(baud))
    if replay:
        root.after(200, lambda: window.start_replay(replay))
    elif port:
        window.port_var.set(port)
        root.after(200, window.toggle_connect)
    root.mainloop()
    return 0
