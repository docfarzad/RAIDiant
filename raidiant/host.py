"""Bounded-memory host I/O and conservative portable pathname handling.

All allocation and flush errors are propagated.  These operations rely on the
host filesystem and device honoring their durability contracts; snapshots,
thin provisioning and remote filesystems can weaken those contracts.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import unicodedata
from typing import BinaryIO, Callable


ALIGNMENT = 4096
ALLOCATION_CHUNK = 4 * 1024 * 1024
_UNSUPPORTED = {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$",
    *(f"COM{number}" for number in "123456789\u00b9\u00b2\u00b3"),
    *(f"LPT{number}" for number in "123456789\u00b9\u00b2\u00b3"),
}


def durable_flush(file: BinaryIO) -> None:
    """Flush Python buffers and request persistent storage synchronization.

    macOS's F_FULLFSYNC additionally asks the device to flush its write cache.
    Python's Windows fsync uses the CRT commit operation (FlushFileBuffers).
    A failed sync is a failed transaction, not a best-effort warning.
    """
    file.flush()
    os.fsync(file.fileno())
    if sys.platform == "darwin":
        import fcntl

        fcntl.fcntl(file.fileno(), getattr(fcntl, "F_FULLFSYNC", 51))


def sync_directory(path: str | Path) -> None:
    """Persist directory entries on platforms with a directory-fsync API.

    Windows has no equivalent portable unprivileged directory operation.  On
    Windows this validates the directory only; callers must durably flush the
    created member itself and must not promise POSIX directory-fsync semantics.
    """
    directory = Path(path)
    if not stat.S_ISDIR(directory.stat().st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(directory))
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_allocate(descriptor: int, size: int) -> None:
    import msvcrt
    from ctypes import wintypes

    # AllocationInfo on compressed/sparse files does not make the same promise.
    attributes = getattr(os.fstat(descriptor), "st_file_attributes", 0)
    if attributes & (0x200 | 0x800):  # SPARSE_FILE | COMPRESSED
        raise OSError(errno.ENOTSUP, "Member files must not be sparse or compressed")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    setter = kernel.SetFileInformationByHandle
    setter.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    setter.restype = wintypes.BOOL
    allocation = ctypes.c_longlong(size)
    if not setter(msvcrt.get_osfhandle(descriptor), 5, ctypes.byref(allocation), 8):
        error = ctypes.get_last_error()
        if error in (1, 50, 87, 120):  # Unsupported operation/parameter.
            raise OSError(errno.ENOTSUP, "Native file preallocation is unavailable")
        raise ctypes.WinError(error)
    os.ftruncate(descriptor, size)


def _mac_allocate(descriptor: int, size: int) -> None:
    import fcntl

    # fstore_t: uint32 flags, int posmode, off_t offset/length/bytesalloc.
    # F_ALLOCATEALL and F_PEOFPOSMODE allocate all requested storage without
    # requiring one contiguous extent.  ftruncate establishes the logical EOF.
    request = struct.pack("=Iiqqq", 4, 3, 0, size, 0)
    fcntl.fcntl(descriptor, 42, request)  # F_PREALLOCATE
    os.ftruncate(descriptor, size)


def _native_allocate(descriptor: int, size: int) -> None:
    if os.name == "nt":
        _windows_allocate(descriptor, size)
    elif sys.platform == "darwin":
        _mac_allocate(descriptor, size)
    elif hasattr(os, "posix_fallocate"):
        os.posix_fallocate(descriptor, 0, size)
    else:
        raise OSError(errno.ENOSYS, "Native file preallocation is unavailable")


def _write_all(file: BinaryIO, data: bytes | memoryview) -> None:
    view = memoryview(data)
    while view:
        written = file.write(view)
        if written is None or written <= 0:
            raise OSError(errno.EIO, "Short write during member allocation")
        view = view[written:]


def _zero_fill(
    file: BinaryIO,
    size: int,
    existing_size: int,
    progress: Callable[[int, int], None] | None,
) -> None:
    """Touch every byte, including any holes in the preserved existing prefix."""
    zeros = bytes(min(ALLOCATION_CHUNK, size))
    position = 0
    while position < size:
        length = min(ALLOCATION_CHUNK, size - position)
        if position < existing_size:
            preserved = min(length, existing_size - position)
            file.seek(position)
            data = file.read(preserved)
            if len(data) != preserved:
                raise OSError(errno.EIO, "Short read during member allocation")
            data += zeros[: length - preserved]
        else:
            data = zeros[:length]
        file.seek(position)
        _write_all(file, data)
        position += length
        if progress is not None:
            progress(position, size)
    file.truncate(size)


def allocate_file(
    file: BinaryIO,
    size: int,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Reserve a fixed-size regular member without using sparse truncate alone.

    Native preallocation is preferred. Unsupported operations fall back to
    bounded writes; capacity, quota, I/O and flush failures always propagate.
    Existing bytes are preserved, shrinking is refused, and the file position
    is restored.  The caller must exclusively own the file for this operation.
    The optional progress callback receives (completed_bytes, total_bytes).
    """
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("Allocation size must be a nonnegative integer")
    file.flush()
    descriptor = file.fileno()
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("A member must be a regular file")
    if size < info.st_size:
        raise ValueError("Allocation must not truncate existing member data")
    if not file.writable():
        raise ValueError("Allocation requires a writable file")
    if os.name == "nt" and getattr(info, "st_file_attributes", 0) & (0x200 | 0x800):
        # Do not zero-fill this failure: zeroes remain compressed or sparse.
        raise OSError(errno.ENOTSUP, "Member files must not be sparse or compressed")
    saved_position = file.tell()
    try:
        if progress is not None:
            progress(0, size)
        if size:
            try:
                _native_allocate(descriptor, size)
            except OSError as error:
                if error.errno not in _UNSUPPORTED:
                    raise
                if info.st_size and not file.readable():
                    raise ValueError("Fallback allocation of an existing file requires read access") from error
                _zero_fill(file, size, info.st_size, progress)
        durable_flush(file)
        if os.fstat(descriptor).st_size != size:
            raise OSError(errno.EIO, "Member allocation produced an unexpected file size")
        if progress is not None:
            progress(size, size)
    finally:
        file.seek(saved_position)


def extend_file(file: BinaryIO, size: int) -> None:
    """Physically back only a growing member's new tail, in bounded memory.

    No committed prefix is rewritten by the portable fallback. Callers journal
    the desired extent first and must recover a partially extended tail on error.
    """
    descriptor = file.fileno()
    before = os.fstat(descriptor)
    if type(size) is not int or size < before.st_size or not stat.S_ISREG(before.st_mode):
        raise ValueError("Extension requires a regular file and a nonshrinking size")
    if size == before.st_size:
        return
    position = file.tell()
    try:
        try:
            if os.name == "nt":
                _windows_allocate(descriptor, size)
            elif sys.platform != "darwin" and hasattr(os, "posix_fallocate"):
                os.posix_fallocate(descriptor, before.st_size, size - before.st_size)
            else:
                raise OSError(errno.ENOSYS, "Incremental native allocation is unavailable")
        except OSError as error:
            if error.errno not in _UNSUPPORTED:
                raise
            # macOS and unsupported filesystems use a bounded tail-only fill.
            file.seek(before.st_size)
            remaining = size - before.st_size
            zeros = bytes(min(ALLOCATION_CHUNK, remaining))
            while remaining:
                block = zeros[:min(remaining, len(zeros))]
                _write_all(file, block)
                remaining -= len(block)
            file.truncate(size)
        durable_flush(file)
        if os.fstat(descriptor).st_size != size:
            raise OSError(errno.EIO, "Member extension produced an unexpected file size")
    finally:
        file.seek(position)


class MemberLock:
    """A nonblocking session lock: multiple readers or one exclusive writer.

    Locks are held on the open file descriptor, not a removable sidecar.  They
    disappear when that descriptor closes or the process exits.  Windows locks
    a byte beyond the supported file range, avoiding accidental interference
    with ordinary header reads through a second descriptor.
    """

    def __init__(self, file: BinaryIO, writable: bool = True) -> None:
        self.file = file
        self.writable = writable
        self._locked = False
        self._overlapped = None

    def acquire(self) -> "MemberLock":
        if self._locked:
            return self
        if os.name == "nt":
            self._windows_lock(unlock=False)
        else:
            import fcntl

            mode = fcntl.LOCK_EX if self.writable else fcntl.LOCK_SH
            fcntl.flock(self.file.fileno(), mode | fcntl.LOCK_NB)
        self._locked = True
        return self

    def release(self) -> None:
        if not self._locked:
            return
        if self.file.closed:
            self._locked = False
            return
        if os.name == "nt":
            self._windows_lock(unlock=True)
        else:
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self._locked = False

    def _windows_lock(self, *, unlock: bool) -> None:
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if self._overlapped is None:
            self._overlapped = Overlapped(0, 0, 0xFFFFFFFE, 0x7FFFFFFF, None)
        handle = msvcrt.get_osfhandle(self.file.fileno())
        if unlock:
            operation = kernel.UnlockFileEx
            operation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_void_p]
            operation.restype = wintypes.BOOL
            result = operation(handle, 0, 1, 0, ctypes.byref(self._overlapped))
        else:
            operation = kernel.LockFileEx
            operation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            operation.restype = wintypes.BOOL
            flags = 1 | (2 if self.writable else 0)  # FAIL_IMMEDIATELY | EXCLUSIVE_LOCK
            result = operation(handle, flags, 0, 1, 0, ctypes.byref(self._overlapped))
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self) -> "MemberLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _existing_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    try:
        candidate.stat()
    except FileNotFoundError:
        if not candidate.parent.is_dir():
            raise FileNotFoundError(errno.ENOENT, "Member parent directory does not exist", str(candidate.parent))
        return candidate.parent
    return candidate


def _windows_volume_key(path: Path) -> str:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_root = kernel.GetVolumePathNameW
    get_root.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_root.restype = wintypes.BOOL
    root = ctypes.create_unicode_buffer(32768)
    if not get_root(str(path), root, len(root)):
        raise ctypes.WinError(ctypes.get_last_error())
    get_name = kernel.GetVolumeNameForVolumeMountPointW
    get_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_name.restype = wintypes.BOOL
    name = ctypes.create_unicode_buffer(128)
    if get_name(root.value, name, len(name)):
        return "win-volume:" + name.value.casefold()
    # UNC shares have no volume GUID. Keeping the share root groups directories
    # on the share conservatively; the report separately flags remote paths.
    return "win-root:" + root.value.casefold()


def _linux_pool_key(path: Path) -> str | None:
    """Recognize shared Btrfs subvolumes and ZFS datasets without shell tools."""
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    matching: list[tuple[int, str, str]] = []
    for line in lines:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields, filesystem = before.split(), after.split()
        if len(fields) < 5 or len(filesystem) < 2:
            continue
        mount = fields[4]
        for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            mount = mount.replace(encoded, decoded)
        if path == Path(mount) or Path(mount) in path.parents:
            matching.append((len(mount), filesystem[0], filesystem[1]))
    if not matching:
        return None
    _, kind, source = max(matching)
    if kind == "btrfs":
        # statvfs f_fsid is shared by subvolumes, unlike stat st_dev.
        fsid = getattr(os.statvfs(path), "f_fsid", 0)
        return f"btrfs:{fsid}" if fsid else "btrfs-device:" + os.path.realpath(source)
    if kind == "zfs":
        return "zfs-pool:" + source.split("/", 1)[0]
    return None


def get_volume_key(path: str | Path) -> str:
    """Identify the free-space domain, including common shared-pool layouts.

    This is not an identity for an independent physical disk. Some external
    thin-provisioning layers cannot be discovered from a file path.
    """
    existing = _existing_path(path)
    if os.name == "nt":
        return _windows_volume_key(existing)
    if sys.platform == "darwin":
        # diskutil accepts a mount point, not every arbitrary file pathname.
        mount = existing if existing.is_dir() else existing.parent
        device = mount.stat().st_dev
        while mount.parent != mount and mount.parent.stat().st_dev == device:
            mount = mount.parent
        try:
            result = subprocess.run(
                ["/usr/sbin/diskutil", "info", "-plist", str(mount)],
                check=True, capture_output=True, timeout=10,
            )
            info = plistlib.loads(result.stdout)
            container = info.get("APFSContainerReference")
            if container:
                return "apfs-container:" + str(container)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
            pass
    elif sys.platform.startswith("linux"):
        shared_pool = _linux_pool_key(existing)
        if shared_pool:
            return shared_pool
    return "device:" + str(existing.stat().st_dev)


def capacity_report(
    paths: list[str | Path],
    reserve_fraction: float = 0.05,
    reserve_bytes: int = 64 * 1024**2,
) -> dict:
    """Recommend equal member lengths, dividing free space among shared targets.

    The reserve is the larger of the fixed reserve and the fractional reserve.
    Recommendations are advisory until allocation succeeds. Existing targets
    are included only for inspection: the caller must never overwrite them.
    """
    if not paths:
        raise ValueError("Choose at least one member location")
    if not isinstance(reserve_fraction, (int, float)) or not math.isfinite(reserve_fraction) or not 0 <= reserve_fraction < 1:
        raise ValueError("Reserve fraction must be between zero (inclusive) and one")
    if isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0:
        raise ValueError("Reserve bytes must be a nonnegative integer")
    normalized: set[str] = set()
    identities: set[tuple[int, int]] = set()
    groups: dict[str, dict] = {}
    warnings = [
        "Different volumes can share a physical disk or thin storage pool; verify independent failure domains.",
        "Free space is advisory until allocation succeeds; snapshots, quotas and other writers may consume it.",
    ]
    for supplied in paths:
        target = Path(supplied).expanduser().resolve(strict=False)
        canonical = os.path.normcase(str(target))
        if canonical in normalized:
            raise ValueError(f"A member location was selected more than once: {target}")
        normalized.add(canonical)
        existing = _existing_path(target)
        if existing == target:
            info = target.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"A member location must name a regular file: {target}")
            identity = (info.st_dev, info.st_ino)
            if info.st_ino and identity in identities:
                raise ValueError(f"Member paths refer to the same existing file: {target}")
            identities.add(identity)
            warnings.append(f"The target already exists and must not be overwritten: {target}")
        volume = get_volume_key(target)
        free = shutil.disk_usage(existing).free
        if volume in groups:
            groups[volume]["count"] += 1
            # Quotas can expose different free-space values within one pool.
            groups[volume]["free"] = min(groups[volume]["free"], free)
        else:
            groups[volume] = {"path": str(existing if existing.is_dir() else existing.parent), "free": free, "count": 1}
        if str(target).startswith("\\\\"):
            warnings.append(f"Network location: allocation and durability guarantees depend on the server: {target}")
    volumes = []
    for group in groups.values():
        reserve = max(reserve_bytes, math.ceil(group["free"] * reserve_fraction))
        group["usable"] = max(0, group["free"] - reserve)
        volumes.append(group)
        if group["count"] > 1:
            warnings.append(f"{group['count']} members share free space at {group['path']}; its capacity is divided between them.")
    recommended = min(group["usable"] // group["count"] for group in volumes)
    return {"recommended_size": recommended // ALIGNMENT * ALIGNMENT, "volumes": volumes, "warnings": warnings}


def _truncate_utf8(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _reserved(value: str) -> bool:
    return value.split(".", 1)[0].rstrip(" .").upper() in _RESERVED_NAMES


def sanitize_component(name: str, max_bytes: int = 180) -> str:
    """Produce one portable visible filename component, never a host path."""
    if not isinstance(name, str):
        raise ValueError("A filename must be text")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("Filename byte limit must be a positive integer")
    value = unicodedata.normalize("NFC", name)
    value = "".join(
        "_" if character in '<>:"/\\|?*' or unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in value
    ).strip().rstrip(". ")
    if value.startswith("."):
        value = "_" + value.lstrip(".")
    if not value:
        value = "unnamed"
    value = _truncate_utf8(value, max_bytes).rstrip(". ") or "_"
    if _reserved(value):
        value = _truncate_utf8("_" + value, max_bytes)
    return value


def _collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold().rstrip(". ")


def unique_export_path(parent: Path, name: str) -> Path:
    """Choose a sanitized name without case/Unicode/sanitization collisions.

    This function does not create or reserve the path. To close the race with
    another process the caller must use exclusive creation (open 'xb' or mkdir
    with exist_ok=False), and retry selection on FileExistsError.
    """
    parent = Path(parent)

    def occupied(candidate: str) -> bool:
        # Stream even exceptionally large host directories rather than keeping
        # one Python string per entry in RAM. Usually only one pass is needed.
        key = _collision_key(candidate)
        with os.scandir(parent) as entries:
            return any(_collision_key(entry.name) == key for entry in entries)

    safe = sanitize_component(name)
    if not occupied(safe):
        return parent / safe
    suffix = Path(safe).suffix
    # Avoid an enormous extension starving the useful name and conflict suffix.
    if len(suffix.encode("utf-8")) > 48:
        suffix = ""
    stem = safe[: -len(suffix)] if suffix else safe
    index = 2
    while True:
        tag = f" ({index})"
        budget = 180 - len((tag + suffix).encode("utf-8"))
        candidate = (_truncate_utf8(stem, budget).rstrip(". ") or "_") + tag + suffix
        if not occupied(candidate):
            return parent / candidate
        index += 1


def is_hidden(path: Path) -> bool:
    """Recognize dotfiles, Windows hidden/system and macOS UF_HIDDEN flags.

    Stat errors propagate so an unreadable source is not silently skipped.
    The entry itself is inspected without following symlinks.
    """
    path = Path(path)
    if path.name.startswith("."):
        return True
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    flags = getattr(info, "st_flags", 0)
    return bool(attributes & (0x2 | 0x4) or flags & getattr(stat, "UF_HIDDEN", 0x8000))
