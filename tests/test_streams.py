import hashlib
import os

import pytest

from raidiant.engine import Array, RaidError, UnrecoverableError
from raidiant.streams import open_download, open_upload


def create(tmp_path, **kwargs):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(3)]
    return Array.create(paths, 1, 32 * 1024**2, chunk_size=4096, **kwargs), paths


def read_all(array, identifier):
    with open_download(array, identifier) as stream:
        result = bytearray()
        while part := stream.read(3101):
            result.extend(part)
        return bytes(result)


@pytest.mark.parametrize("preallocate", [False, True])
def test_stream_commit_and_abort(tmp_path, preallocate):
    array, paths = create(tmp_path, preallocate=preallocate)
    with array:
        data = os.urandom(85001)
        with open_upload(array, "document") as target:
            target.write(data[:5])
            target.write(data[5:])
            assert array.list_dir() == []
            with pytest.raises(RaidError, match="operation"):
                array.mkdir("while-uploading")
            identifier = target.finish()
        assert read_all(array, identifier) == data
        sizes = [p.stat().st_size for p in paths]
        # Cross a flush boundary so abort really rolls back member bytes.
        with open_upload(array, "unfinished") as target:
            target.write(b"x" * (17 * 1024**2))
        assert [e["name"] for e in array.list_dir()] == ["document"]
        assert [p.stat().st_size for p in paths] == sizes
        assert read_all(array, identifier) == data
        assert array.status["writable"]
    with Array.open(paths) as reopened:
        assert read_all(reopened, identifier) == data


def test_zero_length_and_exclusive_read(tmp_path):
    array, _ = create(tmp_path)
    with array:
        with open_upload(array, "empty") as writer:
            identifier = writer.finish()
        with open_download(array, identifier) as source:
            assert source.size == 0
            assert source.read(100) == b""
            with pytest.raises(RaidError, match="operation"):
                array.delete(identifier)
        array.delete(identifier)


def test_degraded_download_and_duplicate_refusal(tmp_path):
    array, paths = create(tmp_path)
    with array:
        with open_upload(array, "a/b%name") as writer:
            writer.write(b"verified bytes")
            identifier = writer.finish()
        with pytest.raises(RaidError, match="already exists"):
            open_upload(array, "a/b%name")
        assert array.status["writable"]
    with Array.open(paths[:2]) as degraded:
        assert read_all(degraded, identifier) == b"verified bytes"
        with pytest.raises(RaidError, match="read-only"):
            open_upload(degraded, "forbidden")


def test_download_rejects_unverified_whole_file(tmp_path):
    array, _ = create(tmp_path)
    with array:
        with open_upload(array, "damaged") as writer:
            writer.write(b"hello")
            identifier = writer.finish()
        # Independently valid shard payloads cannot bypass the file checksum.
        array._db.execute("UPDATE entries SET digest=? WHERE id=?", (hashlib.sha256(b"wrong").hexdigest(), identifier))
        with pytest.raises(UnrecoverableError, match="checksum"):
            read_all(array, identifier)
        assert not array.status["writable"]


def test_stream_failure_releases_lease(tmp_path):
    array, _ = create(tmp_path)
    with array:
        with pytest.raises(RaidError, match="no longer exists"):
            open_upload(array, "no parent", "absent")
        array.mkdir("lease released")
        with pytest.raises(RaidError, match="no longer exists"):
            open_download(array, "absent")
        array.mkdir("reader released")
