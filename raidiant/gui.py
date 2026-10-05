"""Portable Tk desktop interface for the RAIDiant file-backed array engine.

All storage work runs off the Tk thread. Workers communicate through a queue;
they never read Tk variables or update widgets. The array owns all format,
integrity, recovery, and host-filename rules.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from .engine import Array, RaidError, inspect_member
from .host import capacity_report, durable_flush, sync_directory
from .branding import ASSETS, apply_window_icon, load_image, prepare_desktop


APP_NAME = "RAIDiant"
PAGE_SIZE = 500
BG = "#f6f5fc"
INK = "#241d3d"
MUTED = "#716984"
ACCENT = "#7650d4"
CYAN = "#24b7ca"
LINE = "#e8e2f3"


def _settings_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "RAIDiant" / "desktop.json"


def _load_settings() -> dict:
    try:
        value = json.loads(_settings_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(settings: dict) -> bool:
    path = _settings_path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="desktop-", suffix=".tmp",
                                         dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(settings, file, indent=2)
            durable_flush(file)
        os.replace(temporary, path)
        sync_directory(path.parent)
        return True
    except OSError:
        # Preference persistence cannot interfere with a storage operation.
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _size(value: int | float) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(number) < 1024 or unit == "PiB":
            return f"{number:,.1f} {unit}" if unit != "B" else f"{number:,.0f} B"
        number /= 1024
    return str(value)


def _initial_member_reservation(member_size: int) -> int:
    """The format's two metadata banks and header copies are reserved upfront."""
    bank_size = max(65536, min(512 * 1024**2, member_size // 32 // 4096 * 4096))
    return 8192 + 2 * bank_size


def _save_filename(*, parent: tk.Misc, **options) -> str:
    """Use native save panels without Tk's oversized macOS Format accessory."""
    if parent.tk.call("tk", "windowingsystem") == "aqua":
        # Tk 8.6's save filter popup extends beyond its accessory view in the
        # compact NSSavePanel. These files have one format; defaultextension
        # still proposes that extension without adding the broken Format row.
        options.pop("filetypes", None)
    return filedialog.asksaveasfilename(parent=parent, **options)


def _date(value: Any) -> str:
    if not value:
        return "—"
    try:
        if isinstance(value, (float, int)):
            return _dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
        return str(value).replace("T", " ")[:19]
    except (ValueError, OverflowError, OSError):
        return str(value)


def _fit_window(window: tk.Misc, width: int, height: int, minimum: tuple[int, int]) -> None:
    """Keep application controls on screen, including on scaled laptop displays."""
    available_width = max(320, window.winfo_screenwidth() - 48)
    available_height = max(240, window.winfo_screenheight() - 100)
    window.geometry(f"{min(width, available_width)}x{min(height, available_height)}")
    window.minsize(min(minimum[0], available_width), min(minimum[1], available_height))


class _ScrollArea(ttk.Frame):
    """A normally borderless page with scrolling when larger text needs room."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0)
        self.vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.body = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self._sync_pending: str | None = None
        self.canvas.bind("<Configure>", self._schedule_sync)
        self.body.bind("<Configure>", self._schedule_sync)
        # Toplevel bindings receive events from their children, without leaving
        # global mouse-wheel bindings behind when a dialog is destroyed.
        self.top = self.winfo_toplevel()
        self._wheel_bindings = [(event, self.top.bind(event, self._wheel, add="+"))
                                for event in ("<MouseWheel>", "<Button-4>", "<Button-5>")]
        self.bind("<Destroy>", self._destroyed, add="+")

    def _schedule_sync(self, _event=None) -> None:
        if self._sync_pending is None:
            self._sync_pending = self.after_idle(self._sync)

    def _sync(self) -> None:
        self._sync_pending = None
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        needed_width, needed_height = self.body.winfo_reqwidth(), self.body.winfo_reqheight()
        self.canvas.itemconfigure(self.window, width=max(width, needed_width), height=max(height, needed_height))
        self.canvas.configure(scrollregion=(0, 0, max(width, needed_width), max(height, needed_height)))
        if needed_height > height + 1:
            self.vertical.grid(row=0, column=1, sticky="ns")
        else:
            self.vertical.grid_remove()
            self.canvas.yview_moveto(0)
        if needed_width > width + 1:
            self.horizontal.grid(row=1, column=0, sticky="ew")
        else:
            self.horizontal.grid_remove()
            self.canvas.xview_moveto(0)

    def _wheel(self, event) -> None:
        widget = event.widget
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        if widget is not self or not self.vertical.winfo_ismapped():
            return
        # Native lists/text retain their own scroll behavior.
        if isinstance(event.widget, (ttk.Treeview, tk.Text, tk.Listbox)):
            return
        direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
        self.canvas.yview_scroll(direction * 3, "units")

    def _destroyed(self, event) -> None:
        if event.widget is not self:
            return
        if self._sync_pending is not None:
            self.after_cancel(self._sync_pending)
            self._sync_pending = None
        for sequence, identifier in self._wheel_bindings:
            try:
                self.top.unbind(sequence, identifier)
            except tk.TclError:
                pass


class _Dialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, geometry: str):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.preferred_size = tuple(int(value) for value in geometry.split("x"))
        _fit_window(self, *self.preferred_size, (650, 440))
        self.configure(background=BG)
        self.transient(parent.winfo_toplevel())
        self.result: Any = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.scroll_area = _ScrollArea(self)
        self.scroll_area.pack(fill="both", expand=True)
        self.body = self.scroll_area.body

    def show(self) -> Any:
        self.update_idletasks()
        width = max(self.preferred_size[0], self.body.winfo_reqwidth() + 24)
        height = max(self.preferred_size[1], self.body.winfo_reqheight() + 24)
        _fit_window(self, width, height, self.minsize())
        self.deiconify()
        self.grab_set()
        self.wait_window()
        return self.result


class _CreateDialog(_Dialog):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent, "Create an array · RAIDiant", "850x720")
        self.n = tk.IntVar(master=self, value=5)
        self.parity = tk.IntVar(master=self, value=2)
        self.path_vars: list[tk.StringVar] = []
        self.recommended = 0
        self.checked_paths: tuple[str, ...] = ()
        self.size_value = tk.StringVar(master=self, value="")
        self.size_unit = tk.StringVar(master=self, value="GiB")
        self.capacity_text = tk.StringVar(master=self, value="Choose every member location, then calculate the recommended maximum.")
        self.capacity_events: queue.Queue[tuple] = queue.Queue()
        self.calculating = False
        self.submit_after_calculation = False
        self.capacity_after: str | None = None

        outer = ttk.Frame(self.body, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Create your array", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Choose member locations and the number of simultaneous member failures to tolerate.",
                  style="Muted.TLabel", wraplength=780).pack(anchor="w", pady=(6, 18))
        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        ttk.Label(controls, text="Total members").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(controls, from_=3, to=32, textvariable=self.n, width=7).grid(row=1, column=0, sticky="w", pady=5)
        ttk.Label(controls, text="Failures tolerated").grid(row=0, column=1, sticky="w", padx=(24, 0))
        ttk.Spinbox(controls, from_=1, to=30, textvariable=self.parity, width=7).grid(row=1, column=1, sticky="w", padx=(24, 0), pady=5)
        ttk.Button(controls, text="Update member list", command=self._rebuild_rows).grid(row=1, column=2, padx=24)
        ttk.Label(outer, text="Every member stores distributed data and redundancy. Separate physical drives provide independent protection.",
                  style="Muted.TLabel", wraplength=780).pack(anchor="w", pady=(8, 12))

        scroll_area = ttk.Frame(outer)
        scroll_area.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(scroll_area, height=160, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.rows = ttk.Frame(self.canvas)
        self.row_window = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.row_window, width=e.width))
        self._rebuild_rows()

        ttk.Separator(outer).pack(fill="x", pady=14)
        capacity = ttk.Frame(outer)
        capacity.pack(fill="x")
        self.calculate_button = ttk.Button(capacity, text="Calculate recommended capacity", command=self._calculate)
        self.calculate_button.grid(row=0, column=0, sticky="w")
        ttk.Label(capacity, text="Maximum per member").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(capacity, textvariable=self.size_value, width=15).grid(row=1, column=1, padx=10, pady=(12, 0))
        ttk.Combobox(capacity, textvariable=self.size_unit, values=("GiB", "MiB"), state="readonly", width=7).grid(row=1, column=2, pady=(12, 0))
        ttk.Label(outer, textvariable=self.capacity_text, style="Muted.TLabel", wraplength=780).pack(anchor="w", pady=(10, 14))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        self.create_button = ttk.Button(buttons, text="Create array", style="Accent.TButton", command=self._submit)
        self.create_button.pack(side="right", padx=8)

    def _configuration(self) -> tuple[int, int] | None:
        try:
            n, parity = self.n.get(), self.parity.get()
        except tk.TclError:
            messagebox.showerror("Invalid configuration", "Enter whole numbers for members and tolerated failures.", parent=self)
            return None
        if not 3 <= n <= 32 or not 1 <= parity <= n - 2:
            messagebox.showerror("Invalid configuration", "Choose 3–32 total members and at least one tolerated failure. At least two data members are required.", parent=self)
            return None
        return n, parity

    def _rebuild_rows(self) -> None:
        config = self._configuration()
        if config is None:
            return
        n, _ = config
        existing = [value.get() for value in self.path_vars]
        for widget in self.rows.winfo_children():
            widget.destroy()
        self.path_vars = []
        self.rows.columnconfigure(1, weight=1)
        for index in range(n):
            value = tk.StringVar(master=self, value=existing[index] if index < len(existing) else "")
            self.path_vars.append(value)
            ttk.Label(self.rows, text=f"Member {index + 1:02d}").grid(row=index, column=0, sticky="w", padx=(0, 12), pady=6)
            ttk.Entry(self.rows, textvariable=value).grid(row=index, column=1, sticky="ew", pady=6)
            ttk.Button(self.rows, text="Choose…", command=lambda i=index: self._choose(i)).grid(row=index, column=2, padx=(10, 4), pady=6)
        self.checked_paths = ()
        self.capacity_text.set("Choose every member location, then calculate the recommended maximum.")

    def _choose(self, index: int) -> None:
        filename = _save_filename(parent=self, title=f"Save member {index + 1}",
                                              defaultextension=".r5m", initialfile=f"array-member-{index + 1:02d}.r5m",
                                              filetypes=[("RAIDiant member", "*.r5m"), ("All files", "*")])
        if filename:
            self.path_vars[index].set(filename)
            self.checked_paths = ()
            self.capacity_text.set("Member locations changed. Recalculate capacity before creation.")

    def _paths(self) -> list[str] | None:
        config = self._configuration()
        if config is None:
            return None
        if len(self.path_vars) != config[0]:
            self._rebuild_rows()
            messagebox.showinfo("Member list updated", "Choose a location for each member before continuing.", parent=self)
            return None
        paths = [value.get().strip() for value in self.path_vars]
        if not all(paths):
            messagebox.showerror("Locations needed", "Choose a location for every member.", parent=self)
            return None
        if len({str(Path(p).resolve()) for p in paths}) != len(paths):
            messagebox.showerror("Duplicate location", "Each member needs its own file path.", parent=self)
            return None
        return paths

    def _calculate(self, *, submit_after: bool = False) -> None:
        if self.calculating:
            self.submit_after_calculation |= submit_after
            return
        paths = self._paths()
        if paths is None:
            return
        self.calculating = True
        self.submit_after_calculation = submit_after
        self.calculate_button.configure(state="disabled")
        self.create_button.configure(state="disabled")
        self.capacity_text.set("Inspecting target volumes and available capacity…")

        def worker() -> None:
            try:
                self.capacity_events.put(("done", capacity_report(paths), tuple(paths)))
            except Exception as exc:
                self.capacity_events.put(("error", exc, tuple(paths)))

        threading.Thread(target=worker, name="RAIDiant-capacity", daemon=True).start()
        self.capacity_after = self.after(100, self._capacity_poll)

    def _capacity_poll(self) -> None:
        try:
            event, result, paths = self.capacity_events.get_nowait()
        except queue.Empty:
            self.capacity_after = self.after(100, self._capacity_poll)
            return
        self.capacity_after = None
        self.calculating = False
        submit_after = self.submit_after_calculation
        self.submit_after_calculation = False
        self.calculate_button.configure(state="normal")
        self.create_button.configure(state="normal")
        if paths != tuple(value.get().strip() for value in self.path_vars):
            self.capacity_text.set("Member locations changed. Recalculate capacity before creation.")
            return
        if event == "error":
            self.capacity_text.set("Capacity could not be calculated. Check the target locations.")
            messagebox.showerror("Capacity could not be calculated", str(result), parent=self)
            return
        try:
            report = result
            self.recommended = int(report["recommended_size"])
            self.checked_paths = tuple(paths)
            # A recommendation fills an empty field only. In particular, a
            # capacity check triggered by Create must preserve the user's exact
            # amount and unit, including edits made while the worker ran.
            if not self.size_value.get().strip():
                unit = "GiB" if self.recommended >= 1024**3 else "MiB"
                divisor = 1024**3 if unit == "GiB" else 1024**2
                self.size_unit.set(unit)
                number = Decimal(self.recommended) / Decimal(divisor)
                self.size_value.set(str(number.quantize(Decimal("0.001"), rounding="ROUND_DOWN")))
            warning = "\n".join(str(w) for w in report.get("warnings", []))
            self.capacity_text.set(f"Recommended maximum: {_size(self.recommended)} per member, allowing room for the host.\n"
                                   "Only recovery and metadata space is reserved initially. "
                                   "Data files grow as you upload; the maximum capacity is not reserved."
                                   + (f"\n{warning}" if warning else ""))
        except Exception as exc:
            messagebox.showerror("Capacity could not be calculated", str(exc), parent=self)
            return
        if submit_after:
            self._submit()

    def destroy(self) -> None:
        if getattr(self, "capacity_after", None) is not None:
            self.after_cancel(self.capacity_after)
            self.capacity_after = None
        super().destroy()

    def _submit(self) -> None:
        config = self._configuration()
        paths = self._paths()
        if config is None or paths is None:
            return
        if self.checked_paths != tuple(paths):
            self._calculate(submit_after=True)
            return
        try:
            multiplier = 1024**3 if self.size_unit.get() == "GiB" else 1024**2
            size = int(Decimal(self.size_value.get()) * multiplier)
            if size <= 0:
                raise ValueError
        except (InvalidOperation, ValueError, OverflowError):
            messagebox.showerror("Invalid capacity", "Enter a positive size per member.", parent=self)
            return
        if size > self.recommended:
            messagebox.showerror("Insufficient safe capacity", "The requested member size exceeds the available safe capacity. Choose a smaller size or different locations.", parent=self)
            return
        existing = [p for p in paths if Path(p).exists()]
        if existing:
            messagebox.showerror("File already exists", "Choose new filenames. Creation will not overwrite existing files.\n\n" + "\n".join(existing[:5]), parent=self)
            return
        n, parity = config
        text = (f"Create {n} members with a maximum of {_size(size)} each?\n\n"
                f"This array tolerates {parity} simultaneous member failures. "
                f"Approximate usable capacity: {_size((n - parity) * size)}, before format overhead.\n\n"
                f"Initially reserves {_size(_initial_member_reservation(size))} per member for recovery and metadata. "
                "Member files then grow as you upload. "
                "If a destination fills up, the unfinished file is rolled back. Previously completed files remain safe.")
        if messagebox.askyesno("Confirm array creation", text, parent=self):
            self.result = (paths, parity, size)
            self.destroy()


class _OpenDialog(_Dialog):
    def __init__(self, parent: tk.Misc, first_path: str, info: dict[str, Any]):
        super().__init__(parent, "Choose array members · RAIDiant", "880x640")
        self.info = info
        self.n = int(info["n"])
        self.parity = int(info["parity"])
        self.paths: dict[int, str] = {int(info["index"]): first_path}
        self.readonly = tk.BooleanVar(master=self, value=False)
        self.summary = tk.StringVar(master=self)
        outer = ttk.Frame(self.body, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Find the array's members", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text=f"{self.n} members · {self.parity} failures tolerated · {_size(info['member_size'])} per member",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 8))
        ttk.Label(outer, text="Select available member files. Missing members contain both data and redundancy; any replacement must be rebuilt.",
                  style="Muted.TLabel", wraplength=810).pack(anchor="w", pady=(0, 16))
        container = ttk.Frame(outer)
        container.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(container, height=4, columns=("member", "state", "path"), show="headings", selectmode="browse")
        for column, name, width in (("member", "Member", 80), ("state", "Status", 110), ("path", "Location", 560)):
            self.tree.heading(column, text=name)
            self.tree.column(column, width=width, stretch=column == "path")
        scrollbar = ttk.Scrollbar(container, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._choose_slot())
        row = ttk.Frame(outer)
        row.pack(fill="x", pady=12)
        ttk.Button(row, text="Add member files…", command=self._add).pack(side="left")
        ttk.Button(row, text="Choose selected slot…", command=self._choose_slot).pack(side="left", padx=8)
        ttk.Button(row, text="Remove selected", command=self._remove).pack(side="left")
        ttk.Label(outer, textvariable=self.summary, wraplength=810).pack(anchor="w", pady=(4, 8))
        ttk.Checkbutton(outer, text="Open read-only even when all members are available", variable=self.readonly).pack(anchor="w", pady=(0, 12))
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        ttk.Button(footer, text="Cancel", command=self.destroy).pack(side="right")
        self.open_button = ttk.Button(footer, text="Open array", style="Accent.TButton", command=self._submit)
        self.open_button.pack(side="right", padx=8)
        self._refresh()

    def _refresh(self) -> None:
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for index in range(self.n):
            self.tree.insert("", "end", iid=str(index), values=(f"{index + 1:02d}", "Selected" if index in self.paths else "Missing", self.paths.get(index, "—")))
        if selected:
            self.tree.selection_set(selected)
        count = len(self.paths)
        needed = self.n - self.parity
        if count < needed:
            self.summary.set(f"{count} of {self.n} members selected. At least {needed} distinct valid members are required.")
            self.open_button.configure(state="disabled")
        elif count < self.n:
            self.summary.set(f"{count} of {self.n} selected. The array will open read-only. Choose Rebuild missing in the file manager to create replacements.")
            self.open_button.configure(state="normal")
        else:
            self.summary.set(f"All {self.n} members selected. The engine will verify membership and recovery state before enabling writes.")
            self.open_button.configure(state="normal")

    def _accept(self, path: str, expected: int | None = None) -> None:
        member = inspect_member(path)
        if member.get("state") == "rebuilding":
            raise ValueError("This replacement is incomplete. Open the surviving members, then choose Rebuild missing to resume this file.")
        if member["uuid"] != self.info["uuid"] or int(member["n"]) != self.n or int(member["parity"]) != self.parity:
            raise ValueError("This file belongs to a different array or incompatible configuration.")
        index = int(member["index"])
        if not 0 <= index < self.n:
            raise ValueError("The member index is outside this array's configuration.")
        if expected is not None and index != expected:
            raise ValueError(f"This is member {index + 1}, but you selected slot {expected + 1}. Use Add member files to assign it automatically.")
        previous = self.paths.get(index)
        if previous and str(Path(previous).resolve()) != str(Path(path).resolve()):
            if not messagebox.askyesno("Member already selected", f"Replace the selected file for member {index + 1}?\n\nThe engine will still check its generation when opening.", parent=self):
                return
        self.paths[index] = path

    def _add(self) -> None:
        paths = filedialog.askopenfilenames(parent=self, title="Select array members", filetypes=[("RAIDiant members", "*.r5m"), ("All files", "*")])
        failures = []
        for path in paths:
            try:
                self._accept(path)
            except Exception as exc:
                failures.append(f"{Path(path).name}: {exc}")
        self._refresh()
        if failures:
            messagebox.showerror("Some members could not be added", "\n\n".join(failures[:8]), parent=self)

    def _choose_slot(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = filedialog.askopenfilename(parent=self, title=f"Choose member {int(selection[0]) + 1}", filetypes=[("RAIDiant member", "*.r5m"), ("All files", "*")])
        if path:
            try:
                self._accept(path, int(selection[0]))
            except Exception as exc:
                messagebox.showerror("Member could not be added", str(exc), parent=self)
            self._refresh()

    def _remove(self) -> None:
        for selected in self.tree.selection():
            self.paths.pop(int(selected), None)
        self._refresh()

    def _submit(self) -> None:
        if len(self.paths) >= self.n - self.parity:
            # Degraded arrays are enforced read-only by the storage engine, so
            # rebuilding can restore writes without retaining a user read-only flag.
            self.result = ([self.paths[i] for i in sorted(self.paths)], self.readonly.get())
            self.destroy()


class RaidApp:
    """The single-window application; public for launchers and UI smoke checks."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        _fit_window(self.root, 1160, 780, (960, 720))
        self.root.configure(background=BG)
        apply_window_icon(root)
        self.root.protocol("WM_DELETE_WINDOW", self._request_exit)
        self.array: Array | None = None
        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.operation = ""
        self.operation_allows_browse = False
        self.pending_exit = False
        self.worker: threading.Thread | None = None
        self.export_worker: threading.Thread | None = None
        self.health_worker: threading.Thread | None = None
        self.export_busy = False
        self.health_busy = False
        self.ftp_service = None
        self.ftp_worker: threading.Thread | None = None
        self.ftp_transition = False
        self.ftp_dialog = None
        self._ftp_was_active = False
        self._pending_close_array = False
        self._ftp_stop_complete = None
        self.export_cancel = threading.Event()
        self._pending_export_progress = None
        self._next_health_check = time.monotonic() + 30
        self._last_activity = time.monotonic()
        self.settings = _load_settings()
        due = self.settings.get("verify_due", {})
        self.settings["verify_due"] = {str(key): value for key, value in due.items() if isinstance(value, (int, float))} if isinstance(due, dict) else {}
        self.auto_verify = tk.BooleanVar(master=root, value=bool(self.settings.get("auto_verify", False)))
        self.auto_health = tk.BooleanVar(master=root, value=bool(self.settings.get("auto_health", False)))
        try:
            self.verify_days = max(1, min(365, int(self.settings.get("verify_days", 7))))
        except (ValueError, TypeError):
            self.verify_days = 7
        self.current_id = "root"
        self.breadcrumbs = [("root", "Array")]
        self.offset = 0
        self.entries: dict[str, dict] = {}
        self.last_page_count = 0
        self._closing = False
        self._styles()
        self.shell = ttk.Frame(root, padding=28)
        self.shell.pack(fill="both", expand=True)
        header = ttk.Frame(self.shell)
        header.pack(fill="x", pady=(0, 18))
        self.brand_image = load_image(root, "app-icon-64.png")
        ttk.Label(header, image=self.brand_image).pack(side="left", padx=(0, 8))
        ttk.Label(header, text="RAIDiant", style="Brand.TLabel").pack(side="left")
        ttk.Label(header, text="YOUR STORAGE, TOGETHER", style="Eyebrow.TLabel").pack(side="left", padx=18)
        self.close_button = ttk.Button(header, text="Close array", command=self._close_array)
        self.content_area = _ScrollArea(self.shell)
        self.content_area.pack(fill="both", expand=True)
        self.content = self.content_area.body
        self.progress_frame = ttk.Frame(self.shell)
        self.progress_frame.pack(side="bottom", before=self.content_area, fill="x", pady=(14, 0))
        self.progress_text = tk.StringVar(master=root, value="Ready")
        ttk.Label(self.progress_frame, textvariable=self.progress_text, style="Muted.TLabel", wraplength=980).pack(anchor="w")
        progress_row = ttk.Frame(self.progress_frame)
        progress_row.pack(fill="x", pady=(6, 0))
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_button = ttk.Button(progress_row, text="Cancel safely", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=(12, 0))
        self.export_frame = ttk.Frame(self.shell)
        self.export_text = tk.StringVar(master=root, value="")
        ttk.Label(self.export_frame, textvariable=self.export_text, style="Muted.TLabel").pack(anchor="w")
        export_row = ttk.Frame(self.export_frame)
        export_row.pack(fill="x", pady=(5, 0))
        self.export_progress = ttk.Progressbar(export_row, style="Export.Horizontal.TProgressbar")
        self.export_progress.pack(side="left", fill="x", expand=True)
        self.export_cancel_button = ttk.Button(export_row, text="Cancel copy", command=self._cancel_export)
        self.export_cancel_button.pack(side="right", padx=(12, 0))
        self.root.bind_all("<KeyPress>", self._activity, add="+")
        self.root.bind_all("<ButtonPress>", self._activity, add="+")
        self._home()
        self.root.after(100, self._poll)
        self.root.after(250, self._startup)

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        default_font = tkfont.nametofont("TkDefaultFont", root=self.root)
        native_size = int(default_font.cget("size"))
        if native_size < 0:
            native_size = round(-native_size / self.root.winfo_fpixels("1p"))
        size = max(13 if self.root.tk.call("tk", "windowingsystem") == "aqua" else 11, native_size)
        # Entries, menus and native Tk dialogs use different named fonts. Keep
        # their native families, with one readable body size across all screens.
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            if name in tkfont.names(root=self.root):
                tkfont.nametofont(name, root=self.root).configure(size=size)
        self.ui_fonts = {}
        for name, points in (("title", max(22, round(size * 1.7))), ("brand", max(23, round(size * 1.8))),
                             ("card", max(16, round(size * 1.3))), ("bold", size)):
            derived = default_font.copy()
            derived.configure(size=points, weight="bold")
            self.ui_fonts[name] = derived
        style.configure(".", font="TkDefaultFont", background=BG, foreground=INK)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=self.ui_fonts["title"])
        style.configure("Brand.TLabel", font=self.ui_fonts["brand"], foreground=INK)
        style.configure("Eyebrow.TLabel", font=self.ui_fonts["bold"], foreground=ACCENT)
        style.configure("Card.TFrame", background="white", bordercolor=LINE, borderwidth=1, relief="solid")
        style.configure("Card.TLabel", background="white")
        style.configure("CardTitle.TLabel", background="white", font=self.ui_fonts["card"])
        style.configure("TButton", padding=(13, 9), background="white", bordercolor=LINE,
                        lightcolor="white", darkcolor="white", focuscolor=ACCENT)
        style.map("TButton", background=[("active", "#eee8fc"), ("disabled", BG)],
                  foreground=[("disabled", "#a7a0b4")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white")
        style.map("Accent.TButton", background=[("active", "#6340bb"), ("disabled", "#c4b8df")], foreground=[("disabled", "#f5f7fa")])
        style.configure("TEntry", fieldbackground="white", bordercolor=LINE, padding=7)
        style.configure("TCombobox", fieldbackground="white", bordercolor=LINE, padding=6)
        # Clam otherwise inherits grey/blue active states for individual elements.
        # Keep hover, keyboard focus and selection in the application's palette.
        hover, pressed, muted = "#eee8fc", "#ded1f5", "#a7a0b4"
        for control in ("TCheckbutton", "TRadiobutton"):
            style.configure(control, background=BG, foreground=INK, focuscolor=ACCENT,
                            indicatorbackground="white", indicatorforeground="white",
                            upperbordercolor=LINE, lowerbordercolor=LINE)
            style.map(control,
                      background=[("disabled", BG), ("pressed", pressed), ("active", hover)],
                      foreground=[("disabled", muted)],
                      indicatorbackground=[("disabled", "#ded9e8"), ("selected", ACCENT), ("active", hover)],
                      indicatorforeground=[("disabled", muted), ("selected", "white")],
                      upperbordercolor=[("disabled", LINE), ("active", ACCENT), ("focus", ACCENT)],
                      lowerbordercolor=[("disabled", LINE), ("active", ACCENT), ("focus", ACCENT)])
        for control in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(control, fieldbackground="white", bordercolor=LINE,
                            arrowcolor=ACCENT, selectbackground=ACCENT, selectforeground="white")
            style.map(control, bordercolor=[("disabled", LINE), ("focus", ACCENT), ("active", ACCENT)],
                      background=[("disabled", BG), ("pressed", pressed), ("active", hover)],
                      fieldbackground=[("disabled", BG), ("readonly", "white")],
                      arrowcolor=[("disabled", muted), ("active", ACCENT)])
        style.map("Treeview.Heading", background=[("pressed", pressed), ("active", hover)])
        for control in ("Horizontal.TScrollbar", "Vertical.TScrollbar"):
            style.configure(control, background=LINE, troughcolor=BG, arrowcolor=ACCENT,
                            bordercolor=BG, lightcolor=LINE, darkcolor=LINE)
            style.map(control, background=[("pressed", pressed), ("active", hover)],
                      arrowcolor=[("disabled", muted), ("active", ACCENT)])
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.font", "TkTextFont")
        style.configure("Treeview", background="white", fieldbackground="white",
                        rowheight=max(36, default_font.metrics("linespace") + 14), borderwidth=0)
        style.configure("Treeview.Heading", font=self.ui_fonts["bold"], padding=(8, 9))
        style.map("Treeview", background=[("selected", "#eee6ff")], foreground=[("selected", INK)])
        style.configure("TProgressbar", background=ACCENT, troughcolor=LINE, thickness=7)
        style.configure("Export.Horizontal.TProgressbar", background=CYAN, troughcolor=LINE, thickness=7)
        style.configure("TSeparator", background=LINE)

    def _clear_content(self) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()
        self.content_area.canvas.xview_moveto(0)
        self.content_area.canvas.yview_moveto(0)

    def _home(self) -> None:
        self._clear_content()
        self.close_button.pack_forget()
        self.root.title(APP_NAME)
        hero = ttk.Frame(self.content, padding=(18, 8, 18, 12))
        hero.pack(fill="x")
        self.hero_image = load_image(self.root, "app-icon-256.png")
        ttk.Label(hero, image=self.hero_image).pack(side="right", padx=(20, 0))
        intro = ttk.Frame(hero)
        intro.pack(side="left", fill="both", expand=True, pady=25)
        ttk.Label(intro, text="A home for your files.\nBuilt to stay resilient.", style="Title.TLabel").pack(anchor="w")
        ttk.Label(intro, text="Bring your storage together in one protected array.\nChoose your capacity, browse your files, and recover with confidence.",
                  style="Muted.TLabel", wraplength=630, justify="left").pack(anchor="w", pady=(18, 0))
        cards = ttk.Frame(self.content, padding=18)
        cards.pack(fill="x")
        cards.columnconfigure(0, weight=1, uniform="card")
        cards.columnconfigure(1, weight=1, uniform="card")
        self.home_buttons = []
        for column, title, body, button, command in (
            (0, "Create an array", "Choose member files that grow as you upload, a maximum capacity, and how many simultaneous failures to tolerate.", "Create array", self._create),
            (1, "Open an existing array", "Start with one member file. Add the available members and safely resume access to your files.", "Open array", self._open),
        ):
            card = ttk.Frame(cards, style="Card.TFrame", padding=26)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column == 0 else (10, 0))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=body, style="Card.TLabel", wraplength=385, justify="left").pack(anchor="w", pady=(14, 26))
            action = ttk.Button(card, text=button, style="Accent.TButton", command=command)
            action.pack(anchor="w")
            self.home_buttons.append(action)
        ttk.Label(self.content, text="Choose independent physical drives for independent protection. Your files stay accessible within the array's failure tolerance.",
                  style="Muted.TLabel", wraplength=940).pack(anchor="w", padx=18, pady=18)

    def _activity(self, _event=None) -> None:
        self._last_activity = time.monotonic()

    def _startup(self) -> None:
        if self._closing or self.busy or self.array is not None:
            return
        def inspect(_progress, _cancel):
            from .lifecycle import cleanup_orphan_scratch, pending_creations
            from .exporting import cleanup_orphan_exports
            cleanup_orphan_scratch()
            cleanup_orphan_exports()
            return pending_creations()
        self._run("Checking unfinished work", inspect, self._unfinished_creations, cancellable=False)

    def _unfinished_creations(self, pending) -> None:
        if not pending or self.pending_exit:
            return
        item = pending[0]
        dialog = _Dialog(self.root, "Unfinished array creation · RAIDiant", "730x450")
        body = ttk.Frame(dialog.body, padding=26)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Finish what you started", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"An earlier array creation was interrupted.\n{len(item['paths'])} members · {_size(item['member_size'])} per member",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(16, 12))
        ttk.Label(body, text=item.get("detail", "Resume creation to safely reuse its allocated member files."),
                  wraplength=660).pack(anchor="w", pady=(0, 14))
        ttk.Label(body, text="\n".join(str(path) for path in item["paths"][:5]),
                  style="Muted.TLabel", wraplength=660).pack(anchor="w")
        actions = ttk.Frame(body)
        actions.pack(side="bottom", fill="x", pady=(20, 0))
        def choose(value):
            dialog.result = value
            dialog.destroy()
        ttk.Button(actions, text="Resume creation", style="Accent.TButton", state="normal" if item.get("can_resume", True) else "disabled",
                   command=lambda: choose("resume")).pack(side="left")
        ttk.Button(actions, text="Remove incomplete files", state="normal" if item.get("can_discard") else "disabled",
                   command=lambda: choose("discard")).pack(side="left", padx=8)
        ttk.Button(actions, text="Later", command=lambda: choose("later")).pack(side="right")
        choice = dialog.show()
        if choice == "resume":
            def resume(progress, cancel):
                from .lifecycle import resume_creation
                return Array.open(resume_creation(item["id"], progress=progress, cancel=cancel), progress=progress)
            self._run("Resuming array creation", resume, self._opened)
        elif choice == "discard":
            if messagebox.askyesno("Remove unfinished member files", "Remove only the verified incomplete files owned by this interrupted creation?\n\nCompleted or unverified member files are preserved.", parent=self.root):
                def discard(_progress, _cancel):
                    from .lifecycle import discard_creation, pending_creations
                    discard_creation(item["id"])
                    return pending_creations()
                self._run("Removing incomplete creation", discard, self._unfinished_creations, cancellable=False)

    def _create(self) -> None:
        if self.busy:
            return
        result = _CreateDialog(self.root).show()
        if result:
            paths, parity, size = result
            self._run("Creating array", lambda progress, cancel: Array.create(paths, parity, size, progress=progress, cancel=cancel), self._opened)

    def _open(self) -> None:
        if self.busy:
            return
        first = filedialog.askopenfilename(parent=self.root, title="Open one array member", filetypes=[("RAIDiant member", "*.r5m"), ("All files", "*")])
        if not first:
            return
        self._run("Reading member", lambda _p, _c: inspect_member(first),
                  lambda info: self._choose_open_members(first, info), cancellable=False)

    def _choose_open_members(self, first: str, info: dict) -> None:
        result = _OpenDialog(self.root, first, info).show()
        if result:
            paths, readonly = result
            self._run("Opening array", lambda progress, cancel: Array.open(paths, readonly=readonly, progress=progress), self._opened, cancellable=False)

    def _opened(self, array: Array) -> None:
        self.array = array
        self._last_status = dict(array.status)
        self._recovery_notice = None
        self.current_id = "root"
        self.breadcrumbs = [("root", "Array")]
        self.offset = 0
        self._next_health_check = time.monotonic() + 30
        checks = self.settings.setdefault("verify_due", {})
        if not isinstance(checks, dict):
            checks = self.settings["verify_due"] = {}
        checks.setdefault(str(array.status["uuid"]), time.time() + self.verify_days * 86400)
        _save_settings(self.settings)
        self._manager()
        self._refresh()
        status = self._status()
        if self._needs_attention(status):
            self._offer_recovery()

    def _manager(self) -> None:
        self._clear_content()
        self.close_button.pack(side="right")
        self.summary_var = tk.StringVar(master=self.root)
        self.health_var = tk.StringVar(master=self.root)
        self.issues_var = tk.StringVar(master=self.root)
        self.path_var = tk.StringVar(master=self.root, value="Array")
        self.page_var = tk.StringVar(master=self.root)
        top = ttk.Frame(self.content)
        top.pack(fill="x")
        ttk.Label(top, text="Your array", style="Title.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.health_var, style="Eyebrow.TLabel").pack(side="right")
        ttk.Label(self.content, textvariable=self.summary_var, style="Muted.TLabel").pack(anchor="w", pady=(7, 6))
        ttk.Label(self.content, textvariable=self.issues_var, style="Muted.TLabel", wraplength=1080).pack(anchor="w", pady=(0, 12))

        toolbar = ttk.Frame(self.content)
        toolbar.pack(fill="x", pady=(0, 8))
        self.mutation_buttons = []
        for label, command in (("Upload folder…", self._upload_folder), ("Upload files…", self._upload_files), ("New folder", self._mkdir), ("Rename", self._rename), ("Delete", self._delete)):
            button = ttk.Button(toolbar, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.mutation_buttons.append(button)
        self.export_button = ttk.Button(toolbar, text="Copy to host…", command=self._export)
        self.export_button.pack(side="right")
        operations = ttk.Frame(self.content)
        operations.pack(fill="x", pady=(0, 14))
        self.verify_button = ttk.Button(operations, text="Verify integrity", command=self._scrub)
        self.verify_button.pack(side="left", padx=(0, 6))
        self.repair_button = ttk.Button(operations, text="Repair array", command=self._repair)
        self.repair_button.pack(side="left", padx=(0, 6))
        self.rebuild_button = ttk.Button(operations, text="Replace / rebuild…", command=self._rebuild)
        self.rebuild_button.pack(side="left", padx=(0, 6))
        self.members_button = ttk.Button(operations, text="Members", command=self._members)
        self.members_button.pack(side="right")
        self.monitor_button = ttk.Button(operations, text="Automatic checks…", command=self._monitor_settings)
        self.monitor_button.pack(side="right", padx=(0, 6))
        self.ftp_button = ttk.Button(operations, text="FTP access…", command=self._ftp_access)
        self.ftp_button.pack(side="right", padx=(0, 6))
        navigation = ttk.Frame(self.content)
        navigation.pack(fill="x", pady=(0, 8))
        self.up_button = ttk.Button(navigation, text="↑ Parent", command=self._up)
        self.up_button.pack(side="left")
        ttk.Label(navigation, textvariable=self.path_var).pack(side="left", padx=12)
        self.refresh_button = ttk.Button(navigation, text="Refresh", command=self._refresh)
        self.refresh_button.pack(side="right")
        listing = ttk.Frame(self.content)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, height=5, columns=("name", "kind", "size", "modified"), show="headings", selectmode="browse")
        for column, heading, width, anchor in (("name", "Name", 520, "w"), ("kind", "Type", 90, "w"), ("size", "Size", 120, "e"), ("modified", "Modified", 170, "w")):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, minwidth=60, stretch=column == "name", anchor=anchor)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._enter)
        self.tree.bind("<Return>", self._enter)
        self.tree.bind("<BackSpace>", lambda _e: self._up())
        footer = ttk.Frame(self.content)
        footer.pack(before=listing, side="bottom", fill="x", pady=(8, 0))
        ttk.Label(footer, textvariable=self.page_var, style="Muted.TLabel").pack(side="left")
        self.next_button = ttk.Button(footer, text="Next →", command=lambda: self._page(1))
        self.next_button.pack(side="right")
        self.previous_button = ttk.Button(footer, text="← Previous", command=lambda: self._page(-1))
        self.previous_button.pack(side="right", padx=6)

    def _status(self) -> dict:
        if self.array is None:
            return {}
        try:
            result = self.array.try_status() if hasattr(self.array, "try_status") else self.array.status
            if result is not None:
                self._last_status = dict(result)
            return dict(getattr(self, "_last_status", {"state": "checking", "writable": False}))
        except Exception as exc:
            return {"state": "recovery_required", "writable": False, "issues": [str(exc)]}

    def _refresh(self) -> None:
        if self.array is None:
            return
        if self.busy and not self.operation_allows_browse:
            self._set_controls()
            return
        status = self._status()
        if not self._needs_attention(status):
            self._recovery_notice = None
        state = str(status.get("state", "unknown")).replace("_", " ")
        self.health_var.set(f"{state.upper()}  ·  {'READ / WRITE' if status.get('writable') and not self.busy and not self.export_busy else 'READ ONLY'}")
        self.summary_var.set(f"{status.get('n', '?')} members  ·  {status.get('parity', '?')} failures tolerated  ·  "
                             f"{_size(status.get('used', 0))} used of {_size(status.get('capacity', 0))}  ·  {_size(status.get('free', 0))} free")
        issues = [str(x) for x in status.get("issues", [])]
        if status.get("missing"):
            issues.insert(0, "Missing members: " + ", ".join(str(int(i) + 1) for i in status["missing"]) + ". Access remains read-only until replacements are rebuilt.")
        self.issues_var.set("\n".join(issues[:3]) if issues else "All available members are consistent. Verify integrity to check stored file data.")
        try:
            listing = self.array.try_list_dir if hasattr(self.array, "try_list_dir") else self.array.list_dir
            rows = listing(self.current_id, offset=self.offset, limit=PAGE_SIZE)
            if rows is None:
                self._set_controls()
                return
            if not rows and self.offset:
                self.offset = max(0, self.offset - PAGE_SIZE)
                rows = listing(self.current_id, offset=self.offset, limit=PAGE_SIZE)
                if rows is None:
                    self._set_controls()
                    return
            selected = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            self.entries = {}
            for entry in rows:
                identity = str(entry["id"])
                self.entries[identity] = entry
                self.tree.insert("", "end", iid=identity, values=(entry["name"], "Folder" if entry["is_dir"] else "File", "—" if entry["is_dir"] else _size(entry.get("size", 0)), _date(entry.get("modified"))))
            if selected and selected[0] in self.entries:
                self.tree.selection_set(selected[0])
            self.last_page_count = len(rows)
            self.path_var.set(" / ".join(name for _, name in self.breadcrumbs))
            self.page_var.set(f"Items {self.offset + 1}–{self.offset + len(rows)} · Page {self.offset // PAGE_SIZE + 1}" if rows else "This folder is empty")
        except Exception as exc:
            self.issues_var.set(f"Directory could not be read: {exc}")
            self.tree.delete(*self.tree.get_children())
            self.entries = {}
            self.last_page_count = 0
            self.page_var.set("Directory unavailable")
        self._set_controls()

    def _set_controls(self) -> None:
        occupied = self.busy or self.export_busy or self.health_busy or self._ftp_active() or self.ftp_transition
        if self.array is None:
            for button in getattr(self, "home_buttons", []):
                if button.winfo_exists():
                    button.configure(state="disabled" if occupied else "normal")
            return
        status = self._status()
        editable = bool(status.get("writable")) and not occupied
        browse = not self.busy or self.operation_allows_browse
        for button in self.mutation_buttons:
            button.configure(state="normal" if editable else "disabled")
        for button in (self.verify_button, self.repair_button, self.close_button):
            button.configure(state="disabled" if occupied else "normal")
        self.repair_button.configure(state="normal" if not occupied and status.get("repairable", True) else "disabled")
        readable = status.get("readable", status.get("state") != "recovery_required")
        self.export_button.configure(state="normal" if browse and readable and not self.export_busy and not self.health_busy and not self._ftp_active() and not self.ftp_transition else "disabled",
                                     text="Copying to host…" if self.export_busy else "Copy to host…")
        self.rebuild_button.configure(state="normal" if not occupied and self._affected_slots(status) and status.get("repairable", True) else "disabled")
        self.refresh_button.configure(state="normal" if browse else "disabled")
        self.up_button.configure(state="normal" if browse and len(self.breadcrumbs) > 1 else "disabled")
        self.previous_button.configure(state="normal" if browse and self.offset else "disabled")
        self.next_button.configure(state="normal" if browse and self.last_page_count == PAGE_SIZE else "disabled")
        self.members_button.configure(state="normal" if browse else "disabled")
        self.monitor_button.configure(state="normal" if not occupied else "disabled")
        self.ftp_button.configure(state="disabled" if self.pending_exit else "normal",
                                  text="FTP running…" if self.ftp_service and self.ftp_service.running else "FTP access…")

    @staticmethod
    def _affected_slots(status: dict) -> list[int]:
        return sorted(set(status.get("missing", [])) | set(status.get("bad_members", [])))

    @classmethod
    def _needs_attention(cls, status: dict) -> bool:
        return bool(status.get("state") == "recovery_required" or status.get("bad_members")
                    or status.get("missing") or status.get("integrity_failed"))

    def _ftp_active(self) -> bool:
        return bool(self.ftp_service and self.ftp_service.status.get("transfers", 0))

    def _get_ftp_credentials(self, *, regenerate: bool = False) -> tuple[str, str]:
        from .ftp_service import generate_credentials
        saved = self.settings.get("ftp_credentials", {})
        if not isinstance(saved, dict):
            saved = {}
        username, password = saved.get("username"), saved.get("password")
        valid = all(isinstance(value, str) and len(value) == 4 and value.isascii() and value.isdigit()
                    for value in (username, password))
        if regenerate or not valid:
            username, password = generate_credentials()
            updated = dict(self.settings, ftp_credentials={"username": username, "password": password})
            if not _save_settings(updated):
                raise OSError("FTP credentials could not be saved. Check access to the application settings folder.")
            self.settings = updated
        return username, password

    def _ftp_access(self) -> None:
        if self.array is None or self.pending_exit:
            return
        try:
            username, password = self._get_ftp_credentials()
        except Exception as exc:
            messagebox.showerror("FTP access unavailable", str(exc), parent=self.root)
            return
        dialog = _Dialog(self.root, "FTP access · RAIDiant", "740x690")
        _fit_window(dialog, 740, 690, (700, 660))
        self.ftp_dialog = dialog
        body = ttk.Frame(dialog.body, padding=26)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Connect to your array", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Use these details in an FTP client while this array is open. Credentials stay the same until you generate a new pair.",
                  style="Muted.TLabel", wraplength=670).pack(anchor="w", pady=(10, 18))
        service_status = self.ftp_service.status if self.ftp_service else {}
        self.ftp_lan = tk.BooleanVar(master=dialog, value=service_status.get("host") == "0.0.0.0")
        self.ftp_port = tk.StringVar(master=dialog, value=str(service_status.get("port") or self.settings.get("ftp_port", 2121)))
        self.ftp_username = tk.StringVar(master=dialog, value=username)
        self.ftp_password = tk.StringVar(master=dialog, value=password)
        self.ftp_connection = tk.StringVar(master=dialog)
        self.ftp_state = tk.StringVar(master=dialog)
        fields = ttk.Frame(body)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        for row, label, value in ((0, "Username", self.ftp_username), (1, "Password", self.ftp_password)):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 18), pady=5)
            ttk.Entry(fields, textvariable=value, state="readonly").grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Label(fields, text="Port").grid(row=2, column=0, sticky="w", pady=5)
        self.ftp_port_entry = ttk.Entry(fields, textvariable=self.ftp_port, width=10)
        self.ftp_port_entry.grid(row=2, column=1, sticky="w", pady=5)
        self.ftp_lan_check = ttk.Checkbutton(body, text="Allow connections from my local network", variable=self.ftp_lan)
        self.ftp_lan_check.pack(anchor="w", pady=(14, 10))
        ttk.Label(body, text="FTP sends passwords and files without encryption. These four-digit credentials are intended for a trusted network. Keep this service off the public internet.",
                  style="Muted.TLabel", wraplength=670).pack(anchor="w", pady=(0, 16))
        ttk.Label(body, textvariable=self.ftp_connection, wraplength=670).pack(anchor="w", pady=(0, 8))
        ttk.Label(body, textvariable=self.ftp_state, style="Muted.TLabel", wraplength=670).pack(anchor="w")
        ttk.Label(body, text="The array's read-only and recovery rules also apply to FTP. Transfers pause other storage work. Names containing FTP path separators are shown with reversible percent escapes.",
                  style="Muted.TLabel", wraplength=670).pack(anchor="w", pady=(12, 0))
        buttons = ttk.Frame(body)
        buttons.pack(side="bottom", fill="x", pady=(20, 0))
        self.ftp_start_button = ttk.Button(buttons, text="Start FTP", style="Accent.TButton", command=self._ftp_start_from_dialog)
        self.ftp_start_button.pack(side="left")
        self.ftp_stop_button = ttk.Button(buttons, text="Stop FTP", command=self._stop_ftp)
        self.ftp_stop_button.pack(side="left", padx=8)
        self.ftp_regenerate_button = ttk.Button(buttons, text="New credentials", command=self._ftp_regenerate)
        self.ftp_regenerate_button.pack(side="left")
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right")
        self._ftp_dialog_refresh()
        try:
            dialog.show()
        finally:
            self.ftp_dialog = None

    def _ftp_dialog_refresh(self) -> None:
        if self.ftp_dialog is None or not self.ftp_dialog.winfo_exists():
            return
        status = self.ftp_service.status if self.ftp_service else {}
        running = bool(status.get("running"))
        locked = running or self.ftp_transition
        self.ftp_port_entry.configure(state="disabled" if locked else "normal")
        self.ftp_lan_check.configure(state="disabled" if locked else "normal")
        can_start = self.array is not None and not (locked or self.pending_exit or self.busy or self.export_busy or self.health_busy)
        self.ftp_start_button.configure(state="normal" if can_start else "disabled")
        self.ftp_stop_button.configure(state="normal" if running and not self.ftp_transition else "disabled")
        self.ftp_regenerate_button.configure(state="disabled" if self.ftp_transition or self.pending_exit else "normal")
        if running:
            addresses = ["127.0.0.1"] if status.get("host") == "127.0.0.1" else status.get("local_addresses", [])
            address = ", ".join(str(value) for value in addresses) or "No local network IP detected; check this computer's network settings"
            self.ftp_connection.set(f"Host: {address}\nPort: {status.get('port', 2121)}  ·  Protocol: FTP")
            self.ftp_state.set(f"Running · {status.get('connections', 0)} connections · {status.get('transfers', 0)} active transfers")
        elif self.ftp_transition:
            self.ftp_connection.set("Waiting for FTP to reach a safe stopping or starting point…")
            self.ftp_state.set("Active transfers are cancelled safely before the array closes.")
        else:
            self.ftp_connection.set("FTP is stopped. By default, only this computer can connect.")
            self.ftp_state.set(str(status.get("last_error") or ""))

    def _ftp_start_from_dialog(self) -> None:
        try:
            port = int(self.ftp_port.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid FTP port", "Choose a port from 1 to 65535.", parent=self.ftp_dialog or self.root)
            return
        self._start_ftp("0.0.0.0" if self.ftp_lan.get() else "127.0.0.1", port)

    def _start_ftp(self, host: str, port: int) -> None:
        if self.array is None or self.ftp_transition or self.pending_exit or self.busy or self.export_busy or self.health_busy or (self.ftp_service and self.ftp_service.running):
            return
        try:
            username, password = self._get_ftp_credentials()
        except Exception as exc:
            messagebox.showerror("FTP could not start", str(exc), parent=self.ftp_dialog or self.root)
            return
        self.settings["ftp_port"] = port
        _save_settings(self.settings)
        self.ftp_transition = True
        self._set_controls()
        self._ftp_dialog_refresh()
        array = self.array
        def start():
            service = None
            try:
                from .ftp_service import FTPService
                service = FTPService(array, username, password, host=host, port=port,
                                     on_event=lambda event: self.events.put(("ftp_event", event)))
                service.start()
                self.events.put(("ftp_started", service))
            except Exception as exc:
                if service is not None:
                    try:
                        service.stop()
                    except Exception:
                        # Retain the service so shutdown can be retried safely.
                        self.events.put(("ftp_started", service))
                self.events.put(("ftp_failed", exc))
        self.ftp_worker = threading.Thread(target=start, name="RAIDiant-FTP-start", daemon=False)
        self.ftp_worker.start()

    def _stop_ftp(self, complete: Callable | None = None) -> None:
        if complete is not None:
            self._ftp_stop_complete = complete
        if self.ftp_transition:
            return
        if self.ftp_service is None:
            callback, self._ftp_stop_complete = self._ftp_stop_complete, None
            if callback:
                callback()
            return
        self.ftp_transition = True
        self._set_controls()
        self._ftp_dialog_refresh()
        service = self.ftp_service
        def stop():
            try:
                service.stop()
                self.events.put(("ftp_stopped", service))
            except Exception as exc:
                self.events.put(("ftp_stop_failed", exc))
        self.ftp_worker = threading.Thread(target=stop, name="RAIDiant-FTP-stop", daemon=False)
        self.ftp_worker.start()

    def _ftp_regenerate(self) -> None:
        def regenerate():
            try:
                username, password = self._get_ftp_credentials(regenerate=True)
                if self.ftp_dialog is not None and self.ftp_dialog.winfo_exists():
                    self.ftp_username.set(username)
                    self.ftp_password.set(password)
                self._ftp_dialog_refresh()
            except Exception as exc:
                messagebox.showerror("Credentials could not be changed", str(exc), parent=self.root)
        self._stop_ftp(regenerate)

    def _handle_ftp_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "ftp_event":
            detail = event[1]
            if detail.get("type") == "error" and not self.pending_exit:
                messagebox.showerror("FTP operation stopped", str(detail.get("message", "The FTP operation could not complete.")), parent=self.ftp_dialog or self.root)
            if detail.get("type") == "changed":
                self._last_activity = time.monotonic()
                self._refresh()
        elif kind == "ftp_started":
            self.ftp_transition = False
            self.ftp_service = event[1]
            self.progress_text.set("FTP access started")
        elif kind == "ftp_stopped":
            self.ftp_transition = False
            self.ftp_service = None
            self.progress_text.set("FTP stopped safely")
            callback, self._ftp_stop_complete = self._ftp_stop_complete, None
            if callback and not self.pending_exit and not self._pending_close_array:
                callback()
        elif kind in ("ftp_failed", "ftp_stop_failed"):
            self.ftp_transition = False
            self._ftp_stop_complete = None
            if kind == "ftp_stop_failed":
                # Preserve the array and service when shutdown was not confirmed.
                self.pending_exit = False
                self._pending_close_array = False
            if not self.pending_exit:
                messagebox.showerror("FTP could not " + ("stop" if kind == "ftp_stop_failed" else "start"), str(event[1]), parent=self.ftp_dialog or self.root)
        self._ftp_dialog_refresh()
        self._set_controls()
        if self.pending_exit:
            self._finish_exit()
        elif self._pending_close_array:
            self._close_array()
        elif self.array is not None and self._needs_attention(self._status()):
            self._offer_recovery()

    def _monitor_settings(self) -> None:
        dialog = _Dialog(self.root, "Automatic checks · RAIDiant", "710x500")
        body = ttk.Frame(dialog.body, padding=26)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Keep an eye on your array", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Periodic checks are optional and off by default. Checks during opening, reading, and writing always stay active. Full integrity checks read stored data and can take longer.",
                  style="Muted.TLabel", wraplength=645).pack(anchor="w", pady=(14, 24))
        enabled = tk.BooleanVar(master=dialog, value=self.auto_verify.get())
        health_enabled = tk.BooleanVar(master=dialog, value=self.auto_health.get())
        days = tk.StringVar(master=dialog, value=str(self.verify_days))
        ttk.Checkbutton(body, text="Check member availability and headers every 30 seconds when idle", variable=health_enabled).pack(anchor="w", pady=(0, 12))
        ttk.Checkbutton(body, text="Run periodic full integrity checks when idle", variable=enabled).pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=14)
        ttk.Label(row, text="Check every").pack(side="left")
        ttk.Spinbox(row, from_=1, to=365, width=7, textvariable=days).pack(side="left", padx=10)
        ttk.Label(row, text="days").pack(side="left")
        ttk.Label(body, text="A new array's first automatic check is scheduled after this interval. Checks start after five minutes without activity, while the app is open. You can cancel a check safely.",
                  style="Muted.TLabel", wraplength=645).pack(anchor="w", pady=(4, 18))
        def save():
            try:
                number = int(days.get())
                if not 1 <= number <= 365:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid interval", "Choose 1–365 days.", parent=dialog)
                return
            changed = number != self.verify_days
            self.verify_days = number
            self.auto_verify.set(enabled.get())
            self.auto_health.set(health_enabled.get())
            self.settings.update(auto_verify=enabled.get(), auto_health=health_enabled.get(), verify_days=number)
            if changed and self.array:
                self.settings.setdefault("verify_due", {})[str(self.array.status["uuid"])] = time.time() + number * 86400
            _save_settings(self.settings)
            dialog.destroy()
        ttk.Button(body, text="Save settings", style="Accent.TButton", command=save).pack(side="bottom", anchor="e")
        dialog.show()

    def _record_verification(self, result: Any) -> None:
        if self.array and isinstance(result, dict):
            self.settings.setdefault("verify_due", {})[str(self.array.status["uuid"])] = time.time() + self.verify_days * 86400
            _save_settings(self.settings)

    def _schedule_checks(self) -> None:
        if self.pending_exit or self._closing or not self.array or self.busy or self.export_busy or self.health_busy or self._ftp_active() or self.ftp_transition:
            return
        now = time.monotonic()
        status = self._status()
        due = self.settings.get("verify_due", {}).get(str(status.get("uuid")), time.time() + 86400)
        if self.auto_verify.get() and time.time() >= due and now - self._last_activity >= 300 and not self._needs_attention(status):
            array = self.array
            self._run("Automatic integrity check", lambda progress, cancel: array.scrub(progress=progress, cancel=cancel, repair=False), self._automatic_verified)
            return
        if not self.auto_health.get() or now < self._next_health_check:
            return
        self._next_health_check = now + 30
        self.health_busy = True
        self._health_started = now
        self._set_controls()
        array = self.array
        def check():
            try:
                self.events.put(("health_done", array.health_check()))
            except Exception as exc:
                self.events.put(("health_error", exc))
        self.health_worker = threading.Thread(target=check, name="RAIDiant-health", daemon=False)
        self.health_worker.start()

    def _automatic_verified(self, result: Any) -> None:
        self._record_verification(result)
        if self._needs_attention(self._status()) or (isinstance(result, dict) and result.get("damaged_file_count")):
            self._report("Automatic integrity check", result)

    def _selected(self) -> dict | None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Choose an item", "Select a file or folder first.", parent=self.root)
            return None
        return self.entries.get(selected[0])

    def _enter(self, _event: Any = None) -> None:
        if self.busy and not self.operation_allows_browse:
            return
        selected = self.tree.selection()
        entry = self.entries.get(selected[0]) if selected else None
        if entry and entry["is_dir"]:
            self.current_id = entry["id"]
            self.breadcrumbs.append((entry["id"], entry["name"]))
            self.offset = 0
            self._refresh()

    def _up(self) -> None:
        if len(self.breadcrumbs) > 1 and (not self.busy or self.operation_allows_browse):
            self.breadcrumbs.pop()
            self.current_id = self.breadcrumbs[-1][0]
            self.offset = 0
            self._refresh()

    def _page(self, direction: int) -> None:
        if not self.busy or self.operation_allows_browse:
            self.offset = max(0, self.offset + PAGE_SIZE * direction)
            self._refresh()

    def _upload_folder(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="Upload a folder (hidden items are skipped)")
        if path and self.array:
            array, parent = self.array, self.current_id
            self._run("Uploading folder", lambda progress, cancel: array.upload_folder(path, parent_id=parent, progress=progress, cancel=cancel), self._import_done)

    def _upload_files(self) -> None:
        paths = filedialog.askopenfilenames(parent=self.root, title="Upload files (hidden items are skipped)")
        if paths and self.array:
            array, parent = self.array, self.current_id
            self._run("Uploading files", lambda progress, cancel: array.upload_files(list(paths), parent_id=parent, progress=progress, cancel=cancel), self._import_done)

    def _import_done(self, result: Any) -> None:
        self._refresh()
        if isinstance(result, dict):
            skipped = result.get("skipped", 0)
            if skipped:
                detail = len(skipped) if isinstance(skipped, (list, tuple, set)) else skipped
                messagebox.showinfo("Import complete", f"Import complete. {detail} hidden, unsupported, or skipped items were not imported.", parent=self.root)

    def _mkdir(self) -> None:
        name = simpledialog.askstring("New folder", "Folder name:", parent=self.root)
        if name is not None and self.array:
            array, parent = self.array, self.current_id
            self._run("Creating folder", lambda _p, _c: array.mkdir(name, parent_id=parent), cancellable=False)

    def _rename(self) -> None:
        entry = self._selected()
        if entry and self.array:
            name = simpledialog.askstring("Rename", "New name (host-safe names are applied only when exporting):", initialvalue=entry["name"], parent=self.root)
            if name is not None:
                array = self.array
                self._run("Renaming item", lambda _p, _c: array.rename(entry["id"], name), cancellable=False)

    def _delete(self) -> None:
        entry = self._selected()
        if entry and self.array and messagebox.askyesno("Delete from array", f"Permanently delete {entry['name']!r}" + (" and all of its contents?" if entry["is_dir"] else "?") + "\n\nThis cannot be undone.", icon="warning", parent=self.root):
            array = self.array
            self._run("Deleting item", lambda progress, cancel: array.delete(entry["id"], progress=progress, cancel=cancel))

    def _export(self) -> None:
        if self.export_busy or self.health_busy or self._ftp_active() or self.ftp_transition or (self.busy and not self.operation_allows_browse):
            return
        entry = self._selected()
        if not entry or not self.array:
            return
        destination = filedialog.askdirectory(parent=self.root, title="Copy to host — choose destination folder")
        if destination:
            array = self.array
            self._start_export(array, entry, destination)

    def _start_export(self, array: Array, entry: dict, destination: str) -> None:
        if self.export_busy or self.health_busy or self._ftp_active() or self.ftp_transition or (self.busy and not self.operation_allows_browse):
            return
        self.export_busy = True
        self.export_cancel.clear()
        self.export_frame.pack(side="bottom", before=self.content_area, fill="x", pady=(14, 0))
        self.export_progress.configure(value=0, maximum=100)
        self.export_text.set(f"Copying {entry['name']} to host…")
        self.export_cancel_button.configure(state="normal")
        self._set_controls()
        def progress(current, total, message=""):
            self._pending_export_progress = (current, total, message)
        def copy():
            try:
                result = array.export(entry["id"], destination, progress=progress, cancel=self.export_cancel.is_set)
                self.events.put(("export_done", result))
            except Exception as exc:
                self.events.put(("export_error", exc))
        self.export_worker = threading.Thread(target=copy, name="RAIDiant-export", daemon=False)
        self.export_worker.start()

    def _cancel_export(self) -> None:
        if self.export_busy:
            self.export_cancel.set()
            self.export_cancel_button.configure(state="disabled")
            self.export_text.set("Stopping copy safely…")

    def _scrub(self) -> None:
        if self.array:
            array = self.array
            self._run("Verifying array integrity", lambda progress, cancel: array.scrub(progress=progress, cancel=cancel, repair=False),
                      self._verified)

    def _verified(self, result: Any) -> None:
        self._record_verification(result)
        self._report("Integrity check", result)

    def _repair(self) -> None:
        if self.array and messagebox.askyesno("Repair array", "The engine will verify recovery records and repair data only when it has enough trustworthy information.\n\nFile operations will pause until repair finishes. Continue?", parent=self.root):
            array = self.array
            self._run("Repairing array", lambda progress, cancel: array.repair(progress=progress, cancel=cancel),
                      lambda result: self._report("Repair result", result))

    def _rebuild(self) -> None:
        if not self.array:
            return
        affected = self._affected_slots(self._status())
        if not affected:
            self._members()
            return
        self._rebuild_slots(affected)

    def _rebuild_slots(self, slots) -> None:
        replacements = {}
        for index in slots:
            destination = _save_filename(parent=self.root, title=f"Replacement location for member {int(index) + 1}", initialfile=f"array-member-{int(index) + 1:02d}-rebuilt.r5m", defaultextension=".r5m", filetypes=[("RAIDiant member", "*.r5m"), ("All files", "*")])
            if not destination:
                return
            if Path(destination).exists():
                try:
                    info = inspect_member(destination)
                    if info.get("state") != "rebuilding" or info["uuid"] != self.array.status["uuid"] or info["index"] != index:
                        raise ValueError("Choose a new filename or an incomplete rebuild for this member. Existing complete files are preserved.")
                except Exception as exc:
                    messagebox.showerror("Cannot resume this file", str(exc), parent=self.root)
                    return
            replacements[int(index)] = destination
        if len({str(Path(p).resolve()) for p in replacements.values()}) != len(replacements):
            messagebox.showerror("Duplicate replacement", "Each replacement needs a separate file path.", parent=self.root)
            return
        if messagebox.askyesno("Rebuild members", f"Create or resume {len(replacements)} replacement member(s)?\n\nThe array remains read-only until rebuild completes. Original members are preserved. You can browse and copy files to the host while reconstruction runs.", parent=self.root):
            array = self.array
            self._run("Rebuilding missing members", lambda progress, cancel: array.rebuild(replacements, progress=progress, cancel=cancel),
                      lambda _result: messagebox.showinfo("Rebuild complete", "Replacement members are complete. The array's current state is shown above the file list.", parent=self.root), allow_browse=True)

    def _members(self) -> None:
        status = self._status()
        dialog = _Dialog(self.root, "Array members · RAIDiant", "850x500")
        outer = ttk.Frame(dialog.body, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Member health and locations", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 14))
        tree = ttk.Treeview(outer, height=5, columns=("member", "state", "path"), show="headings")
        for column, heading, width in (("member", "Member", 80), ("state", "State", 120), ("path", "Location", 560)):
            tree.heading(column, text=heading)
            tree.column(column, width=width, stretch=column == "path")
        tree.pack(fill="both", expand=True)
        for member in status.get("members", []):
            tree.insert("", "end", iid=str(member["index"]), values=(int(member.get("index", 0)) + 1, member.get("state", "unknown"), member.get("path") or "Missing"))
        for position, member in enumerate(status.get("quarantined", [])):
            tree.insert("", "end", iid=f"quarantined-{position}", values=("—" if member.get("index") is None else int(member["index"]) + 1,
                        "Original isolated", member.get("path", "")))
        def replace_selected():
            selected = tree.selection()
            if selected and not selected[0].startswith("quarantined-"):
                index = int(selected[0])
                dialog.destroy()
                self._rebuild_slots([index])
        ttk.Button(outer, text="Replace selected member…", command=replace_selected,
                   state="normal" if status.get("repairable", True) and not (self.busy or self.export_busy or self.health_busy) else "disabled").pack(anchor="w", pady=(14, 0))
        ttk.Button(outer, text="Close", command=dialog.destroy).pack(anchor="e", pady=(14, 0))
        dialog.show()

    def _report(self, title: str, result: Any) -> None:
        self._refresh()
        if isinstance(result, dict):
            lines = []
            for key, value in result.items():
                label = key.replace("_", " ").capitalize()
                if isinstance(value, list):
                    value = "\n  ".join(str(item) for item in value[:12]) or "None"
                lines.append(f"{label}: {value}")
            text = "\n".join(lines) or "Operation completed."
        else:
            text = str(result) if result is not None else "Operation completed."
        report = result.get("report_path") if isinstance(result, dict) else None
        if report:
            dialog = _Dialog(self.root, title + " · RAIDiant", "810x560")
            body = ttk.Frame(dialog.body, padding=24)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text=title, style="Title.TLabel").pack(anchor="w", pady=(0, 14))
            display = tk.Text(body, wrap="word", background="white", foreground=INK,
                              borderwidth=0, padx=14, pady=14, height=12, font="TkTextFont")
            display.pack(fill="both", expand=True)
            display.insert("1.0", text)
            display.configure(state="disabled")
            ttk.Label(body, text="Save the detailed report before closing the array. It lists exact file names and each detected error.",
                      style="Muted.TLabel", wraplength=750).pack(anchor="w", pady=12)
            actions = ttk.Frame(body)
            actions.pack(fill="x")
            def save_report():
                target = _save_filename(parent=dialog, title="Save integrity report", initialfile="RAIDiant-integrity.jsonl",
                                                    defaultextension=".jsonl", filetypes=[("Integrity report", "*.jsonl"), ("All files", "*")])
                if target:
                    dialog.result = target
                    dialog.destroy()
            ttk.Button(actions, text="Save detailed report…", style="Accent.TButton", command=save_report).pack(side="left")
            ttk.Button(actions, text="Close", command=dialog.destroy).pack(side="right")
            target = dialog.show()
            if target:
                self._run("Saving integrity report", lambda progress, cancel: self._copy_report(report, target, progress, cancel),
                          lambda path: messagebox.showinfo("Report saved", f"Saved to:\n{path}", parent=self.root))
        else:
            messagebox.showinfo(title, text, parent=self.root)
        if self._needs_attention(self._status()):
            self._offer_recovery()

    @staticmethod
    def _copy_report(source, target, progress, cancel):
        import tempfile
        target = Path(target)
        temporary = None
        try:
            with open(source, "rb") as incoming, tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".raidiant-report-", delete=False) as outgoing:
                temporary = Path(outgoing.name)
                total, copied = os.fstat(incoming.fileno()).st_size, 0
                while chunk := incoming.read(1024 * 1024):
                    if cancel():
                        raise RaidError("Saving the report was cancelled.")
                    outgoing.write(chunk)
                    copied += len(chunk)
                    progress(copied, total, "Saving integrity report")
                outgoing.flush()
                os.fsync(outgoing.fileno())
            os.replace(temporary, target)
            return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _offer_recovery(self) -> None:
        if self.pending_exit or self.busy or self.export_busy or self.health_busy or self._ftp_active() or self.ftp_transition or not self.array:
            return
        status = self._status()
        notice = (status.get("state"), tuple(str(x) for x in status.get("issues", [])),
                  tuple(self._affected_slots(status)), bool(status.get("integrity_failed")))
        if getattr(self, "_recovery_notice", None) == notice:
            return
        self._recovery_notice = notice
        dialog = _Dialog(self.root, "Array needs attention · RAIDiant", "760x500")
        body = ttk.Frame(dialog.body, padding=28)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Choose how to recover", style="Title.TLabel").pack(anchor="w")
        details = [str(issue) for issue in status.get("issues", [])[:3]]
        affected = self._affected_slots(status)
        if affected:
            details.insert(0, "Members needing attention: " + ", ".join(str(index + 1) for index in affected))
        ttk.Label(body, text="\n".join(details) or "The array has a condition that needs recovery.",
                  wraplength=690, style="Muted.TLabel").pack(anchor="w", pady=(16, 18))
        explanation = "Repair checks recovery records and trustworthy data with exclusive access. Replace creates fresh member files and preserves the originals."
        if status.get("explicit_readonly"):
            explanation += "\n\nYou explicitly opened this array read-only. Close and reopen with write access before repairing or replacing members."
        if not status.get("readable", status.get("state") != "recovery_required"):
            explanation += "\n\nYou can browse the catalog, but copying files is blocked until recovery establishes a safe readable state."
        else:
            explanation += "\n\nRead-only browsing and verified copying remain available."
        ttk.Label(body, text=explanation, wraplength=690).pack(anchor="w")
        buttons = ttk.Frame(body)
        buttons.pack(side="bottom", fill="x", pady=(24, 0))
        def choose(value):
            dialog.result = value
            dialog.destroy()
        can_repair = bool(status.get("repairable", True)) and not status.get("explicit_readonly")
        ttk.Button(buttons, text="Repair now", style="Accent.TButton", state="normal" if can_repair else "disabled",
                   command=lambda: choose("repair")).pack(side="left")
        ttk.Button(buttons, text="Replace affected members…", state="normal" if can_repair and affected else "disabled",
                   command=lambda: choose("replace")).pack(side="left", padx=8)
        ttk.Button(buttons, text="Browse read-only", command=lambda: choose("browse")).pack(side="right")
        choice = dialog.show()
        if choice == "repair":
            array = self.array
            self._run("Recovering array", lambda progress, cancel: array.repair(progress=progress, cancel=cancel),
                      lambda result: self._report("Recovery result", result))
        elif choice == "replace":
            self._rebuild_slots(affected)

    def _run(self, title: str, function: Callable, complete: Callable | None = None, *, cancellable: bool = True, allow_browse: bool = False) -> None:
        if self.busy or self.export_busy or self.health_busy or self.pending_exit or self._ftp_active() or self.ftp_transition:
            return
        self.busy = True
        self.operation = title
        self.operation_allows_browse = allow_browse
        self._last_worker_progress = time.monotonic()
        self.cancel_event.clear()
        self.progress.configure(value=0, maximum=100)
        self.progress_text.set(title + "…")
        self.cancel_button.configure(state="normal" if cancellable else "disabled")
        self._set_controls()

        def progress(current: int, total: int, message: str = "") -> None:
            # Keep only the latest progress sample; terabyte operations must not
            # accumulate one queued Python object per stripe while a dialog is open.
            self._pending_progress = ("progress", current, total, message)
            self._last_worker_progress = time.monotonic()

        def worker() -> None:
            try:
                result = function(progress, self.cancel_event.is_set)
                self.events.put(("done", result, complete))
            except Exception as exc:
                self.events.put(("error", exc, title))

        # Non-daemon: closing the window must never abandon an in-flight write.
        self.worker = threading.Thread(target=worker, name="RAIDiant-storage", daemon=False)
        self.worker.start()

    def _poll(self) -> None:
        latest_progress = getattr(self, "_pending_progress", None)
        self._pending_progress = None
        export_progress = self._pending_export_progress
        self._pending_export_progress = None
        if export_progress and self.export_busy:
            current, total, message = export_progress
            fraction = max(0, min(100, 100 * current / total)) if total else 0
            self.export_progress.configure(value=fraction)
            self.export_text.set((message or "Copying to host") + (f" · {fraction:.0f}%" if total else ""))
        terminal_events = []
        for _ in range(1000):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "progress":
                latest_progress = event
            else:
                terminal_events.append(event)
        if latest_progress and not terminal_events:
            _, current, total, message = latest_progress
            fraction = max(0, min(100, 100 * current / total)) if total else 0
            self.progress.configure(value=fraction)
            suffix = f" · {fraction:.0f}%" if total else ""
            self.progress_text.set((message or self.operation) + suffix)
        for event in terminal_events:
            if self._closing:
                return
            kind = event[0]
            if kind.startswith("ftp_"):
                self._handle_ftp_event(event)
                continue
            if kind.startswith("export_"):
                self.export_busy = False
                self.export_frame.pack_forget()
                if not self.pending_exit:
                    if kind == "export_done":
                        messagebox.showinfo("Copy complete", f"Saved to:\n{event[1]}\n\nNames were made safe for this host where needed.", parent=self.root)
                    else:
                        messagebox.showerror("Copy stopped", str(event[1]), parent=self.root)
                if self.pending_exit:
                    self._finish_exit()
                else:
                    self._refresh()
                    if self._needs_attention(self._status()):
                        self._offer_recovery()
                continue
            if kind.startswith("health_"):
                self.health_busy = False
                if not self.pending_exit:
                    self._refresh()
                    if kind == "health_error":
                        self.progress_text.set("Member health check could not complete")
                        messagebox.showerror("Member health check", str(event[1]), parent=self.root)
                    if self._needs_attention(self._status()):
                        self._offer_recovery()
                else:
                    self._finish_exit()
                continue
            self.busy = False
            self.operation_allows_browse = False
            self.cancel_button.configure(state="disabled")
            self.progress.configure(value=100 if event[0] == "done" else 0)
            if event[0] == "done":
                _, result, complete = event
                self.progress_text.set(self.operation + " complete")
                # Even when exit was requested, adopt a newly created/opened
                # array so that its locks and descriptors are properly closed.
                if self.pending_exit and self.array is None and hasattr(result, "close"):
                    self.array = result
                elif not self.pending_exit and complete:
                    complete(result)
            else:
                _, exc, title = event
                self.progress_text.set(title + " stopped")
                if not self.pending_exit:
                    messagebox.showerror(title + " stopped", str(exc) or type(exc).__name__, parent=self.root)
            if self.pending_exit:
                self._finish_exit()
                continue
            self._refresh() if self.array else self._set_controls()
            if self._needs_attention(self._status()):
                self._offer_recovery()
        if not self._closing:
            ftp_active = self._ftp_active()
            if ftp_active:
                self._last_activity = time.monotonic()
            if ftp_active != self._ftp_was_active:
                self._ftp_was_active = ftp_active
                self._refresh() if self.array else self._set_controls()
                if not ftp_active and self.array and self._needs_attention(self._status()):
                    self._offer_recovery()
            self._ftp_dialog_refresh()
            if self.array is not None:
                revision = getattr(self.array, "health_revision", 0)
                if revision != getattr(self, "_shown_health_revision", 0):
                    self._shown_health_revision = revision
                    status = self._status()
                    if self._needs_attention(status) and hasattr(self, "health_var"):
                        self.health_var.set("NEEDS ATTENTION  ·  READ ONLY")
                        self.issues_var.set("\n".join(str(issue) for issue in status.get("issues", [])[-3:]))
            io = getattr(self.array, "io_status", None) if self.array is not None and (self.busy or self.export_busy or self.health_busy or ftp_active) else None
            if io and io.get("seconds", 0) >= 30:
                message = f"Member {int(io['index']) + 1} is taking longer to {io['operation']}. Waiting for host I/O; safe cancellation takes effect when it returns."
                if self.export_busy and not self.busy:
                    self.export_text.set(message)
                else:
                    self.progress_text.set(message)
            elif self.health_busy and time.monotonic() - getattr(self, "_health_started", time.monotonic()) >= 30:
                self.progress_text.set("Waiting for the host to finish a member health read. The window remains responsive; closing will wait for the read to return.")
            elif self.busy and time.monotonic() - self._last_worker_progress >= 30:
                self.progress_text.set("Waiting for storage to respond. Cancel requests stop safely when the current host I/O returns.")
            self._schedule_checks()
            self.root.after(100, self._poll)

    def _cancel(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.progress_text.set("Stopping safely at the next recovery boundary…")

    def _close_array(self) -> None:
        if self.busy or self.export_busy or self.health_busy or self.array is None:
            return
        if self.ftp_service is not None or self.ftp_transition:
            self._pending_close_array = True
            self._stop_ftp()
            return
        self._pending_close_array = False
        try:
            self.array.close()
        except Exception as exc:
            messagebox.showerror("Array close failed", str(exc), parent=self.root)
            return
        self.array = None
        self._home()
        self.progress.configure(value=0)
        self.progress_text.set("Array closed safely")

    def _request_exit(self) -> None:
        if self.pending_exit:
            return
        if self.busy or self.export_busy or self.health_busy or self._ftp_active() or self.ftp_transition:
            if not messagebox.askyesno("Operation in progress", "Stop active operations safely and close RAIDiant?\n\nThe window will stay open until all reads, writes, and copies reach a safe stopping point.", parent=self.root):
                return
            self.pending_exit = True
            self._cancel()
            self._cancel_export()
            self._stop_ftp()
            return
        self.pending_exit = True
        self._finish_exit()

    def _finish_exit(self) -> None:
        if self._closing:
            return
        if self.ftp_service is not None or self.ftp_transition:
            self._stop_ftp()
            return
        if self.busy or self.export_busy or self.health_busy:
            return
        if any(worker is not None and worker.is_alive() for worker in (self.worker, self.export_worker, self.health_worker, self.ftp_worker)):
            self.root.after(100, self._finish_exit)
            return
        if self.array is not None:
            try:
                self.array.close()
            except Exception as exc:
                self.pending_exit = False
                messagebox.showerror("Could not close safely", str(exc), parent=self.root)
                return
        self._closing = True
        self.root.destroy()


def main() -> None:
    prepare_desktop()
    root = tk.Tk(className="RAIDiant")
    RaidApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
