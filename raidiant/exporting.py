"""Tracked export staging and atomic, non-overwriting publication."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
from pathlib import Path
import sys
import uuid

from .host import sync_directory
from .lifecycle import LifecycleError, _area, _identifier, _identity, _lock, _safe_info, _save


def publish_file(source, destination):
    """Publish a complete same-directory file, never an incomplete final name."""
    try:
        os.link(source, destination)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP,
                             errno.ENOSYS, errno.EINVAL, errno.EXDEV, errno.EACCES):
            raise
    if os.name == "nt":
        # Windows rename refuses an existing destination (unlike POSIX rename).
        os.rename(source, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        operation = library.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        operation = library.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "This destination cannot publish files atomically without overwriting. Choose another local filesystem.")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


class ExportStage:
    def __init__(self, parent):
        self.id = str(uuid.uuid4())
        self.directory = _area("exports") / self.id
        self.directory.mkdir(mode=0o700)
        self._file, self._lock = _lock(self.directory / "operation.lock", create=True)
        self.path = Path(parent).resolve() / f".raidiant-export-{self.id}.part"
        self.document = dict(id=self.id, path=str(self.path), identity=None)
        self.target = None
        try:
            _save(self.directory / "manifest.json", self.document)
            self.target = open(self.path, "x+b", buffering=0)
            self.document["identity"] = _identity(os.fstat(self.target.fileno()))
            _save(self.directory / "manifest.json", self.document)
            sync_directory(self.directory.parent)
        except BaseException:
            if self.target:
                self.target.close()
            self._lock.release()
            self._file.close()
            raise

    def __enter__(self):
        return self

    def publish(self, destination):
        self.target.close()
        if _identity(_safe_info(self.path)) != self.document["identity"]:
            raise ValueError("The export staging file was replaced; nothing was published")
        publish_file(self.path, destination)
        if _identity(_safe_info(Path(destination))) != self.document["identity"]:
            raise ValueError("The export destination changed during publication; inspect the destination before using it")
        sync_directory(Path(destination).parent)

    def __exit__(self, *_):
        self.target.close()
        try:
            _clean_stage(self.directory, self.document)
            _drop_manifest(self.directory)
        finally:
            self._lock.release()
            self._file.close()
        _retire(self.directory)


def _clean_stage(directory, document):
    operation_id = _identifier(document["id"])
    path = Path(document["path"])
    if (directory.name != operation_id or not path.is_absolute() or
            path.name != f".raidiant-export-{operation_id}.part"):
        raise ValueError("Unrecognized export recovery record; preserving files")
    try:
        info = _safe_info(path)
    except FileNotFoundError:
        return
    if document["identity"] is None or _identity(info) != document["identity"]:
        raise ValueError("Export temporary file identity changed; preserving it")
    path.unlink()
    sync_directory(path.parent)


def _drop_manifest(directory):
    # Retire the recoverable record while its lock is still exclusively held.
    (directory / "manifest.json").unlink(missing_ok=True)
    sync_directory(directory)


def _retire(directory):
    # A competing cleaner may have opened the lock before seeing the retired
    # manifest. Its brief handle must not turn a successful copy into an error.
    with contextlib.suppress(OSError):
        (directory / "operation.lock").unlink(missing_ok=True)
        for path in directory.glob("manifest-*.tmp"):
            path.unlink(missing_ok=True)
        directory.rmdir()


def cleanup_orphan_exports():
    removed = []
    for directory in _area("exports").iterdir():
        file = lock = None
        try:
            _identifier(directory.name)
            if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
                continue
            file, lock = _lock(directory / "operation.lock")
            manifest = directory / "manifest.json"
            if not manifest.exists():
                lock.release()
                file.close()
                file = lock = None
                _retire(directory)
                continue
            if manifest.is_symlink() or manifest.stat().st_size > 65536:
                continue
            with manifest.open(encoding="utf-8") as source:
                document = json.load(source)
            _clean_stage(directory, document)
            _drop_manifest(directory)
            lock.release()
            file.close()
            file = lock = None
            _retire(directory)
            removed.append(document["path"])
        except (LifecycleError, OSError, ValueError, KeyError, TypeError):
            continue
        finally:
            if lock:
                with contextlib.suppress(OSError):
                    lock.release()
            if file:
                file.close()
    return removed
