"""Grow-on-demand allocation, durable tail cleanup and fixed-format compatibility."""
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from raidiant import engine, host
from raidiant.engine import Array, Cancelled, RaidError, RecoveryRequired


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIDIANT_STATE_DIR", str(tmp_path / "state"))


def make_array(tmp_path, *, preallocate=False):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(3)]
    array = Array.create(paths, 1, 8 * 1024**2, chunk_size=4096, preallocate=preallocate)
    source = tmp_path / "original.bin"
    source.write_bytes(bytes(range(251)) * 509)
    array.upload_files([source])
    return array, paths, source, array.list_dir()[0]


def check_export(array, entry, source, tmp_path):
    directory = tmp_path / "exports"
    directory.mkdir(exist_ok=True)
    assert array.export(entry["id"], directory).read_bytes() == source.read_bytes()


def test_dynamic_growth_delete_and_interior_reuse(tmp_path):
    array, paths, source, first = make_array(tmp_path)
    with array:
        assert array.dynamic and array.h["version"] == 3
        assert all(p.stat().st_size == array._live_end() < array.h["member_size"] for p in paths)
        array.upload_files([source])
        second = next(row for row in array.list_dir() if row["id"] != first["id"])
        peak = [p.stat().st_size for p in paths]
        array.delete(first["id"])
        assert [p.stat().st_size for p in paths] == peak, "Interior holes remain reusable"
        array.upload_files([source])
        assert [p.stat().st_size for p in paths] == peak, "Reusing a free extent must not grow the files"
        for row in array.list_dir():
            array.delete(row["id"])
        assert all(p.stat().st_size == array._stripe_offset(0) for p in paths)
    with Array.open(paths) as reopened:
        assert reopened.status["writable"]


def test_legacy_fixed_arrays_keep_their_reservation(tmp_path):
    array, paths, source, entry = make_array(tmp_path, preallocate=True)
    with array:
        assert not array.dynamic and array.h["version"] == 1
        array.delete(entry["id"])
        assert all(p.stat().st_size == array.h["member_size"] for p in paths)
    with Array.open(paths) as reopened:
        assert reopened.status["writable"] and not reopened.dynamic
        reopened.upload_files([source])


def test_cancelled_upload_reclaims_its_reserved_tail_immediately(tmp_path):
    array, paths, source, entry = make_array(tmp_path)
    before = [p.stat().st_size for p in paths]
    with array:
        cancelled = False
        def progress(*_):
            nonlocal cancelled
            cancelled = True
        with pytest.raises(Cancelled):
            array.upload_files([source], progress=progress, cancel=lambda: cancelled)
        assert [p.stat().st_size for p in paths] == before
        assert len(array.list_dir()) == 1
        assert array.status["writable"]
        check_export(array, entry, source, tmp_path)


def test_partial_tail_allocation_enospc_stops_and_reclaims(tmp_path, monkeypatch):
    array, paths, source, entry = make_array(tmp_path)
    before = [p.stat().st_size for p in paths]
    original = engine.extend_file
    failed = False
    def no_space(file, size):
        nonlocal failed
        if file is array._files[1] and not failed:
            failed = True
            file.seek(0, os.SEEK_END)
            file.write(bytes(257))
            raise OSError(errno.ENOSPC, "Injected physical disk exhaustion")
        return original(file, size)
    monkeypatch.setattr(engine, "extend_file", no_space)
    with array:
        with pytest.raises(RecoveryRequired, match="Physical storage is full on member 2"):
            array.upload_files([source])
        assert [p.stat().st_size for p in paths] == before
        assert len(array.list_dir()) == 1
        assert not array.status["writable"]
        array.repair()
        check_export(array, entry, source, tmp_path)


@pytest.mark.parametrize("boundary", [
    "allocation_intent_durable", "allocation_member_durable", "stripe_written",
    "data_durable", "before_file_commit",
])
def test_process_kill_recovery_reclaims_uncommitted_growth(tmp_path, boundary):
    array, paths, source, entry = make_array(tmp_path)
    before = [p.stat().st_size for p in paths]
    array.close()
    script = r'''
import os, sys
from raidiant.engine import Array
array = Array.open(sys.argv[3:])
array._fault_hook = lambda name: os._exit(87) if name == sys.argv[1] else None
array.upload_files([sys.argv[2]])
'''
    result = subprocess.run([sys.executable, "-c", script, boundary, str(source), *map(str, paths)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 87, result.stderr
    with Array.open(paths) as reopened:
        assert reopened.status["state"] == "recovery_required"
        with pytest.raises(RaidError):
            reopened.upload_files([source])
        assert len(reopened.list_dir()) == 1
        reopened.repair()
        assert [p.stat().st_size for p in paths] == before
        check_export(reopened, entry, source, tmp_path)


def test_kill_during_cleanup_is_resumable(tmp_path):
    array, paths, source, entry = make_array(tmp_path)
    before = [p.stat().st_size for p in paths]
    array.close()
    upload = r'''
import os, sys
from raidiant.engine import Array
a = Array.open(sys.argv[2:])
a._fault_hook = lambda name: os._exit(87) if name == 'data_durable' else None
a.upload_files([sys.argv[1]])
'''
    assert subprocess.run([sys.executable, "-c", upload, str(source), *map(str, paths)], timeout=30).returncode == 87
    recover = r'''
import os, sys
from raidiant.engine import Array
a = Array.open(sys.argv[1:])
a._fault_hook = lambda name: os._exit(88) if name == 'tail_reclaimed' else None
a.repair()
'''
    assert subprocess.run([sys.executable, "-c", recover, *map(str, paths)], timeout=30).returncode == 88
    with Array.open(paths) as reopened:
        assert not reopened.status["writable"]
        reopened.repair()
        assert [p.stat().st_size for p in paths] == before
        check_export(reopened, entry, source, tmp_path)


def test_partly_replicated_finish_is_preserved_during_abort(tmp_path):
    array, paths, source, entry = make_array(tmp_path)
    original_append = array._append
    def append(event):
        if event["op"] == "finish":
            def fail(name):
                if name == "metadata_member_written":
                    array._fault_hook = None
                    raise OSError(errno.EIO, "Interrupted finish publication")
            array._fault_hook = fail
        return original_append(event)
    array._append = append
    with array:
        with pytest.raises(OSError, match="finish publication"):
            array.upload_files([source])
        assert len(array.list_dir()) == 2, "A valid finish record is never rolled back"
        array._append = original_append
        array.repair()
        for row in array.list_dir():
            check_export(array, row, source, tmp_path)
        assert all(p.stat().st_size == array._live_end() for p in paths)


def test_short_dynamic_member_is_detected_and_reconstructed(tmp_path):
    array, paths, source, entry = make_array(tmp_path)
    prefix = array._stripe_offset(0)
    expected = array._live_end()
    array.close()
    with paths[1].open("r+b") as file:
        file.truncate(prefix + 31)
    with Array.open(paths) as reopened:
        assert not reopened.status["writable"]
        assert 1 in reopened.status["bad_members"]
        reopened.repair()
        assert reopened.status["writable"]
        assert paths[1].stat().st_size == expected
        check_export(reopened, entry, source, tmp_path)


def test_external_size_changes_during_session_are_not_silently_accepted(tmp_path):
    array, paths, source, entry = make_array(tmp_path)
    with array:
        with paths[1].open("ab") as file:
            file.write(b"external")
        report = array.health_check()
        assert not array.status["writable"]
        assert 1 in array.status["bad_members"]


def test_dynamic_rebuild_allocates_live_data_only(tmp_path):
    array, paths, source, entry = make_array(tmp_path)
    expected = array._live_end()
    array.close()
    replacement = tmp_path / "rebuilt.r5m"
    with Array.open(paths[1:]) as degraded:
        degraded.rebuild({0: replacement})
        assert replacement.stat().st_size == expected < degraded.h["member_size"]
        assert degraded.h["version"] == 3
        check_export(degraded, entry, source, tmp_path)


def test_extension_fallback_touches_only_new_tail(tmp_path, monkeypatch):
    path = tmp_path / "tail.bin"
    data = b"preserve committed bytes" * 200
    path.write_bytes(data)
    def unsupported(*_):
        raise OSError(errno.ENOTSUP, "Use fallback")
    monkeypatch.setattr(host, "_windows_allocate", unsupported)
    monkeypatch.setattr(host.os, "posix_fallocate", unsupported, raising=False)
    monkeypatch.setattr(host, "ALLOCATION_CHUNK", 17)
    with path.open("r+b", buffering=0) as file:
        file.seek(7)
        host.extend_file(file, len(data) + 79)
        assert file.tell() == 7
    assert path.read_bytes() == data + bytes(79)
