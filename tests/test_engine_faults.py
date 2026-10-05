import errno
import os
from pathlib import Path
import subprocess
import sys

import pytest

from raidiant import engine
from raidiant.engine import Array, Cancelled, RaidError, RecoveryRequired, inspect_member


def setup_array(tmp_path, size=2 * 1024**2):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(5)]
    source = tmp_path / "file.bin"
    data = bytes(range(256)) * 1024
    source.write_bytes(data)
    array = Array.create(paths, parity=2, member_size=size, chunk_size=4096)
    array.upload_files([source])
    return array, paths, source, array.list_dir()[0], data


def export_bytes(array, entry, tmp_path):
    output = tmp_path / "exports"
    output.mkdir(exist_ok=True)
    return array.export(entry["id"], output).read_bytes()


def test_disk_full_mid_shard_does_not_damage_committed_files(tmp_path, monkeypatch):
    array, paths, source, entry, data = setup_array(tmp_path)
    original = engine._write_all
    hit = False
    def fail(file, block):
        nonlocal hit
        if not hit and file is array._files[1] and len(block) > 4096:
            hit = True
            file.write(block[:2000])
            raise OSError(errno.ENOSPC, "Disk full after a short write")
        return original(file, block)
    monkeypatch.setattr(engine, "_write_all", fail)
    with pytest.raises(RecoveryRequired):
        array.upload_files([source])
    assert not array.status["writable"]
    monkeypatch.setattr(engine, "_write_all", original)
    array.repair()
    assert export_bytes(array, entry, tmp_path) == data
    array.close()


def test_flush_error_freezes_and_recovery_retains_old_data(tmp_path, monkeypatch):
    array, paths, source, entry, data = setup_array(tmp_path)
    original = engine.durable_flush
    def fail(file):
        if file is array._files[2]:
            raise OSError(errno.EIO, "Injected persistence failure")
        original(file)
    monkeypatch.setattr(engine, "durable_flush", fail)
    with pytest.raises(RecoveryRequired):
        array.upload_files([source])
    assert not array.status["writable"]
    with pytest.raises(RaidError):
        array.delete(entry["id"])
    monkeypatch.setattr(engine, "durable_flush", original)
    array.repair()
    assert export_bytes(array, entry, tmp_path) == data
    array.close()


def test_metadata_full_can_still_delete_and_reuse(tmp_path):
    array, paths, source, entry, data = setup_array(tmp_path)
    with array:
        created = []
        for i in range(100):
            try:
                created.append(array.mkdir(str(i) + "a" * 3500))
            except RaidError as error:
                assert "metadata" in str(error).lower()
                break
        else:
            pytest.fail("Test did not fill the metadata region")
        assert created
        array.delete(created[0])
        array.mkdir("reclaimed" + "b" * 3000)
        assert export_bytes(array, entry, tmp_path) == data
    with Array.open(paths) as reopened:
        assert reopened.status["writable"]


@pytest.mark.parametrize("boundary", ["checkpoint_data_durable", "checkpoint_header_written"])
def test_process_crash_during_checkpoint(tmp_path, boundary):
    array, paths, source, entry, data = setup_array(tmp_path)
    array.close()
    script = """
import os,sys
from raidiant.engine import Array
a=Array.open(sys.argv[2:])
a._fault_hook=lambda name: os._exit(83) if name==sys.argv[1] else None
a._checkpoint()
"""
    result = subprocess.run([sys.executable, "-c", script, boundary, *map(str, paths)], timeout=30)
    assert result.returncode == 83
    with Array.open(paths) as reopened:
        if boundary == "checkpoint_header_written":
            # A complete bank on every member is not enough: every root header
            # must also be advanced before more writes are acknowledged.
            assert reopened.status["state"] == "recovery_required"
            with pytest.raises(RaidError):
                reopened.mkdir("unsafe")
            reopened.repair()
        assert export_bytes(reopened, entry, tmp_path) == data
        reopened.mkdir("acknowledged after recovery")
    # Drop the member which had the newest header at the interruption.
    with Array.open(paths[1:4]) as degraded:
        assert any(e["name"] == "acknowledged after recovery" for e in degraded.list_dir())
        assert export_bytes(degraded, entry, tmp_path) == data


def test_resume_cancelled_rebuild_and_replace_present_member(tmp_path):
    array, paths, source, entry, data = setup_array(tmp_path)
    array.close()
    replacement = tmp_path / "replacement.r5m"
    with Array.open(paths[1:]) as degraded:
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls > 8
        with pytest.raises(Cancelled):
            degraded.rebuild({0: replacement}, cancel=cancel)
        assert replacement.exists()
        assert inspect_member(replacement)["state"] == "rebuilding"
        with pytest.raises(RaidError, match="incomplete"):
            Array.open([replacement, *paths[1:]])
        degraded.rebuild({0: replacement})
        assert degraded.status["writable"]
        assert export_bytes(degraded, entry, tmp_path) == data
        moved = tmp_path / "moved-member2.r5m"
        degraded.rebuild({2: moved})
        assert paths[2].exists(), "Replacement must preserve original files"
        assert degraded.status["writable"]


def test_rebuild_process_crash_resume(tmp_path):
    array, paths, source, entry, data = setup_array(tmp_path)
    array.close()
    replacement = tmp_path / "replacement.r5m"
    script = """
import os,sys
from raidiant.engine import Array
a=Array.open(sys.argv[2:])
a._fault_hook=lambda name: os._exit(91) if name=='rebuild_checkpoint_durable' else None
a.rebuild({0:sys.argv[1]})
"""
    result = subprocess.run([sys.executable, "-c", script, str(replacement), *map(str, paths[1:])], timeout=30)
    assert result.returncode == 91
    assert inspect_member(replacement)["rebuild_next"] > 0
    with Array.open(paths[1:]) as degraded:
        degraded.rebuild({0: replacement})
        assert degraded.status["writable"]
        assert export_bytes(degraded, entry, tmp_path) == data


def test_corrupt_metadata_repaired_from_other_members(tmp_path):
    array, paths, source, entry, data = setup_array(tmp_path)
    tail = array._tail
    bank = 8192 + array.h["bank"] * array.h["bank_size"]
    array.close()
    with open(paths[0], "r+b", buffering=0) as target:
        target.seek(bank + tail - 5)
        target.write(b"BAD!!")
    with Array.open(paths) as recovered:
        assert recovered.status["state"] == "recovery_required"
        recovered.repair()
        assert export_bytes(recovered, entry, tmp_path) == data


def test_stale_member_requires_repair(tmp_path):
    import shutil
    array, paths, source, entry, data = setup_array(tmp_path)
    array.close()
    stale = tmp_path / "stale.r5m"
    shutil.copyfile(paths[0], stale)
    with Array.open(paths) as array:
        array.rename(entry["id"], "new-name")
    with Array.open([stale, *paths[1:]]) as array:
        assert array.status["state"] == "recovery_required"
        array.repair()
        assert array.get_entry(entry["id"])["name"] == "new-name"
        assert export_bytes(array, entry, tmp_path) == data
