"""Ready replacements remain recoverable after interrupted roster publication."""
import hashlib
import os
import subprocess
import sys

import pytest

from raidiant import engine
from raidiant.engine import Array, RaidError, inspect_member


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIDIANT_STATE_DIR", str(tmp_path / "state"))


def interrupted_rebuild(tmp_path, *, boundary="replacement_ready_durable", preallocate=False):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(3)]
    data = bytes(range(251)) * 611
    source = tmp_path / "committed.bin"
    source.write_bytes(data)
    with Array.create(paths, 1, 4 * 1024**2, chunk_size=4096, preallocate=preallocate) as array:
        array.upload_files([source])
        entry = array.list_dir()[0]
    original = hashlib.sha256(paths[1].read_bytes()).digest()
    replacement = tmp_path / "member-1-new.r5m"
    script = r'''
import os, sys
from raidiant.engine import Array
a = Array.open(sys.argv[3:])
a._fault_hook = lambda name: os._exit(81) if name == sys.argv[1] else None
a.rebuild({1: sys.argv[2]})
'''
    result = subprocess.run([sys.executable, "-c", script, boundary, str(replacement),
                             str(paths[0]), str(paths[2])], capture_output=True, text=True, timeout=30)
    assert result.returncode == 81, result.stderr
    assert inspect_member(replacement)["state"] == "ready"
    assert hashlib.sha256(paths[1].read_bytes()).digest() == original
    return paths, replacement, entry, data, original


def check_content(array, entry, data, tmp_path):
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    assert array.export(entry["id"], output).read_bytes() == data


@pytest.mark.parametrize("preallocate", [False, True])
@pytest.mark.parametrize("legacy_proof", [False, True])
def test_ready_target_can_finish_rebuild_from_survivors(tmp_path, preallocate, legacy_proof):
    paths, replacement, entry, data, original = interrupted_rebuild(tmp_path, preallocate=preallocate)
    if legacy_proof:
        # Version 0.2 published ready targets without retaining source tags.
        header = inspect_member(replacement)
        for key in ("rebuild_source_hash", "rebuild_source_epoch", "rebuild_next"):
            header.pop(key, None)
        with replacement.open("r+b", buffering=0) as file:
            file.write(engine._header_bytes(header) * 2)
    with Array.open([paths[0], paths[2]]) as array:
        array.rebuild({1: replacement})
        assert array.status["writable"]
        check_content(array, entry, data, tmp_path)
    assert hashlib.sha256(paths[1].read_bytes()).digest() == original
    with Array.open([paths[0], replacement, paths[2]]) as array:
        assert array.status["writable"]


@pytest.mark.parametrize("boundary", ["replacement_ready_durable", "membership_header_durable"])
def test_mixed_generations_open_and_repair(tmp_path, boundary):
    paths, replacement, entry, data, original = interrupted_rebuild(tmp_path, boundary=boundary)
    with Array.open([paths[0], replacement, paths[2]]) as array:
        assert array.status["state"] == "recovery_required"
        array.repair()
        assert array.status["writable"]
        check_content(array, entry, data, tmp_path)
    assert hashlib.sha256(paths[1].read_bytes()).digest() == original


def test_partial_survivor_publication_can_resume_from_ready_target(tmp_path):
    paths, replacement, entry, data, original = interrupted_rebuild(tmp_path, boundary="membership_header_durable")
    with Array.open([paths[0], paths[2]]) as array:
        array.rebuild({1: replacement})
        assert array.status["writable"]
        check_content(array, entry, data, tmp_path)


def test_ready_target_with_additional_commits_is_preserved(tmp_path):
    paths, replacement, entry, data, original = interrupted_rebuild(tmp_path)
    header = inspect_member(replacement)
    with Array.open([paths[0], paths[2]]) as array:
        extra, _digest = engine._record(array._seq + 1, array._hash,
            {"op": "rename", "id": entry["id"], "name": "newer commit", "modified": 123})
        with replacement.open("r+b", buffering=0) as file:
            file.seek(8192 + header["bank"] * header["bank_size"] + array._tail)
            file.write(extra + bytes(engine.LOG_STRUCT.size))
        before = hashlib.sha256(replacement.read_bytes()).digest()
        with pytest.raises(RaidError, match="different transaction history"):
            array.rebuild({1: replacement})
        assert hashlib.sha256(replacement.read_bytes()).digest() == before


def test_unrelated_ready_original_cannot_be_adopted_as_replacement(tmp_path):
    paths, replacement, entry, data, original = interrupted_rebuild(tmp_path)
    with Array.open([paths[0], paths[2]]) as array:
        with pytest.raises(RaidError, match="not a matching interrupted replacement"):
            array.rebuild({1: paths[1]})
    assert hashlib.sha256(paths[1].read_bytes()).digest() == original
