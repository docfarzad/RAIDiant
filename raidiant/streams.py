"""Bounded array streams shared by network adapters.

A stream owns its storage-operation lease for its entire lifetime. Uploads are
invisible until finish(); close() without finish() always abandons the upload.
There is no host staging file and no buffer proportional to the file size.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
import uuid

from .engine import RaidError, UnrecoverableError, _valid_name


class UploadStream:
    def __init__(self, array, name, parent_id="root"):
        _valid_name(name)
        self.array, self.name = array, name
        self.closed = False
        self._lease = array._mutating()
        self._lease.__enter__()
        self.identifier = str(uuid.uuid4())
        self._token = uuid.UUID(self.identifier).bytes
        self._buffer = bytearray()
        self._batch_bytes = max(array.stripe_bytes, (16 * 1024**2 // array.stripe_bytes) * array.stripe_bytes)
        self._ordinal = self._total = 0
        self._digest = hashlib.sha256()
        try:
            if not array.get_entry(parent_id)["is_dir"]:
                raise RaidError("Destination must be a directory.")
            with array._db_lock:
                if array._db.execute("SELECT 1 FROM entries WHERE parent_id=? AND name=?", (parent_id, name)).fetchone():
                    raise RaidError("An entry with this name already exists; existing files are never overwritten.")
            array._append({"op": "entry", "entry": [self.identifier, parent_id, name, 0, 0, time.time(), 0, None]})
        except BaseException:
            self.closed = True
            self._lease.__exit__(None, None, None)
            raise

    def _flush_buffer(self):
        array = self.array
        offset = 0
        while offset < len(self._buffer):
            wanted = (len(self._buffer) - offset + array.stripe_bytes - 1) // array.stripe_bytes
            start, count = array._free_run(wanted)
            array._prepare_extent(self.identifier, start, count, self._ordinal)
            for delta in range(count):
                data = bytes(self._buffer[offset:offset + array.stripe_bytes])
                array._write_stripe(start + delta, self._token, data)
                self._digest.update(data)
                self._total += len(data)
                offset += len(data)
            array._flush()
            array._boundary("data_durable")
            array._append({"op": "extent", "extent": [self.identifier, start, count, self._ordinal]})
            self._ordinal += count
        self._buffer.clear()

    def write(self, data):
        if self.closed:
            raise ValueError("Upload is closed")
        view = memoryview(data).cast("B")
        length = len(view)
        try:
            while view:
                take = min(len(view), self._batch_bytes - len(self._buffer))
                self._buffer.extend(view[:take])
                view = view[take:]
                if len(self._buffer) == self._batch_bytes:
                    self._flush_buffer()
            return length
        except BaseException:
            with contextlib.suppress(Exception):
                self.abort()
            raise

    def finish(self):
        if self.closed:
            raise ValueError("Upload is closed")
        try:
            self._flush_buffer()
            self.array._boundary("before_file_commit")
            self.array._append({"op": "finish", "id": self.identifier,
                                "size": self._total, "digest": self._digest.hexdigest()})
            return self.identifier
        except BaseException:
            with contextlib.suppress(Exception):
                self.array._abandon_upload(self.identifier)
            raise
        finally:
            self._release()

    def _release(self):
        if not self.closed:
            self.closed = True
            self._buffer.clear()
            self._lease.__exit__(None, None, None)

    def abort(self):
        if not self.closed:
            try:
                self.array._abandon_upload(self.identifier)
            finally:
                self._release()

    close = abort

    def flush(self):
        # Network libraries may flush before deciding whether transfer succeeded.
        # Publication must be explicit through finish(), never implicit here.
        pass

    def tell(self):
        return self._total + len(self._buffer)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class DownloadStream:
    def __init__(self, array, entry_id):
        self.array = array
        self.closed = False
        self._lease = array._export_access()
        self._lease.__enter__()
        try:
            self.entry = array.get_entry(entry_id)
            if self.entry["is_dir"]:
                raise RaidError("Choose a file to download.")
            self.name = self.entry["name"]
            self.size = self.entry["size"]
            self._position = 0
            self._pending = b""
            self._iterator = self._chunks()
        except BaseException:
            self.closed = True
            self._lease.__exit__(None, None, None)
            raise

    def _chunks(self):
        remaining = self.size
        digest = hashlib.sha256()
        token = uuid.UUID(self.entry["id"]).bytes
        for extent in self.array._extents(self.entry["id"]):
            for stripe in range(extent["start"], extent["start"] + extent["count"]):
                data, _bad = self.array._read_stripe(stripe, token)
                for shard in data[:self.array.k]:
                    part = shard[:min(remaining, len(shard))]
                    digest.update(part)
                    remaining -= len(part)
                    if part:
                        yield part
        if remaining or digest.hexdigest() != self.entry["digest"]:
            self.array._integrity_failed = True
            self.array._issue(f"Content checksum failed for {self.name}.")
            raise UnrecoverableError("File checksum mismatch; download cannot be verified.")

    def read(self, size=65536):
        if self.closed:
            raise ValueError("Download is closed")
        if size is None or size < 0:
            raise ValueError("Array downloads require bounded read sizes")
        # The adapter uses 64 KiB reads. Refuse an accidental whole-file request.
        if size > 16 * 1024**2:
            raise ValueError("Read at most 16 MiB at a time")
        output = bytearray()
        try:
            while len(output) < size:
                if not self._pending:
                    try:
                        self._pending = next(self._iterator)
                    except StopIteration:
                        break
                take = min(size - len(output), len(self._pending))
                output.extend(self._pending[:take])
                self._pending = self._pending[take:]
                self._position += take
            return bytes(output)
        except BaseException:
            self.close()
            raise

    def tell(self):
        return self._position

    def close(self):
        if not self.closed:
            self.closed = True
            self._pending = b""
            self._iterator.close()
            self._lease.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def open_upload(array, name, parent_id="root"):
    return UploadStream(array, name, parent_id)


def open_download(array, entry_id):
    return DownloadStream(array, entry_id)
