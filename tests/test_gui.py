"""Desktop coordination checks: real Tk widgets with bounded fake storage jobs."""

import threading
import time
import json
import sys
import types
import gc
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont, ttk

import pytest

from raidiant import gui


class FakeArray:
    def __init__(self):
        self.closed = False
        self.copy_started = threading.Event()
        self.copy_release = threading.Event()
        self.health_started = threading.Event()
        self.health_release = threading.Event()
        self.entry = dict(id="one", name="notes.txt", is_dir=False, size=12, modified=0)
        self.status = dict(state="healthy", writable=True, readable=True, repairable=True,
                           uuid="test-array", n=5, parity=2, missing=[], bad_members=[],
                           issues=[], members=[], capacity=100, used=12, free=88)

    def list_dir(self, *_args, **_kwargs):
        return [self.entry]

    def export(self, _entry, destination, progress, cancel):
        self.copy_started.set()
        while not self.copy_release.wait(.01):
            if cancel():
                return Path(destination) / "cancelled"
        progress(1, 1, "Copied")
        return Path(destination) / "notes.txt"

    def health_check(self):
        self.health_started.set()
        self.health_release.wait(3)
        return self.status

    def close(self):
        self.closed = True


@pytest.fixture
def app(monkeypatch, tmp_path, tk_runtime):
    # Keep native image handling within one Tcl interpreter for the suite,
    # with a separate application window and no surviving callbacks per test.
    gc.collect()
    monkeypatch.setattr(gui, "_settings_path", lambda: tmp_path / "preferences.json")
    monkeypatch.setattr(gui.RaidApp, "_startup", lambda self: None)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *_a, **_k: None)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *_a, **_k: None)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *_a, **_k: True)
    root = tk.Toplevel(tk_runtime)
    root.withdraw()
    app = gui.RaidApp(root)
    array = FakeArray()
    app._opened(array)
    yield app, array
    array.copy_release.set()
    array.health_release.set()
    app.cancel_event.set()
    app.export_cancel.set()
    if app.ftp_service is not None:
        app.ftp_service.stop_release.set()
        app.ftp_service.start_release.set()
        app.ftp_service.stop()
    for worker in (app.worker, app.export_worker, app.health_worker, app.ftp_worker):
        if worker:
            worker.join(3)
    app._closing = True
    try:
        root.destroy()
    except tk.TclError:
        pass
    for callback in tk_runtime.tk.splitlist(tk_runtime.tk.call("after", "info")):
        tk_runtime.after_cancel(callback)


@pytest.fixture(scope="module")
def tk_runtime():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk requires a display (run with xvfb-run on Linux)")
    root.withdraw()
    yield root
    root.destroy()


def pump(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.root.update()
        time.sleep(.01)
    assert condition(), "GUI worker did not reach expected state"


def test_export_runs_during_rebuild_and_holds_mutations(app, tmp_path):
    desktop, array = app
    release = threading.Event()
    def rebuilding(_progress, cancel):
        while not release.wait(.01) and not cancel():
            pass
    desktop._run("Rebuilding", rebuilding, allow_browse=True)
    desktop._start_export(array, array.entry, str(tmp_path))
    assert array.copy_started.wait(2)
    assert desktop.busy and desktop.export_busy
    assert desktop.mutation_buttons[0].instate(["disabled"])
    desktop._close_array()
    assert not array.closed
    release.set()
    pump(desktop, lambda: not desktop.busy)
    assert desktop.export_busy
    assert desktop.repair_button.instate(["disabled"])
    assert desktop.close_button.instate(["disabled"])
    array.copy_release.set()
    pump(desktop, lambda: not desktop.export_busy)
    assert desktop.mutation_buttons[0].instate(["!disabled"])


def test_close_waits_for_idle_health_probe(app):
    desktop, array = app
    desktop.auto_health.set(True)
    desktop._next_health_check = 0
    desktop._schedule_checks()
    assert array.health_started.wait(2)
    desktop._request_exit()
    assert desktop.pending_exit and not array.closed
    array.health_release.set()
    pump(desktop, lambda: desktop._closing)
    assert array.closed


def test_periodic_checks_default_off_but_have_future_schedule(app):
    desktop, array = app
    due = desktop.settings["verify_due"][array.status["uuid"]]
    assert time.time() + 6 * 86400 < due < time.time() + 8 * 86400
    assert not desktop.auto_verify.get()
    assert not desktop.auto_health.get()
    desktop._next_health_check = 0
    desktop._schedule_checks()
    assert not array.health_started.is_set()


def test_recovery_replaces_bad_present_member_and_deduplicates_prompt(app, monkeypatch):
    desktop, array = app
    array.status.update(state="recovery_required", writable=False, bad_members=[2], issues=["Header damaged"])
    shown = []
    def choose(dialog):
        shown.append(dialog)
        dialog.destroy()
        return "replace"
    monkeypatch.setattr(gui._Dialog, "show", choose)
    replaced = []
    monkeypatch.setattr(desktop, "_rebuild_slots", lambda slots: replaced.extend(slots))
    desktop._offer_recovery()
    assert replaced == [2]
    desktop._refresh()
    desktop._offer_recovery()
    assert len(shown) == 1


def test_branding_assets_load_from_package(app):
    desktop, _array = app
    assert len(desktop.root._raidiant_icons) == 4
    assert (gui.ASSETS / "app-icon.ico").is_file()
    assert (gui.ASSETS / "app-icon.icns").is_file()


def test_body_inputs_status_and_report_use_readable_fonts(app, monkeypatch):
    desktop, _ = app
    body = tkfont.nametofont("TkDefaultFont", root=desktop.root)
    minimum = 13 if desktop.root.tk.call("tk", "windowingsystem") == "aqua" else 11
    assert body.cget("size") >= minimum
    for kind in (ttk.Entry, ttk.Combobox, ttk.Spinbox):
        control = kind(desktop.content)
        control_font = tkfont.Font(root=desktop.root, font=control.cget("font"))
        assert control_font.actual("size") == body.actual("size")
        control.destroy()
    style = ttk.Style(desktop.root)
    status = tkfont.Font(root=desktop.root, font=style.lookup("Eyebrow.TLabel", "font"))
    title = tkfont.Font(root=desktop.root, font=style.lookup("Title.TLabel", "font"))
    assert status.actual("size") == body.actual("size")
    assert title.actual("size") > body.actual("size")
    assert int(style.lookup("Treeview", "rowheight")) >= body.metrics("linespace") + 14

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)
    def inspect_report(dialog):
        display = next(widget for widget in descendants(dialog) if isinstance(widget, tk.Text))
        report_font = tkfont.Font(root=desktop.root, font=display.cget("font"))
        assert report_font.actual("size") == body.actual("size")
        dialog.destroy()
    monkeypatch.setattr(gui._Dialog, "show", inspect_report)
    desktop._report("Integrity report", {"report_path": "unused-report.jsonl"})


def test_large_native_fonts_keep_dialog_actions_reachable(app):
    desktop, _ = app
    old_fonts = {name: tkfont.nametofont(name, root=desktop.root).actual()
                 for name in tkfont.names(root=desktop.root) if name.startswith("Tk")}
    dialog = None
    try:
        native = tkfont.nametofont("TkDefaultFont", root=desktop.root)
        family = native.actual("family")
        native.configure(size=22)
        desktop._styles()
        assert native.actual("size") == 22
        assert native.actual("family") == family
        dialog = gui._CreateDialog(desktop.root)
        dialog.transient("")
        dialog.minsize(0, 0)
        dialog.geometry("500x300")
        dialog.deiconify()
        desktop.root.update()
        area = dialog.scroll_area
        assert area.vertical.winfo_ismapped()
        assert area.horizontal.winfo_ismapped()
        area.canvas.yview_moveto(1)
        area.canvas.xview_moveto(1)
        desktop.root.update_idletasks()
        action = dialog.create_button
        assert area.canvas.winfo_rooty() <= action.winfo_rooty()
        assert action.winfo_rooty() + action.winfo_height() <= area.canvas.winfo_rooty() + area.canvas.winfo_height()
        assert action.winfo_rootx() + action.winfo_width() <= area.canvas.winfo_rootx() + area.canvas.winfo_width()
    finally:
        if dialog is not None:
            dialog.destroy()
        for name, attributes in old_fonts.items():
            tkfont.nametofont(name, root=desktop.root).configure(**attributes)


def test_window_dimensions_respect_available_display():
    class SmallDisplay:
        def winfo_screenwidth(self): return 800
        def winfo_screenheight(self): return 600
        def geometry(self, value): self.size = value
        def minsize(self, width, height): self.minimum = width, height
    window = SmallDisplay()
    gui._fit_window(window, 1160, 780, (960, 720))
    assert window.size == "752x500"
    assert window.minimum == (752, 500)


@pytest.fixture
def fake_ftp(monkeypatch):
    services = []
    generated = iter((("0123", "4567"), ("8901", "2345"), ("6789", "0123")))
    class Service:
        def __init__(self, array, username, password, host, port, on_event):
            self.array, self.username, self.password = array, username, password
            self.on_event = on_event
            self.running = False
            self.stop_started = threading.Event()
            self.stop_release = threading.Event()
            self.start_started = threading.Event()
            self.start_release = threading.Event()
            self.start_release.set()
            self.stop_release.set()
            self.status = dict(running=False, transfers=0, connections=0, host=host, port=port,
                               local_addresses=["192.168.1.25", "10.0.0.5"])
            services.append(self)

        def start(self):
            self.start_started.set()
            self.start_release.wait(5)
            self.running = self.status["running"] = True

        def stop(self):
            self.stop_started.set()
            self.stop_release.wait(5)
            assert not self.array.closed, "Array was closed before FTP transfers stopped"
            self.running = self.status["running"] = False
            self.status["transfers"] = 0
    monkeypatch.setitem(sys.modules, "raidiant.ftp_service", types.SimpleNamespace(
        FTPService=Service, generate_credentials=lambda: next(generated)))
    return services


def test_ftp_credentials_persist_and_regenerate_only_on_request(app, fake_ftp):
    desktop, _ = app
    assert desktop._get_ftp_credentials() == ("0123", "4567")
    assert desktop._get_ftp_credentials() == ("0123", "4567")
    saved = json.loads(gui._settings_path().read_text(encoding="utf-8"))
    assert saved["ftp_credentials"] == {"username": "0123", "password": "4567"}
    assert desktop._get_ftp_credentials(regenerate=True) == ("8901", "2345")
    assert gui._load_settings()["ftp_credentials"]["username"] == "8901"


def test_ftp_refuses_start_if_credentials_cannot_be_retained(app, fake_ftp, monkeypatch):
    desktop, _ = app
    monkeypatch.setattr(gui, "_save_settings", lambda settings: False)
    desktop._start_ftp("127.0.0.1", 2121)
    assert not fake_ftp
    assert not desktop.ftp_transition


def test_ftp_stop_precedes_array_close_and_cancels_transfers(app, fake_ftp):
    desktop, array = app
    desktop._start_ftp("127.0.0.1", 2121)
    pump(desktop, lambda: desktop.ftp_service is not None)
    service = fake_ftp[0]
    service.stop_release.clear()
    service.status["transfers"] = 1
    desktop._close_array()
    assert service.stop_started.wait(2)
    assert not array.closed
    assert desktop.ftp_transition
    service.stop_release.set()
    pump(desktop, lambda: array.closed)
    assert desktop.ftp_service is None
    assert desktop.array is None


def test_ftp_activity_defers_periodic_checks_and_ui_mutations(app, fake_ftp):
    desktop, array = app
    desktop._start_ftp("127.0.0.1", 2121)
    pump(desktop, lambda: desktop.ftp_service is not None)
    fake_ftp[0].status["transfers"] = 1
    desktop.auto_health.set(True)
    desktop._next_health_check = 0
    desktop._schedule_checks()
    desktop._set_controls()
    assert not array.health_started.is_set()
    assert desktop.mutation_buttons[0].instate(["disabled"])
    assert desktop.verify_button.instate(["disabled"])
    assert desktop.ftp_button.instate(["!disabled"])


def test_exit_waits_for_ftp_start_then_stops_before_closing(app, fake_ftp, monkeypatch):
    desktop, array = app
    service_class = sys.modules["raidiant.ftp_service"].FTPService
    original_start = service_class.start
    def waiting_start(service):
        service.start_release.clear()
        original_start(service)
    monkeypatch.setattr(service_class, "start", waiting_start)
    desktop._start_ftp("127.0.0.1", 2121)
    deadline = time.monotonic() + 2
    while not fake_ftp and time.monotonic() < deadline:
        time.sleep(.01)
    service = fake_ftp[0]
    assert service.start_started.wait(2)
    desktop._request_exit()
    assert desktop.pending_exit and not array.closed
    service.start_release.set()
    pump(desktop, lambda: desktop._closing)
    assert service.stop_started.is_set()
    assert array.closed


def test_failed_ftp_stop_preserves_open_array(app, fake_ftp, monkeypatch):
    desktop, array = app
    desktop._start_ftp("127.0.0.1", 2121)
    pump(desktop, lambda: desktop.ftp_service is not None)
    service = fake_ftp[0]
    original_stop = service.stop
    def fail():
        raise OSError("Storage cleanup still needs recovery")
    monkeypatch.setattr(service, "stop", fail)
    desktop._close_array()
    pump(desktop, lambda: not desktop.ftp_transition)
    assert not array.closed
    assert desktop.ftp_service is service
    assert not desktop._pending_close_array
    monkeypatch.setattr(service, "stop", original_stop)


def test_ftp_dialog_defaults_local_and_shows_actual_lan_addresses(app, fake_ftp, monkeypatch):
    desktop, _ = app
    seen = []
    def show(dialog):
        seen.append((desktop.ftp_lan.get(), desktop.ftp_port.get(), desktop.ftp_connection.get()))
        dialog.destroy()
    monkeypatch.setattr(gui._Dialog, "show", show)
    desktop._ftp_access()
    assert seen[0][0:2] == (False, "2121")
    desktop._start_ftp("0.0.0.0", 2121)
    pump(desktop, lambda: desktop.ftp_service is not None)
    desktop._ftp_access()
    assert seen[1][0] is True
    assert "192.168.1.25" in seen[1][2] and "10.0.0.5" in seen[1][2]


def test_ftp_notifications_do_not_finish_unrelated_storage_job(app):
    desktop, _ = app
    desktop.busy = True
    desktop._handle_ftp_event(("ftp_event", {"type": "error", "message": "Client disconnected"}))
    assert desktop.busy


def test_initial_reservation_preserves_metadata_bank_capacity():
    assert gui._initial_member_reservation(1024 ** 4) == 8192 + 1024 ** 3
    assert gui._initial_member_reservation(8 * 1024 ** 2) == 8192 + 512 * 1024


def creation_dialog(desktop, tmp_path):
    dialog = gui._CreateDialog(desktop.root)
    dialog.n.set(3)
    dialog.parity.set(1)
    dialog._rebuild_rows()
    for i, variable in enumerate(dialog.path_vars):
        variable.set(str(tmp_path / f"create-member-{i}.r5m"))
    return dialog


def test_create_replace_and_report_use_shared_save_panel(app, monkeypatch, tmp_path):
    desktop, _ = app
    chosen = tmp_path / "new-member.r5m"
    calls = []
    def choose(**options):
        calls.append(options)
        return str(chosen) if len(calls) == 1 else ""
    monkeypatch.setattr(gui, "_save_filename", choose)
    dialog = creation_dialog(desktop, tmp_path)
    try:
        dialog._choose(0)
        assert dialog.path_vars[0].get() == str(chosen)
        assert calls[0]["parent"] is dialog
        assert calls[0]["initialfile"] == "array-member-01.r5m"
        assert calls[0]["defaultextension"] == ".r5m"
    finally:
        dialog.destroy()
    desktop._rebuild_slots([2])
    assert calls[1]["initialfile"] == "array-member-03-rebuilt.r5m"
    assert calls[1]["defaultextension"] == ".r5m"
    assert not desktop.busy  # Cancelling cannot start a rebuild.

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)
    def save_report(report):
        action = next(widget for widget in descendants(report)
                      if isinstance(widget, ttk.Button) and widget.cget("text") == "Save detailed report…")
        action.invoke()
        assert report.result is None  # Cancelling keeps the report open.
        assert report.winfo_exists()
        report.destroy()
    monkeypatch.setattr(gui._Dialog, "show", save_report)
    desktop._report("Integrity report", {"report_path": "unused-report.jsonl"})
    assert calls[2]["initialfile"] == "RAIDiant-integrity.jsonl"
    assert calls[2]["defaultextension"] == ".jsonl"
    assert not desktop.busy


def test_create_preserves_exact_manual_size_and_continues_after_check(app, tmp_path, monkeypatch):
    desktop, _ = app
    monkeypatch.setattr(gui, "capacity_report", lambda paths: {"recommended_size": 2 * 1024**3})
    dialog = creation_dialog(desktop, tmp_path)
    dialog.size_value.set("64.125")
    dialog.size_unit.set("MiB")
    dialog._submit()
    pump(desktop, lambda: dialog.result is not None)
    assert dialog.result[2] == 64 * 1024**2 + 128 * 1024
    assert dialog.size_value.get() == "64.125"
    assert dialog.size_unit.get() == "MiB"


def test_manual_size_edit_during_calculation_wins(app, tmp_path, monkeypatch):
    desktop, _ = app
    started, release = threading.Event(), threading.Event()
    def report(paths):
        started.set()
        assert release.wait(3)
        return {"recommended_size": 3 * 1024**3}
    monkeypatch.setattr(gui, "capacity_report", report)
    dialog = creation_dialog(desktop, tmp_path)
    try:
        dialog._calculate()
        assert started.wait(1)
        dialog.size_value.set("23.5")
        dialog.size_unit.set("MiB")
        release.set()
        pump(desktop, lambda: not dialog.calculating)
        assert dialog.size_value.get() == "23.5"
        assert dialog.size_unit.get() == "MiB"
        assert dialog.result is None
    finally:
        release.set()
        dialog.destroy()


def test_create_autofills_only_empty_size(app, tmp_path, monkeypatch):
    desktop, _ = app
    monkeypatch.setattr(gui, "capacity_report", lambda paths: {"recommended_size": 8 * 1024**2})
    dialog = creation_dialog(desktop, tmp_path)
    dialog._submit()
    pump(desktop, lambda: dialog.result is not None)
    assert dialog.result[2] == 8 * 1024**2


def test_oversized_manual_value_is_rejected_without_replacing_it(app, tmp_path, monkeypatch):
    desktop, _ = app
    errors = []
    monkeypatch.setattr(gui, "capacity_report", lambda paths: {"recommended_size": 8 * 1024**2})
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, *args, **kwargs: errors.append(title))
    dialog = creation_dialog(desktop, tmp_path)
    try:
        dialog.size_value.set("16")
        dialog.size_unit.set("MiB")
        dialog._submit()
        pump(desktop, lambda: bool(errors))
        assert errors == ["Insufficient safe capacity"]
        assert dialog.size_value.get() == "16"
        assert dialog.size_unit.get() == "MiB"
        assert dialog.result is None
    finally:
        dialog.destroy()
