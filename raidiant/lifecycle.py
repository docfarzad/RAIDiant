"""Crash tracking for member creation and disposable session catalogs.

Creation intents are durable before large allocations.  File identities plus
random ownership tokens prevent cleanup from deleting unrelated replacement
files.  Once any ready member can exist, recovery finishes publication instead
of deleting an array that might already have been opened.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid

from .host import MemberLock, allocate_file, durable_flush, sync_directory

_OWNER_MAGIC = b"RDICREAT"
_BLOCK = 4096
_SCHEMA = 1


class LifecycleError(Exception):
    """An interrupted operation needs user attention; originals are preserved."""


def _state_root():
    override = os.environ.get("RAIDIANT_STATE_DIR")
    if override:
        root = Path(override).expanduser().absolute()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "RAIDiant"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/RAIDiant"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "raidiant"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise LifecycleError("The RAIDiant recovery directory must not be a symbolic link.")
    return root


def _area(name):
    path = _state_root() / name
    path.mkdir(exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise LifecycleError("The RAIDiant recovery directory must not be a symbolic link.")
    return path


def _identifier(value):
    try:
        valid = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleError("Invalid interrupted-operation identity.") from exc
    if valid != value:
        raise LifecycleError("Invalid interrupted-operation identity.")
    return valid


def _write_all(file, data):
    view = memoryview(data)
    while view:
        count = file.write(view)
        if not count:
            raise OSError("Incomplete write while saving recovery information.")
        view = view[count:]


def _save(path, document):
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix="manifest-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb", buffering=0) as file:
            _write_all(file, raw)
            durable_flush(file)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(temporary).unlink()


def _lock(path, create=False):
    if path.is_symlink():
        raise LifecycleError("Recovery lock was replaced; preserving the operation.")
    file = open(path, "x+b" if create else "r+b", buffering=0)
    try:
        lock = MemberLock(file, writable=True).acquire()
        if create:
            durable_flush(file)
        return file, lock
    except BaseException:
        file.close()
        raise


def _identity(info):
    return [info.st_dev, info.st_ino]


def _safe_info(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()):
        raise LifecycleError(f"{path}: original member was replaced; preserving it.")
    return info


def _owner_block(document, entry):
    raw = json.dumps({"operation": document["id"], "array": document["common"]["uuid"],
                      "index": entry["index"], "token": entry["token"]}, separators=(",", ":")).encode()
    return (_OWNER_MAGIC + len(raw).to_bytes(4, "little") + hashlib.sha256(raw).digest() + raw).ljust(_BLOCK, b"\0")


def _header_owned(header, document, entry):
    return (header.get("creation_id") == document["id"] and
            header.get("creation_member") == entry["token"] and
            header.get("uuid") == document["common"]["uuid"] and
            header.get("index") == entry["index"])


def _ownership(file, document, entry):
    """Return a ready header, None for an allocation marker, or refuse."""
    from .engine import RaidError, _read_headers

    expected = entry.get("identity")
    if expected is not None and _identity(os.fstat(file.fileno())) != expected:
        raise LifecycleError(f"{entry['path']}: file identity changed; preserving it.")
    file.seek(0)
    raw = file.read(2 * _BLOCK)
    marker = _owner_block(document, entry)
    try:
        headers = _read_headers(file)
    except RaidError as exc:
        if raw[:_BLOCK] == marker or raw[_BLOCK:2 * _BLOCK] == marker:
            return None
        raise LifecycleError(f"{entry['path']}: ownership cannot be verified; preserving it.") from exc
    owned = [header for header in headers if _header_owned(header, document, entry)]
    if not owned or len(owned) != len(headers):
        raise LifecycleError(f"{entry['path']}: member identity changed; preserving it.")
    return max(owned, key=lambda header: header["epoch"])


def _load(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise LifecycleError("Recovery record is not an owned directory.")
    path = directory / "manifest.json"
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise LifecycleError("Invalid recovery record; preserving member files.")
    with path.open("r", encoding="utf-8") as file:
        document = json.load(file)
    if document.get("schema") != _SCHEMA or document.get("id") != directory.name:
        raise LifecycleError("Invalid recovery record; preserving member files.")
    _identifier(document["id"])
    _identifier(document["common"]["uuid"])
    entries = document["members"]
    if not isinstance(entries, list) or len(entries) != document["common"]["n"]:
        raise LifecycleError("Invalid recovery member list.")
    if document["phase"] not in ("allocating", "publishing", "complete", "discarding"):
        raise LifecycleError("Invalid creation phase.")
    for index, entry in enumerate(entries):
        if entry["index"] != index or not Path(entry["path"]).is_absolute():
            raise LifecycleError("Invalid recovery member path.")
        _identifier(entry["token"])
    return document


class CreationJob:
    def __init__(self, directory, document, file, lock):
        self.directory, self.document = directory, document
        self._file, self._lock = file, lock

    @classmethod
    def begin(cls, paths, common):
        identity = str(uuid.uuid4())
        directory = _area("creations") / identity
        directory.mkdir(mode=0o700)
        file, lock = _lock(directory / "operation.lock", create=True)
        document = {"schema": _SCHEMA, "id": identity, "phase": "allocating", "common": common,
                    "members": [{"path": str(Path(path).absolute()), "index": index,
                                 "token": str(uuid.uuid4()), "identity": None}
                                for index, path in enumerate(paths)]}
        job = cls(directory, document, file, lock)
        try:
            job._save()
            sync_directory(directory.parent)
        except BaseException:
            job.close()
            raise
        return job

    @classmethod
    def claim(cls, operation_id):
        directory = _area("creations") / _identifier(operation_id)
        if directory.is_symlink():
            raise LifecycleError("Recovery record was replaced; preserving it.")
        try:
            file, lock = _lock(directory / "operation.lock")
        except OSError as exc:
            raise LifecycleError("Creation is active in another process, or its recovery record is unavailable.") from exc
        try:
            return cls(directory, _load(directory), file, lock)
        except BaseException:
            lock.release()
            file.close()
            raise

    def _save(self):
        _save(self.directory / "manifest.json", self.document)

    def close(self):
        if self._file is not None:
            self._lock.release()
            self._file.close()
            self._file = None

    def _retire(self):
        # Remove the manifest while the operation lock is held. A subsequent
        # claimant cannot recover a record after this point.
        (self.directory / "manifest.json").unlink(missing_ok=True)
        sync_directory(self.directory)
        self.close()
        for path in self.directory.iterdir():
            if path.name == "operation.lock" or (path.name.startswith("manifest-") and path.suffix == ".tmp"):
                path.unlink(missing_ok=True)
        self.directory.rmdir()
        sync_directory(self.directory.parent)

    def _open_members(self, create=False):
        opened = []
        try:
            for entry in self.document["members"]:
                path = Path(entry["path"])
                if not path.exists():
                    if not create:
                        continue
                    if entry.get("identity") is not None:
                        raise LifecycleError(f"{path}: a previously allocated member is missing. Reconnect its volume before resuming.")
                    file = open(path, "x+b", buffering=0)
                    lock = MemberLock(file, writable=True)
                    opened.append((entry, file, lock, None))
                    lock.acquire()
                    marker = _owner_block(self.document, entry)
                    _write_all(file, marker + marker)
                    durable_flush(file)
                    sync_directory(path.parent)
                    entry["identity"] = _identity(os.fstat(file.fileno()))
                    self._save()  # Must precede the first large allocation.
                else:
                    before = _safe_info(path)
                    file = open(path, "r+b", buffering=0)
                    lock = MemberLock(file, writable=True)
                    opened.append((entry, file, lock, None))
                    lock.acquire()
                    if _identity(before) != _identity(os.fstat(file.fileno())):
                        raise LifecycleError(f"{path}: file changed while opening; preserving it.")
                    header = _ownership(file, self.document, entry)
                    opened[-1] = (entry, file, lock, header)
                    if entry.get("identity") is None:
                        entry["identity"] = _identity(os.fstat(file.fileno()))
                        self._save()
            return opened
        except BaseException:
            self._close_members(opened)
            raise

    @staticmethod
    def _close_members(opened):
        for _entry, file, lock, _header in opened:
            with contextlib.suppress(Exception):
                lock.release()
            file.close()

    def run(self, progress=None, cancel=None):
        from .engine import LOG_STRUCT, ZERO_HASH, _cancel, _header_bytes, _progress, _record, _valid_member_size

        if self.document["phase"] == "discarding":
            raise LifecycleError("Removal of this unfinished creation was interrupted. Choose Remove incomplete files to finish cleanup.")
        opened = self._open_members(create=True)
        common = self.document["common"]
        size = (8192 + 2 * common["bank_size"] if common.get("storage") == "dynamic"
                else common["member_size"])
        n = common["n"]
        first, _digest = _record(1, ZERO_HASH, {"op": "init"})
        try:
            if not all(header is not None for _entry, _file, _lock, header in opened):
                # Never reset metadata after a partially published array has
                # been opened and used independently of this creation job.
                for _entry, file, _lock, header in opened:
                    if header is not None:
                        file.seek(8192 + len(first))
                        if header["epoch"] != 1 or file.read(LOG_STRUCT.size) != bytes(LOG_STRUCT.size):
                            raise LifecycleError("This partially created array was subsequently used. Open its ready members and rebuild the missing positions; creation recovery will preserve every file.")
                for entry, file, _lock, header in opened:
                    _cancel(cancel)
                    if header is not None:
                        continue
                    def update(done, _total=None, *unused, index=entry["index"]):
                        _cancel(cancel)
                        _progress(progress, index * size + done, n * size, f"Allocating member {index + 1} of {n}")
                    allocate_file(file, size, progress=update)
                    file.seek(8192)
                    _write_all(file, first + bytes(LOG_STRUCT.size))
                    durable_flush(file)
                    _progress(progress, (entry["index"] + 1) * size, n * size,
                              f"Allocated member {entry['index'] + 1} of {n}")
                self.document["phase"] = "publishing"
                self._save()
                for entry, file, _lock, _header in opened:
                    # A previous interruption may have published only one of
                    # the two header copies. Finish both copies on every member.
                    published = dict(common, index=entry["index"], creation_id=self.document["id"],
                                     creation_member=entry["token"])
                    if published["version"] >= 2:
                        published["member_id"] = published["member_roster"][entry["index"]]
                    file.seek(0)
                    raw = _header_bytes(published)
                    _write_all(file, raw + raw)
                    durable_flush(file)
                    sync_directory(Path(entry["path"]).parent)
            for entry, file, _lock, _header in opened:
                if not _valid_member_size(common, os.fstat(file.fileno()).st_size):
                    raise LifecycleError(f"{entry['path']}: member size changed; preserving all files.")
            self.document["phase"] = "complete"
            self._save()
        finally:
            self._close_members(opened)
        paths = [Path(entry["path"]) for entry in self.document["members"]]
        self._retire()
        return paths

    def discard(self):
        if self.document["phase"] not in ("allocating", "discarding"):
            raise LifecycleError("Member publication has begun. Resume creation to preserve the array; these files cannot be automatically removed.")
        opened = self._open_members(create=False)
        try:
            if any(header is not None for _entry, _file, _lock, header in opened):
                raise LifecycleError("A ready RAID member exists. Resume creation instead of removing these files.")
            # Validate the complete set before removing any member.  Member
            # locks remain held until the deletion pass begins.
            for entry, file, _lock, _header in opened:
                if _identity(_safe_info(Path(entry["path"]))) != _identity(os.fstat(file.fileno())):
                    raise LifecycleError(f"{entry['path']}: path changed; preserving all remaining files.")
            self.document["phase"] = "discarding"
            self._save()
        except BaseException:
            self._close_members(opened)
            raise
        removed = []
        try:
            for entry, file, lock, _header in opened:
                path = Path(entry["path"])
                identity = _identity(os.fstat(file.fileno()))
                lock.release()
                file.close()  # Windows cannot unlink an ordinary open file.
                if _identity(_safe_info(path)) != identity:
                    raise LifecycleError(f"{path}: path changed; preserving it.")
                path.unlink()
                sync_directory(path.parent)
                removed.append(str(path))
        finally:
            self._close_members(opened)
        self._retire()
        return removed


def pending_creations():
    """List recoverable inactive creations without touching their members."""
    result = []
    for directory in _area("creations").iterdir():
        try:
            job = CreationJob.claim(directory.name)
        except (LifecycleError, OSError, ValueError, KeyError, TypeError):
            continue
        try:
            document = job.document
            common = document["common"]
            result.append({"id": document["id"], "array_uuid": common["uuid"],
                           "paths": [entry["path"] for entry in document["members"]],
                           "member_size": common["member_size"], "parity": common["parity"],
                           "phase": document["phase"],
                           "can_discard": document["phase"] in ("allocating", "discarding"),
                           "can_resume": document["phase"] != "discarding",
                           "detail": ("Removal was interrupted. Remove the remaining unfinished files to finish cleanup."
                                      if document["phase"] == "discarding" else
                                      "Allocation was interrupted. Resume, or remove the unfinished member files."
                                      if document["phase"] == "allocating" else
                                      "Member publication was interrupted. Resume to finish opening the array.")})
        finally:
            job.close()
    return result


def resume_creation(operation_id, progress=None, cancel=None):
    job = CreationJob.claim(operation_id)
    try:
        return job.run(progress=progress, cancel=cancel)
    finally:
        job.close()


def discard_creation(operation_id):
    job = CreationJob.claim(operation_id)
    try:
        return job.discard()
    finally:
        job.close()


class ManagedScratch:
    """TemporaryDirectory-compatible storage with a persistent ownership lock."""

    def __init__(self):
        root = _area("scratch")
        self._id = str(uuid.uuid4())
        directory = root / self._id
        directory.mkdir(mode=0o700)
        self.name = str(directory)
        self._file, self._lock = _lock(root / f"{self._id}.lock", create=True)
        try:
            _save(directory / "owner.json", {"schema": _SCHEMA, "id": self._id,
                                           "identity": _identity(directory.stat())})
        except BaseException:
            self._lock.release()
            self._file.close()
            raise
        self._closed = False

    def cleanup(self):
        if self._closed:
            return
        self._closed = True
        try:
            _remove_scratch(Path(self.name), self._id)
        finally:
            self._lock.release()
            self._file.close()
        (Path(self.name).parent / f"{self._id}.lock").unlink(missing_ok=True)


def _remove_scratch(directory, identity):
    if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
        raise LifecycleError("Scratch directory was replaced; preserving it.")
    resolved_root = _area("scratch").resolve(strict=True)
    resolved_directory = directory.resolve(strict=True)
    # Packaged Windows apps can expose a virtual LocalAppData root while child
    # paths resolve into the package cache. Compare the actual directory identity
    # as well as the exact child name, not only their textual spellings.
    if not resolved_directory.parent.samefile(resolved_root) or resolved_directory.name != identity:
        raise LifecycleError("Scratch directory is outside its recovery root; preserving it.")
    marker = directory / "owner.json"
    if marker.is_symlink() or marker.stat().st_size > 4096:
        raise LifecycleError("Scratch ownership cannot be verified; preserving it.")
    with marker.open("r", encoding="utf-8") as file:
        owner = json.load(file)
    if (owner != {"schema": _SCHEMA, "id": identity, "identity": _identity(directory.stat())} or
            directory.parent != _area("scratch")):
        raise LifecycleError("Scratch ownership cannot be verified; preserving it.")
    shutil.rmtree(resolved_directory)


def cleanup_orphan_scratch():
    """Remove only marked scratch directories whose process lock is free."""
    removed = []
    root = _area("scratch")
    for directory in root.iterdir():
        try:
            identity = _identifier(directory.name)
            file, lock = _lock(root / f"{identity}.lock")
        except (LifecycleError, OSError):
            continue
        try:
            _remove_scratch(directory, identity)
            removed.append(str(directory))
        except (LifecycleError, OSError, ValueError, TypeError, KeyError):
            continue
        finally:
            lock.release()
            file.close()
        (root / f"{identity}.lock").unlink(missing_ok=True)
    return removed
