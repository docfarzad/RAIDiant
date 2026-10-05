"""Member isolation, incarnation retirement, and interrupted publication tests."""
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from raidiant import engine
from raidiant.engine import Array, Cancelled, RaidError, UnrecoverableError, inspect_member


def populated(tmp_path, preallocate=False):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(5)]
    source = tmp_path / "payload.bin"
    data = bytes(range(251)) * 1024
    source.write_bytes(data)
    array = Array.create(paths, 2, 2 * 1024**2, chunk_size=4096, preallocate=preallocate)
    array.upload_files([source])
    entry = array.list_dir()[0]
    return array, paths, entry, data


def exported(array, entry, tmp_path):
    output = tmp_path / "export"
    output.mkdir(exist_ok=True)
    return array.export(entry["id"], output).read_bytes()


def damage_headers(path):
    with path.open("r+b", buffering=0) as file:
        file.write(b"broken!".ljust(8192, b"\0"))


def test_open_isolates_unknown_corrupt_header_without_guessing_slot(tmp_path):
    array, paths, entry, data = populated(tmp_path)
    array.close()
    damage_headers(paths[0])
    before = paths[0].read_bytes()
    with Array.open(paths) as reopened:
        assert reopened.status["missing"] == [0]
        assert reopened.status["quarantined"][0]["index"] is None
        assert not reopened.status["writable"]
        assert exported(reopened, entry, tmp_path) == data
    assert paths[0].read_bytes() == before


def test_runtime_corrupt_headers_are_isolated_and_replaceable(tmp_path):
    array, paths, entry, data = populated(tmp_path)
    with array:
        damage_headers(paths[0])
        array.repair()
        assert array.status["missing"] == [0]
        assert array.status["quarantined"][0]["index"] == 0
        assert exported(array, entry, tmp_path) == data
        array.rebuild({0: tmp_path / "replacement.r5m"})
        assert array.status["writable"]
        assert exported(array, entry, tmp_path) == data


@pytest.mark.parametrize("failure_after", [0, 2])
def test_metadata_io_failure_uses_sufficient_survivors(tmp_path, monkeypatch, failure_after):
    array, paths, entry, data = populated(tmp_path)
    array.close()
    original = engine._read_record
    calls = 0
    def failing(file, *args):
        nonlocal calls
        if Path(file.name) == paths[0]:
            calls += 1
            if calls > failure_after:
                raise OSError(errno.EIO, "Injected metadata read failure")
        return original(file, *args)
    monkeypatch.setattr(engine, "_read_record", failing)
    with Array.open(paths) as reopened:
        assert reopened.status["missing"] == [0]
        assert exported(reopened, entry, tmp_path) == data


def test_metadata_io_failure_beyond_redundancy_blocks_open(tmp_path, monkeypatch):
    array, paths, _, _ = populated(tmp_path)
    array.close()
    original = engine._read_record
    def failing(file, *args):
        if Path(file.name) in paths[:3]:
            raise OSError(errno.EIO, "Injected metadata read failure")
        return original(file, *args)
    monkeypatch.setattr(engine, "_read_record", failing)
    with pytest.raises(UnrecoverableError, match="fewer than 3"):
        Array.open(paths)


def test_conflicting_valid_header_copies_block_instead_of_quarantine(tmp_path):
    array, paths, _, _ = populated(tmp_path)
    array.close()
    h = inspect_member(paths[0])
    h["uuid"] = str(uuid.uuid4())
    with paths[0].open("r+b", buffering=0) as file:
        file.write(engine._header_bytes(h))
    with pytest.raises(UnrecoverableError, match="Conflicting identities"):
        Array.open(paths)


def test_catalog_reload_exception_freezes_partial_catalog(tmp_path, monkeypatch):
    array, _, entry, _ = populated(tmp_path)
    with array:
        original = array._apply
        def failing(event):
            if event["op"] != "init":
                raise RuntimeError("Injected scratch catalog failure")
            return original(event)
        monkeypatch.setattr(array, "_apply", failing)
        with pytest.raises(RuntimeError, match="scratch catalog"):
            array._load_catalog()
        assert array._needs_recovery
        assert not array._loading
        assert not array.status["writable"]
        with pytest.raises(RaidError):
            exported(array, entry, tmp_path)


@pytest.mark.parametrize("preallocate", [False, True])
def test_replaced_original_is_retired_by_survivors_and_preserved(tmp_path, preallocate):
    array, paths, entry, data = populated(tmp_path, preallocate=preallocate)
    replacement = tmp_path / "replacement.r5m"
    with array:
        assert array.h["version"] == (1 if preallocate else 3)
        array.rebuild({0: replacement})
        assert array.h["version"] == (2 if preallocate else 3)
        array.mkdir("generation persisted")
        array._checkpoint()
        roster = list(array.h["member_roster"])
    original_after_rebuild = hashlib.sha256(paths[0].read_bytes()).digest()
    with Array.open(paths) as reopened:
        assert reopened.status["missing"] == [0]
        assert any("retired" in issue for issue in reopened.status["issues"])
        assert exported(reopened, entry, tmp_path) == data
    assert hashlib.sha256(paths[0].read_bytes()).digest() == original_after_rebuild
    with Array.open([replacement, *paths[1:]]) as reopened:
        assert reopened.status["writable"]
        for index, file in reopened._files.items():
            headers = engine._read_headers(file)
            assert len(headers) == 2
            assert all(h["version"] == (2 if preallocate else 3) and h["member_id"] == roster[index] for h in headers)


def test_conflicting_replacement_rosters_are_not_voted_away(tmp_path):
    array, paths, _, _ = populated(tmp_path)
    replacement = tmp_path / "replacement.r5m"
    with array:
        array.rebuild({0: replacement})
    h = inspect_member(paths[1])
    h["member_roster"][2] = uuid.uuid4().hex
    with paths[1].open("r+b", buffering=0) as file:
        file.write(engine._header_bytes(h) * 2)
    with pytest.raises(UnrecoverableError, match="Conflicting member replacement"):
        Array.open([replacement, *paths[1:]])


def test_cancelled_rebuild_retains_incarnation_when_resumed(tmp_path):
    array, paths, entry, data = populated(tmp_path)
    array.close()
    replacement = tmp_path / "replacement.r5m"
    with Array.open(paths[1:]) as degraded:
        stop = False
        def progress(done, total, message):
            nonlocal stop
            if message == "Rebuilding replacement members" and done >= 2:
                stop = True
        with pytest.raises(Cancelled):
            degraded.rebuild({0: replacement}, progress=progress, cancel=lambda: stop)
        pending = inspect_member(replacement)
        degraded.rebuild({0: replacement})
        ready = inspect_member(replacement)
        assert ready["member_id"] == pending["member_id"]
        assert ready["member_roster"] == pending["member_roster"]
        assert degraded.status["writable"]
        assert exported(degraded, entry, tmp_path) == data


@pytest.mark.parametrize("boundary", ["rebuild_header_durable", "membership_header_durable"])
def test_process_crash_during_replacement_publication(tmp_path, boundary):
    array, paths, entry, data = populated(tmp_path)
    array.close()
    replacement = tmp_path / "replacement.r5m"
    script = """
import os,sys
from raidiant.engine import Array
a=Array.open(sys.argv[3:])
a._fault_hook=lambda name: os._exit(87) if name==sys.argv[1] else None
a.rebuild({0:sys.argv[2]})
"""
    result = subprocess.run([sys.executable, "-c", script, boundary, str(replacement),
                             *map(str, paths[1:])], timeout=30)
    assert result.returncode == 87
    assert inspect_member(replacement)["state"] == "ready"
    with Array.open([replacement, *paths[1:]]) as reopened:
        assert not reopened.status["writable"]
        reopened.repair()
        assert reopened.status["writable"]
        assert exported(reopened, entry, tmp_path) == data
    with Array.open(paths) as old_original:
        assert old_original.status["missing"] == [0]


def test_partial_multi_member_publication_can_finish_remaining_target(tmp_path):
    array, paths, entry, data = populated(tmp_path)
    array.close()
    first, second = tmp_path / "first.r5m", tmp_path / "second.r5m"
    script = """
import os,sys
from raidiant.engine import Array
a=Array.open(sys.argv[3:])
a._fault_hook=lambda name: os._exit(88) if name=='rebuild_header_durable' else None
a.rebuild({0:sys.argv[1],1:sys.argv[2]})
"""
    result = subprocess.run([sys.executable, "-c", script, str(first), str(second),
                             *map(str, paths[2:])], timeout=30)
    assert result.returncode == 88
    assert inspect_member(first)["state"] == "ready"
    pending = inspect_member(second)
    assert pending["state"] == "rebuilding"
    with Array.open([first, *paths[2:]]) as reopened:
        reopened.rebuild({1: second})
        assert reopened.status["writable"]
        assert inspect_member(second)["member_id"] == pending["member_id"]
        assert exported(reopened, entry, tmp_path) == data
