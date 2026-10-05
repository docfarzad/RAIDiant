import errno
import os
from pathlib import Path
import subprocess
import sys

import pytest

from raidiant import exporting
from raidiant.exporting import ExportStage, cleanup_orphan_exports
from raidiant.host import durable_flush


@pytest.fixture(autouse=True)
def local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIDIANT_STATE_DIR", str(tmp_path / "state"))


def test_atomic_fallback_and_no_overwrite(tmp_path, monkeypatch):
    def unsupported(*args):
        raise OSError(errno.ENOTSUP, "No hard links here")
    monkeypatch.setattr(exporting.os, "link", unsupported)
    destination = tmp_path / "finished.bin"
    with ExportStage(tmp_path) as stage:
        stage.target.write(b"complete data")
        durable_flush(stage.target)
        stage.publish(destination)
    assert destination.read_bytes() == b"complete data"
    with pytest.raises(FileExistsError):
        with ExportStage(tmp_path) as stage:
            stage.target.write(b"must not overwrite")
            durable_flush(stage.target)
            stage.publish(destination)
    assert destination.read_bytes() == b"complete data"
    assert not list(tmp_path.glob(".raidiant-export-*"))


def test_live_export_is_not_cleaned(tmp_path):
    with ExportStage(tmp_path) as stage:
        stage.target.write(b"working")
        assert cleanup_orphan_exports() == []
        assert stage.path.exists()
    assert not stage.path.exists()


def test_replaced_staging_file_is_not_published_or_deleted(tmp_path):
    stage = ExportStage(tmp_path)
    stage.target.write(b"verified")
    stage.target.close()
    original = tmp_path / "original.part"
    stage.path.rename(original)
    stage.path.write_bytes(b"unrelated file")
    with pytest.raises(ValueError, match="replaced"):
        stage.publish(tmp_path / "final.bin")
    with pytest.raises(ValueError, match="identity changed"):
        stage.__exit__(None, None, None)
    assert not (tmp_path / "final.bin").exists()
    assert stage.path.read_bytes() == b"unrelated file"
    cleanup_orphan_exports()
    assert stage.path.read_bytes() == b"unrelated file"


def test_manifest_is_retired_before_lock_is_released(tmp_path, monkeypatch):
    with ExportStage(tmp_path) as stage:
        original = stage._lock.release
        def release():
            assert not (stage.directory / "manifest.json").exists()
            original()
        monkeypatch.setattr(stage._lock, "release", release)


def test_killed_export_cleanup_preserves_existing_files(tmp_path):
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"mine")
    script = """
import os,sys
from raidiant.exporting import ExportStage
from raidiant.host import durable_flush
s=ExportStage(sys.argv[1])
s.target.write(b'partial' * 10000)
durable_flush(s.target)
os._exit(82)
"""
    process = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=20)
    assert process.returncode == 82
    assert len(list(tmp_path.glob(".raidiant-export-*"))) == 1
    assert len(cleanup_orphan_exports()) == 1
    assert not list(tmp_path.glob(".raidiant-export-*"))
    assert destination.read_bytes() == b"mine"


def test_killed_after_publication_retains_complete_export(tmp_path):
    destination = tmp_path / "ready.bin"
    script = """
import os,sys
from pathlib import Path
from raidiant.exporting import ExportStage
from raidiant.host import durable_flush
s=ExportStage(sys.argv[1])
s.target.write(b'complete')
durable_flush(s.target)
s.publish(Path(sys.argv[1])/'ready.bin')
os._exit(84)
"""
    process = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=20)
    assert process.returncode == 84
    cleanup_orphan_exports()
    assert destination.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".raidiant-export-*"))
