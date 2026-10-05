import errno
import itertools
import os
from pathlib import Path
import subprocess
import sys

import pytest

from raidiant.engine import Array, RaidError, RecoveryRequired, UnrecoverableError, Cancelled


def members(tmp_path, n=5):
    return [tmp_path / f"member-{i}.r5m" for i in range(n)]


def create(tmp_path, n=5, m=2, size=2 * 1024**2):
    paths = members(tmp_path, n)
    return Array.create(paths, parity=m, member_size=size, chunk_size=4096), paths


def put(array, tmp_path, data=None, name="source.bin"):
    data = data if data is not None else os.urandom(65001)
    path = tmp_path / name
    path.write_bytes(data)
    array.upload_files([path])
    entry = next(e for e in array.list_dir() if e["name"] == name)
    return entry, data


def copy_out(array, entry, tmp_path):
    output = tmp_path / "exports"
    output.mkdir(exist_ok=True)
    return array.export(entry["id"], output).read_bytes()


def test_roundtrip_rename_delete_reopen(tmp_path):
    array, paths = create(tmp_path)
    with array:
        entry, data = put(array, tmp_path)
        array.rename(entry["id"], "../CON:bad/name?.txt")
        entry = array.get_entry(entry["id"])
        assert copy_out(array, entry, tmp_path) == data
        assert array.scrub()["damaged_shards"] == 0
    with Array.open(paths) as reopened:
        assert copy_out(reopened, entry, tmp_path) == data
        before = reopened.status["free"]
        reopened.delete(entry["id"])
        assert reopened.status["free"] > before
        assert reopened.list_dir() == []
    with Array.open(paths) as reopened:
        assert reopened.list_dir() == []


def test_any_two_missing_members(tmp_path):
    array, paths = create(tmp_path)
    with array:
        entry, data = put(array, tmp_path)
    for absent in itertools.combinations(range(5), 2):
        with Array.open([p for i, p in enumerate(paths) if i not in absent]) as degraded:
            assert degraded.status["state"] == "degraded"
            assert copy_out(degraded, entry, tmp_path) == data
            with pytest.raises(RaidError, match="read-only"):
                degraded.mkdir("forbidden")
    with pytest.raises(RaidError, match="At least 3"):
        Array.open(paths[:2])


def test_rebuild_two_members(tmp_path):
    array, paths = create(tmp_path)
    with array:
        entry, data = put(array, tmp_path)
    replacements = {0: tmp_path / "new0.r5m", 4: tmp_path / "new4.r5m"}
    with Array.open(paths[1:4]) as degraded:
        degraded.rebuild(replacements)
        assert degraded.status["writable"]
        assert copy_out(degraded, entry, tmp_path) == data
        degraded.mkdir("after rebuild")
    final = [replacements.get(i, path) for i, path in enumerate(paths)]
    with Array.open(final) as reopened:
        assert reopened.status["writable"]
        assert copy_out(reopened, entry, tmp_path) == data


def test_hidden_folders_empty_files_export_collision(tmp_path):
    array, _ = create(tmp_path)
    source = tmp_path / "folder"
    source.mkdir()
    (source / "empty").write_bytes(b"")
    (source / "data").write_bytes(b"abc")
    (source / ".hidden").write_bytes(b"secret")
    (source / ".hidden-dir").mkdir()
    (source / ".hidden-dir" / "data").write_bytes(b"secret")
    with array:
        result = array.upload_folder(source)
        folder_id = result["folder_id"]
        children = array.list_dir(folder_id)
        assert {e["name"] for e in children} == {"empty", "data"}
        array.rename(children[0]["id"], "a:b")
        array.rename(children[1]["id"], "a?b")
        export = array.export(folder_id, tmp_path)
        exported = [p for p in export.iterdir() if p.suffix != ".tsv"]
        assert len(exported) == 2
        assert {p.read_bytes() for p in exported} == {b"", b"abc"}


def test_corrupt_shards_detect_repair_and_limits(tmp_path):
    array, paths = create(tmp_path)
    with array:
        entry, data = put(array, tmp_path)
        offset = array._stripe_offset(0) + 128
        for index in (0, 1):
            array._files[index].seek(offset)
            array._files[index].write(b"corrupt")
        assert copy_out(array, entry, tmp_path) == data
        assert not array.status["writable"]
        assert array.repair()["repaired_shards"] == 2
        assert array.status["writable"]
        assert array.scrub()["damaged_shards"] == 0
        for index in (0, 1, 2):
            array._files[index].seek(offset)
            array._files[index].write(b"corrupt")
        with pytest.raises(UnrecoverableError):
            copy_out(array, entry, tmp_path)


@pytest.mark.parametrize("boundary", ["stripe_written", "data_durable", "before_file_commit", "metadata_member_written"])
def test_interrupted_import_preserves_committed_file(tmp_path, boundary):
    array, paths = create(tmp_path)
    old, old_data = put(array, tmp_path, name="old.bin")
    source = tmp_path / "new.bin"
    source.write_bytes(os.urandom(40000))
    def failure(name):
        if name == boundary:
            raise OSError(errno.ENOSPC, "Injected full disk")
    array._fault_hook = failure
    with pytest.raises((RaidError, OSError)):
        array.upload_files([source])
    array._fault_hook = None
    array.close()
    with Array.open(paths) as reopened:
        if reopened.status["state"] == "recovery_required":
            reopened.repair()
        assert copy_out(reopened, old, tmp_path) == old_data
        assert reopened.status["writable"]


def test_metadata_compaction_and_small_array_repair(tmp_path):
    array, paths = create(tmp_path)
    with array:
        entry, data = put(array, tmp_path)
        array.repair()
        epoch = array.h["epoch"]
        for i in range(180):
            array.rename(entry["id"], "x" * 1024 + str(i))
        assert array.h["epoch"] > epoch
    with Array.open(paths) as reopened:
        assert copy_out(reopened, entry, tmp_path) == data


def test_cancel_upload_reclaims_staging(tmp_path):
    array, paths = create(tmp_path)
    with array:
        source = tmp_path / "large.bin"
        source.write_bytes(os.urandom(400000))
        free = array.status["free"]
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls > 4
        with pytest.raises(Cancelled):
            array.upload_files([source], cancel=cancel)
        assert array.list_dir() == []
        assert array.status["free"] == free
    with Array.open(paths) as reopened:
        assert reopened.list_dir() == []


def test_process_crash_before_file_commit(tmp_path):
    array, paths = create(tmp_path)
    with array:
        entry, data = put(array, tmp_path, name="old.bin")
    source = tmp_path / "uncommitted.bin"
    source.write_bytes(os.urandom(70000))
    script = """
import os,sys
from raidiant.engine import Array
a=Array.open(sys.argv[2:])
a._fault_hook=lambda boundary: os._exit(77) if boundary=='before_file_commit' else None
a.upload_files([sys.argv[1]])
"""
    result = subprocess.run([sys.executable, "-c", script, str(source), *map(str, paths)], timeout=30)
    assert result.returncode == 77
    with Array.open(paths[:3]) as reopened:
        assert reopened.status["state"] == "recovery_required"
        reopened.repair()
        assert [e["name"] for e in reopened.list_dir()] == ["old.bin"]
        assert copy_out(reopened, entry, tmp_path) == data


def test_member_locks_duplicate_and_wrong_array(tmp_path):
    array, paths = create(tmp_path)
    with array:
        with pytest.raises((RaidError, OSError)):
            Array.open(paths)
    with pytest.raises(RaidError, match="same physical"):
        Array.open([paths[0], paths[0], paths[1]])
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other, other_paths = create(other_dir)
    other.close()
    with pytest.raises(RaidError, match="different arrays"):
        Array.open([*paths[:3], other_paths[0]])


def test_fragmented_free_space_is_reused_and_survives_reopen(tmp_path):
    array, paths = create(tmp_path)
    with array:
        a, _ = put(array, tmp_path, b"a" * 100000, "a.bin")
        b, b_data = put(array, tmp_path, b"b" * 100000, "b.bin")
        c, _ = put(array, tmp_path, b"c" * 100000, "c.bin")
        free = array.status["free"]
        array.delete(a["id"])
        array.delete(c["id"])
        assert array.status["free"] > free
        d, d_data = put(array, tmp_path, b"d" * 250000, "d.bin")
        assert copy_out(array, d, tmp_path) == d_data
    with Array.open(paths) as reopened:
        assert copy_out(reopened, b, tmp_path) == b_data
        assert copy_out(reopened, d, tmp_path) == d_data
        for entry in reopened.list_dir():
            reopened.delete(entry["id"])
        assert reopened.status["free"] == reopened.status["capacity"]


def test_large_stream_coalesces_allocation_metadata(tmp_path):
    paths = members(tmp_path)
    with Array.create(paths, parity=2, member_size=16 * 1024**2) as array:
        source = tmp_path / "stream.bin"
        block = bytes(range(256)) * 4096
        with source.open("wb") as file:
            for _ in range(20):
                file.write(block)
        array.upload_files([source])
        # A multi-batch sequential stream must not create one permanent extent
        # per 16 MiB batch; catalog growth follows fragmentation, not file size.
        assert array._db.execute("SELECT COUNT(*) FROM extents").fetchone()[0] == 1
        assert array.scrub()["damaged_shards"] == 0
        array.repair()
        entry = array.list_dir()[0]
        output = tmp_path / "export"
        output.mkdir()
        copied = array.export(entry["id"], output)
        import hashlib
        with source.open("rb") as original, copied.open("rb") as exported:
            assert hashlib.file_digest(original, "sha256").digest() == hashlib.file_digest(exported, "sha256").digest()
