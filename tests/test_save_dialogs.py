"""Native save-panel options: avoid the clipped Aqua accessory safely."""
from types import SimpleNamespace

import pytest

from raidiant import gui


@pytest.mark.parametrize("window_system", ["aqua", "win32", "x11"])
@pytest.mark.parametrize("selected", ["", "/chosen/folder/custom name.r5m"])
def test_save_panel_preserves_native_selection_and_options(monkeypatch, window_system, selected):
    def tk_call(*args):
        assert args == ("tk", "windowingsystem")
        return window_system
    parent = SimpleNamespace(tk=SimpleNamespace(call=tk_call))
    captured = []
    def save(**options):
        captured.append(options)
        return selected
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", save)
    filters = [("RAIDiant member", "*.r5m"), ("All files", "*")]
    options = dict(title="Save member 1", initialfile="array-member-01.r5m",
                   initialdir="/chosen/folder", defaultextension=".r5m",
                   confirmoverwrite=True, filetypes=filters)
    assert gui._save_filename(parent=parent, **options) == selected
    expected = dict(options, parent=parent)
    if window_system == "aqua":
        expected.pop("filetypes")
    assert captured == [expected]
    assert options["filetypes"] == filters  # Caller's options remain reusable.


def test_macos_report_keeps_jsonl_default_without_format_accessory(monkeypatch):
    parent = SimpleNamespace(tk=SimpleNamespace(call=lambda *_: "aqua"))
    def save(**options):
        assert "filetypes" not in options
        assert options["initialfile"] == "RAIDiant-integrity.jsonl"
        assert options["defaultextension"] == ".jsonl"
        return "/reports/health.jsonl"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", save)
    assert gui._save_filename(parent=parent, initialfile="RAIDiant-integrity.jsonl",
                              defaultextension=".jsonl", filetypes=[("Report", "*.jsonl")]) == "/reports/health.jsonl"
