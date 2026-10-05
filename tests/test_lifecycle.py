"""Process termination and ownership tests for creation/session recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from raidiant import lifecycle
from raidiant.engine import Array, Cancelled, RaidError, inspect_member


@pytest.fixture(autouse=True)
def private_recovery_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIDIANT_STATE_DIR", str(tmp_path / "recovery-state"))


def _crash_creation(tmp_path, stage, published=1):
    paths = [tmp_path / f"member-{index}.r5m" for index in range(5)]
    script = r'''
import os, sys
from raidiant.engine import Array, HEADER_MAGIC
from raidiant import lifecycle
paths = sys.argv[3:]
stage, limit = sys.argv[1], int(sys.argv[2])
if stage == "allocation":
    original = lifecycle.allocate_file
    def interrupted(file, size, progress=None):
        original(file, size, progress=progress)
        os._exit(83)
    lifecycle.allocate_file = interrupted
else:
    original = lifecycle.durable_flush
    published = 0
    def interrupted(file):
        global published
        original(file)
        if str(file.name) in paths:
            position = file.tell()
            file.seek(0)
            ready = file.read(8) == HEADER_MAGIC
            file.seek(position)
            if ready:
                published += 1
                if published == limit:
                    os._exit(83)
    lifecycle.durable_flush = interrupted
Array.create(paths, parity=2, member_size=2 * 1024**2, chunk_size=4096)
'''
    result = subprocess.run([sys.executable, "-c", script, stage, str(published), *map(str, paths)],
                            timeout=30, capture_output=True, text=True)
    assert result.returncode == 83, result.stdout + result.stderr
    pending = lifecycle.pending_creations()
    assert len(pending) == 1
    return paths, pending[0]


def test_killed_allocation_resumes_and_opens(tmp_path):
    paths, pending = _crash_creation(tmp_path, "allocation")
    assert pending["phase"] == "allocating"
    assert pending["can_discard"]
    assert paths[0].stat().st_size == 8192 + 2 * 65536
    assert lifecycle.resume_creation(pending["id"]) == paths
    assert not lifecycle.pending_creations()
    with Array.open(paths) as array:
        assert array.status["writable"]
        assert array.h["uuid"] == pending["array_uuid"]
        array.mkdir("creation recovered")


def test_killed_allocation_can_be_removed(tmp_path):
    paths, pending = _crash_creation(tmp_path, "allocation")
    assert set(lifecycle.discard_creation(pending["id"])) == set(map(str, paths))
    assert not any(path.exists() for path in paths)
    assert not lifecycle.pending_creations()


def test_interrupted_removal_resumes_cleanup_without_recreating_members(tmp_path):
    paths, pending = _crash_creation(tmp_path, "allocation")
    script = r'''
import os, sys
from pathlib import Path
from raidiant import lifecycle
original = Path.unlink
def interrupted(path, *args, **kwargs):
    result = original(path, *args, **kwargs)
    if path.suffix == ".r5m":
        os._exit(85)
    return result
Path.unlink = interrupted
lifecycle.discard_creation(sys.argv[1])
'''
    result = subprocess.run([sys.executable, "-c", script, pending["id"]],
                            timeout=30, capture_output=True, text=True)
    assert result.returncode == 85, result.stderr
    pending = lifecycle.pending_creations()[0]
    assert pending["phase"] == "discarding"
    assert pending["can_discard"] and not pending["can_resume"]
    with pytest.raises(lifecycle.LifecycleError, match="Removal"):
        lifecycle.resume_creation(pending["id"])
    lifecycle.discard_creation(pending["id"])
    assert not any(path.exists() for path in paths)
    assert not lifecycle.pending_creations()


def test_publishing_creation_is_preserved_and_resumed(tmp_path):
    paths, pending = _crash_creation(tmp_path, "publication")
    original_header = inspect_member(paths[0])
    assert pending["phase"] == "publishing"
    assert not pending["can_discard"]
    with pytest.raises(lifecycle.LifecycleError, match="publication"):
        lifecycle.discard_creation(pending["id"])
    assert all(path.exists() for path in paths)
    lifecycle.resume_creation(pending["id"])
    assert inspect_member(paths[0]) == original_header
    with Array.open(paths) as array:
        assert array.status["writable"]


def test_completed_but_unretired_creation_preserves_later_data(tmp_path):
    paths, pending = _crash_creation(tmp_path, "publication", published=5)
    with Array.open(paths) as array:
        array.mkdir("already in use")
        with pytest.raises(OSError):
            lifecycle.resume_creation(pending["id"])
        assert any(entry["name"] == "already in use" for entry in array.list_dir())
    lifecycle.resume_creation(pending["id"])
    with Array.open(paths) as array:
        assert any(entry["name"] == "already in use" for entry in array.list_dir())


def test_replaced_member_is_never_deleted_or_overwritten(tmp_path):
    paths, pending = _crash_creation(tmp_path, "allocation")
    paths[0].rename(tmp_path / "original-member.r5m")
    paths[0].write_bytes(b"unrelated replacement file")
    for operation in (lifecycle.discard_creation, lifecycle.resume_creation):
        with pytest.raises(lifecycle.LifecycleError, match="identity changed"):
            operation(pending["id"])
    assert paths[0].read_bytes() == b"unrelated replacement file"
    assert all(path.exists() for path in paths)


def test_changed_owner_token_refuses_cleanup(tmp_path):
    paths, pending = _crash_creation(tmp_path, "allocation")
    with paths[0].open("r+b") as file:
        file.write(b"not our member".ljust(8192, b"!"))
    with pytest.raises(lifecycle.LifecycleError, match="ownership"):
        lifecycle.discard_creation(pending["id"])
    assert all(path.exists() for path in paths)


def test_live_creation_cannot_be_claimed_or_listed(tmp_path):
    paths, pending = _crash_creation(tmp_path, "allocation")
    job = lifecycle.CreationJob.claim(pending["id"])
    try:
        assert lifecycle.pending_creations() == []
        with pytest.raises(lifecycle.LifecycleError, match="active"):
            lifecycle.discard_creation(pending["id"])
    finally:
        job.close()
    lifecycle.discard_creation(pending["id"])


def test_ordinary_cancel_removes_members_and_recovery_record(tmp_path):
    paths = [tmp_path / f"cancel-{index}.r5m" for index in range(3)]
    with pytest.raises(Cancelled):
        Array.create(paths, parity=1, member_size=2 * 1024**2, cancel=lambda: True)
    assert not any(path.exists() for path in paths)
    assert lifecycle.pending_creations() == []


def test_existing_destination_is_rejected_before_creating_any_members(tmp_path):
    paths = [tmp_path / f"existing-{index}.r5m" for index in range(3)]
    paths[-1].write_bytes(b"original")
    with pytest.raises(RaidError, match="already exists"):
        Array.create(paths, parity=1, member_size=2 * 1024**2)
    assert paths[-1].read_bytes() == b"original"
    assert not paths[0].exists() and not paths[1].exists()
    assert lifecycle.pending_creations() == []


def test_scratch_cleanup_skips_live_owner_then_removes_after_crash(tmp_path):
    live = lifecycle.ManagedScratch()
    live_path = Path(live.name)
    (live_path / "catalog.sqlite").write_bytes(b"active catalog")
    script = r'''
import os
from pathlib import Path
from raidiant.lifecycle import ManagedScratch
scratch = ManagedScratch()
(Path(scratch.name) / "catalog.sqlite").write_bytes(b"abandoned catalog")
print(scratch.name, flush=True)
os._exit(84)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 84, result.stderr
    abandoned = Path(result.stdout.strip())
    try:
        assert lifecycle.cleanup_orphan_scratch() == [str(abandoned)]
        assert not abandoned.exists()
        assert live_path.exists()
        assert (live_path / "catalog.sqlite").read_bytes() == b"active catalog"
    finally:
        live.cleanup()
    assert not live_path.exists()
    assert lifecycle.cleanup_orphan_scratch() == []


def test_scratch_cleanup_preserves_unmarked_and_changed_directories(tmp_path):
    root = lifecycle._area("scratch")
    unrelated = root / str(uuid.uuid4())
    unrelated.mkdir()
    (unrelated / "important.txt").write_text("keep", encoding="utf-8")
    scratch = lifecycle.ManagedScratch()
    directory = Path(scratch.name)
    marker = directory / "owner.json"
    owner = json.loads(marker.read_text(encoding="utf-8"))
    owner["identity"] = [-1, -1]
    marker.write_text(json.dumps(owner), encoding="utf-8")
    scratch._lock.release()
    scratch._file.close()
    scratch._closed = True
    assert lifecycle.cleanup_orphan_scratch() == []
    assert directory.exists()
    assert (unrelated / "important.txt").read_text(encoding="utf-8") == "keep"
