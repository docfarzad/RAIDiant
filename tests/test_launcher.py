"""Frozen helper dispatch must happen before ordinary application arguments."""
import json
import multiprocessing
from pathlib import Path
import runpy
import sys

import pytest

from raidiant import launcher


@pytest.mark.parametrize("arguments", [
    ["-B", "-S", "-I", "-c", "from multiprocessing.resource_tracker import main;main(14)"],
    ["--multiprocessing-fork", "parent_pid=100", "pipe_handle=200"],
])
def test_frozen_helpers_dispatch_before_argument_parser(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["RAIDiant", *arguments])
    diverted = []
    def dispatch():
        diverted.append(sys.argv[1:])
        raise SystemExit(0)
    def unexpected_parser(*args, **kwargs):
        pytest.fail("Helper reached the app's argument parser")
    monkeypatch.setattr(multiprocessing, "freeze_support", dispatch)
    monkeypatch.setattr(launcher.argparse, "ArgumentParser", unexpected_parser)
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code == 0
    assert diverted == [arguments]


def test_normal_diagnostic_runs_after_freeze_support(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: events.append("dispatch"))
    def diagnostic(gui=False):
        assert events == ["dispatch"]
        assert gui
        events.append("diagnostic")
        return {"ok": True, "tests": ["fake diagnostic"]}
    monkeypatch.setattr(launcher, "self_test", diagnostic)
    path = tmp_path / "report.json"
    assert launcher.main(["--gui-smoke-test", "--report", str(path)]) == 0
    assert events == ["dispatch", "diagnostic"]
    assert json.loads(path.read_text())["ok"]


@pytest.mark.parametrize("entry", ["script", "module"])
def test_spawn_import_does_not_launch_app_again(monkeypatch, entry):
    def unexpected_main(*args, **kwargs):
        pytest.fail("Importing the entry point as a worker launched the app")
    monkeypatch.setattr(launcher, "main", unexpected_main)
    if entry == "script":
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "main.py"), run_name="__mp_main__")
    else:
        runpy.run_module("raidiant", run_name="__mp_main__")
