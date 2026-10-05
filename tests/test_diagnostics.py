"""A passing diagnostic requires working helpers and isolated user state."""

import os
from pathlib import Path
from multiprocessing import shared_memory
import time
import types

import pytest

from raidiant import diagnostics, launcher


def _failed_worker(_name, _length, _connection):
    os._exit(7)


def _slow_worker(_name, _length, _connection):
    time.sleep(60)


def test_real_spawn_and_shared_memory_roundtrip():
    result = diagnostics.check_multiprocessing()
    assert "spawned worker/shared memory" in result
    if os.name == "posix":
        assert "POSIX resource-tracker shutdown" in result


def test_resource_tracker_nonzero_exit_is_not_reported_as_success(monkeypatch):
    from multiprocessing import resource_tracker

    read_fd, write_fd = os.pipe()
    tracker = types.SimpleNamespace(_pid=12345, _fd=write_fd, ensure_running=lambda: None)
    monkeypatch.setattr(resource_tracker, "ResourceTracker", lambda: tracker)
    observed = []

    def completed(pid, timeout):
        observed.append(pid)
        return 2

    monkeypatch.setattr(diagnostics, "_wait_tracker", completed)
    try:
        with pytest.raises(RuntimeError, match="Resource-tracker helper failed \\(exit 2\\)"):
            diagnostics._check_posix_tracker(1)
        assert os.read(read_fd, 1024) == b"REGISTER:raidiant-diagnostic:noop\nUNREGISTER:raidiant-diagnostic:noop\n"
        assert tracker._fd is None
        assert tracker._pid is None
        assert observed == [12345]  # It was reaped once; no accidental kill/re-wait.
    finally:
        os.close(read_fd)


def test_resource_tracker_already_reaped_is_failure_without_signaling_pid(monkeypatch):
    from multiprocessing import resource_tracker

    read_fd, write_fd = os.pipe()
    tracker = types.SimpleNamespace(_pid=12345, _fd=write_fd, ensure_running=lambda: None)
    monkeypatch.setattr(resource_tracker, "ResourceTracker", lambda: tracker)

    def already_reaped(*_args):
        raise ChildProcessError("another waiter consumed the status")

    def must_not_kill(*_args):
        pytest.fail("Cannot signal a PID after it was reaped")

    monkeypatch.setattr(diagnostics, "_wait_tracker", already_reaped)
    monkeypatch.setattr(os, "kill", must_not_kill)
    try:
        with pytest.raises(RuntimeError, match="exit status is unavailable"):
            diagnostics._check_posix_tracker(1)
        assert tracker._fd is None
        assert tracker._pid is None
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("worker,error,timeout", [
    (_failed_worker, (EOFError, RuntimeError), 5),
    (_slow_worker, TimeoutError, .2),
])
def test_failed_or_stuck_worker_cannot_pass_and_shared_memory_is_released(monkeypatch, worker, error, timeout):
    names = []
    original = shared_memory.SharedMemory

    def create_memory(*args, **kwargs):
        memory = original(*args, **kwargs)
        if kwargs.get("create"):
            names.append(memory.name)
        return memory

    monkeypatch.setattr(diagnostics.shared_memory, "SharedMemory", create_memory)
    monkeypatch.setattr(diagnostics, "_shared_memory_worker", worker)
    # The independent native helper is covered by the success check above;
    # this test targets worker failure and cleanup, including a short timeout.
    monkeypatch.setattr(diagnostics, "_check_posix_tracker", lambda timeout: None)
    with pytest.raises(error):
        diagnostics.check_multiprocessing(timeout=timeout)
    assert names
    for name in names:
        with pytest.raises(FileNotFoundError):
            original(name=name)


@pytest.mark.parametrize("existing", [None, "original-state"])
def test_diagnostic_state_override_is_restored_even_on_failure(monkeypatch, tmp_path, existing):
    if existing is None:
        monkeypatch.delenv("RAIDIANT_STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("RAIDIANT_STATE_DIR", existing)
    with pytest.raises(RuntimeError):
        with diagnostics.isolated_state(tmp_path):
            assert os.environ["RAIDIANT_STATE_DIR"] == str(tmp_path)
            raise RuntimeError("diagnostic failure")
    assert os.environ.get("RAIDIANT_STATE_DIR") == existing


def test_self_test_cannot_report_success_when_helper_fails(monkeypatch):
    def fail():
        raise RuntimeError("Resource-tracker helper failed (exit 2)")

    monkeypatch.setattr(diagnostics, "check_multiprocessing", fail)
    with pytest.raises(RuntimeError, match="Resource-tracker helper failed"):
        launcher.self_test()


def test_gui_diagnostic_does_not_touch_user_settings_or_recovery_state(monkeypatch, tmp_path):
    import tkinter
    from raidiant import gui

    settings = tmp_path / "real-settings.json"
    settings.write_text('{"untouched": true}', encoding="utf-8")
    settings_path = lambda: settings
    monkeypatch.setattr(gui, "_settings_path", settings_path)
    state = tmp_path / "real-state"
    state.mkdir()
    monkeypatch.setenv("RAIDIANT_STATE_DIR", str(state))
    monkeypatch.setattr(diagnostics, "check_multiprocessing", lambda: ["helpers checked"])
    destroyed = []
    root = types.SimpleNamespace(withdraw=lambda: None, update_idletasks=lambda: None,
                                 destroy=lambda: destroyed.append(True))
    monkeypatch.setattr(tkinter, "Tk", lambda: root)

    class DiagnosticApp:
        def __init__(self, _root):
            assert not gui._load_settings()
            assert Path(os.environ["RAIDIANT_STATE_DIR"]) != state
            self.tree = types.SimpleNamespace(get_children=lambda: ["diagnostic file"])

        def _opened(self, array):
            assert gui._save_settings({"temporary": str(array.status["uuid"])})

    monkeypatch.setattr(gui, "RaidApp", DiagnosticApp)
    assert launcher.self_test(gui=True)["ok"]
    assert settings.read_text(encoding="utf-8") == '{"untouched": true}'
    assert list(state.iterdir()) == []
    assert gui._settings_path is settings_path
    assert os.environ["RAIDIANT_STATE_DIR"] == str(state)
    assert destroyed == [True]
