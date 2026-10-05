import hashlib
import json
import threading

import pytest

from raidiant.engine import Array, RaidError, UnrecoverableError


def make_array(tmp_path):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(5)]
    array = Array.create(paths, 2, 2 * 1024**2, chunk_size=4096)
    for name in ("a.bin", "b.bin"):
        source = tmp_path / name
        source.write_bytes(name.encode() * 6000)
        array.upload_files([source])
    return array, paths


def test_verify_and_repair_do_not_hide_wrong_whole_file_digest(tmp_path):
    array, paths = make_array(tmp_path)
    with array:
        entry = array.list_dir()[0]
        # Simulate a self-consistent metadata record whose expected file digest
        # disagrees with otherwise valid shards/parity.
        array._db.execute("UPDATE entries SET digest=? WHERE id=?", (hashlib.sha256(b"wrong").hexdigest(), entry["id"]))
        array._checkpoint()
        result = array.scrub()
        assert result["damaged_file_count"] == 1
        assert result["files_checked"] == 2
        assert array.status["integrity_failed"]
        with pytest.raises(UnrecoverableError, match="could not recover 1"):
            array.repair()
        assert not array.status["writable"]
        assert array.status["readable"]
        good = array.list_dir()[1]
        output = tmp_path / "salvage"
        output.mkdir()
        assert array.export(good["id"], output).read_bytes() == (tmp_path / good["name"]).read_bytes()
        report = array._last_verification["report_path"]
        with open(report, encoding="utf-8") as file:
            assert any(json.loads(line)["type"] == "damaged_file" for line in file)


def test_verify_reports_all_damaged_files_and_keeps_good_file_exportable(tmp_path):
    array, _ = make_array(tmp_path)
    with array:
        entries = array.list_dir()
        for entry in entries:
            first = next(array._extents(entry["id"]))["start"]
            for index in (0, 1, 2):
                file = array._files[index]
                file.seek(array._stripe_offset(first) + 128)
                file.write(b"damage")
        result = array.scrub()
        assert result["damaged_file_count"] == 2
        assert result["files_checked"] == 2
        assert {item["file_id"] for item in result["damaged_files"]} == {item["id"] for item in entries}
        assert not array.status["writable"]


def test_idle_probe_detects_damaged_header_copies(tmp_path):
    array, _ = make_array(tmp_path)
    with array:
        array._files[0].seek(0)
        array._files[0].write(bytes(8192))
        status = array.health_check()
        assert status["state"] == "recovery_required"
        assert 0 in status["bad_members"]
        result = array.scrub()
        assert result["damaged_file_count"] == 0
        assert not array.status["writable"]


def test_export_excludes_mutations_and_close(tmp_path, monkeypatch):
    array, _ = make_array(tmp_path)
    entry = array.list_dir()[0]
    output = tmp_path / "out"
    output.mkdir()
    entered, release = threading.Event(), threading.Event()
    original = array._export_file
    errors = []
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(array, "_export_file", delayed)
    def work():
        try:
            array.export(entry["id"], output)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(RaidError):
            array.delete(entry["id"])
        with pytest.raises(RaidError):
            array.close()
    finally:
        release.set()
        thread.join(10)
        array.close()
    assert not errors
    assert (output / entry["name"]).read_bytes() == (tmp_path / entry["name"]).read_bytes()


def test_export_runs_during_rebuild(tmp_path, monkeypatch):
    array, paths = make_array(tmp_path)
    entry = array.list_dir()[0]
    array.close()
    array = Array.open(paths[1:])
    output = tmp_path / "out"
    output.mkdir()
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = array._begin_rebuild_reads
    def begin():
        original()
        entered.set()
        assert release.wait(10)
    monkeypatch.setattr(array, "_begin_rebuild_reads", begin)
    def rebuild():
        try:
            array.rebuild({0: tmp_path / "replacement.r5m"})
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=rebuild)
    thread.start()
    try:
        assert entered.wait(10)
        copied = array.export(entry["id"], output)
        assert copied.read_bytes() == (tmp_path / entry["name"]).read_bytes()
        with pytest.raises(RaidError):
            array.mkdir("unsafe")
    finally:
        release.set()
        thread.join(15)
        array.close()
    assert not thread.is_alive()
    assert not errors
