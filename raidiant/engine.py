"""Bounded-memory storage engine.

Data stripes are immutable while referenced. A replicated, checksummed metadata
log publishes files only after all shards have been synchronized. Two metadata
banks and alternating superblocks permit crash-safe streaming compaction. The
SQLite catalog is a disposable on-disk index, never the source of truth.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import threading
import time
import uuid

from .codec import ReedSolomon
from .exporting import ExportStage
from .lifecycle import ManagedScratch
from .host import (MemberLock, allocate_file, extend_file, capacity_report, durable_flush,
                   is_hidden, sync_directory, unique_export_path)

HEADER_SIZE = 4096
HEADER_MAGIC = b"RDIANT01"
LOG_MAGIC = b"RDLOG001"
SHARD_MAGIC = b"RDSHR001"
LOG_STRUCT = struct.Struct("<8sQI32s32s")
SHARD_STRUCT = struct.Struct("<8s16sQI32s")
SHARD_HEADER = 128
ZERO_HASH = bytes(32)
MAX_RECORD = 65536
FORMAT_VERSION = 3


class RaidError(Exception):
    """An actionable storage or format error."""


class RecoveryRequired(RaidError):
    pass


class UnrecoverableError(RaidError):
    pass


class Cancelled(RaidError):
    pass


def _progress(callback, current, total, message):
    if callback:
        callback(current, total, message)


def _cancel(callback):
    if callback and callback():
        raise Cancelled("Operation cancelled. Previously completed files are retained.")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _header_bytes(header):
    payload = _json(header)
    if len(payload) > HEADER_SIZE - 44:
        raise RaidError("Member header is too large.")
    return (HEADER_MAGIC + struct.pack("<I", len(payload)) +
            hashlib.sha256(payload).digest() + payload).ljust(HEADER_SIZE, b"\0")


def _read_headers(file):
    result = []
    for offset in (0, HEADER_SIZE):
        try:
            file.seek(offset)
            data = file.read(HEADER_SIZE)
        except OSError:
            continue
        if len(data) != HEADER_SIZE or data[:8] != HEADER_MAGIC:
            continue
        size = struct.unpack("<I", data[8:12])[0]
        if not 1 <= size <= HEADER_SIZE - 44:
            continue
        payload = data[44:44 + size]
        if hashlib.sha256(payload).digest() != data[12:44]:
            continue
        try:
            header = json.loads(payload)
            if not isinstance(header, dict):
                continue
            _validate_header(header)
            result.append(header)
        except (ValueError, TypeError, KeyError, RaidError):
            continue
    if not result:
        raise RaidError("No valid RAIDiant member header; select another member or replace this one.")
    if len(result) == 2:
        first, second = result
        if _geometry(first) != _geometry(second) or first["index"] != second["index"]:
            raise UnrecoverableError("Conflicting identities in the two member headers; preserve this file.")
        if first.get("state", "ready") == second.get("state", "ready") == "ready":
            if (first["epoch"] == second["epoch"] and
                    _checkpoint_identity(first) != _checkpoint_identity(second)):
                raise UnrecoverableError("Conflicting checkpoints in the two member headers; preserve this file.")
            if (first.get("membership_epoch", 0) == second.get("membership_epoch", 0) and
                    _membership_identity(first) != _membership_identity(second)):
                raise UnrecoverableError("Conflicting replacement histories in the two member headers; preserve this file.")
    return result


def _header_order(header):
    return (header.get("membership_epoch", 0), header["epoch"], header.get("state", "ready") == "ready",
            header.get("rebuild_next", 0))


def _validate_header(h):
    if h.get("version") not in (1, 2, FORMAT_VERSION) or h.get("codec") != "rs-gf256-v1":
        raise RaidError("Unsupported member format or codec.")
    for key in ("n", "parity", "index", "member_size", "chunk_size", "bank_size",
                "stripes", "epoch", "bank", "checkpoint_bytes", "checkpoint_seq"):
        if type(h.get(key)) is not int:
            raise RaidError("Invalid integer in member header.")
    n, m = h["n"], h["parity"]
    if not 3 <= n <= 32 or not 1 <= m <= n - 2 or not 0 <= h["index"] < n:
        raise RaidError("Invalid array geometry.")
    if not 4096 <= h["chunk_size"] <= 4 * 1024**2 or h["chunk_size"] % 4096:
        raise RaidError("Invalid chunk size.")
    if not 65536 <= h["bank_size"] <= 512 * 1024**2 or h["bank_size"] % 4096:
        raise RaidError("Invalid metadata bank size.")
    if h["epoch"] < 1 or h["bank"] not in (0, 1) or h["stripes"] < 1:
        raise RaidError("Invalid array state.")
    if not 0 < h["checkpoint_bytes"] < h["bank_size"] or h["checkpoint_seq"] < 1:
        raise RaidError("Invalid metadata checkpoint.")
    expected = (h["member_size"] - 8192 - 2 * h["bank_size"]) // (h["chunk_size"] + SHARD_HEADER)
    if expected != h["stripes"] or h["member_size"] > 2**63 - 1:
        raise RaidError("Invalid member capacity.")
    if h["version"] >= 3 and h.get("storage") != "dynamic":
        raise RaidError("Unsupported member allocation mode.")
    if not isinstance(h.get("uuid"), str):
        raise RaidError("Invalid array identity.")
    uuid.UUID(h["uuid"])
    if h["version"] >= 2:
        if type(h.get("membership_epoch")) is not int or h["membership_epoch"] < 0:
            raise RaidError("Invalid membership generation.")
        roster = h.get("member_roster")
        if not isinstance(roster, list) or len(roster) != n or len(set(roster)) != n:
            raise RaidError("Invalid member incarnation roster.")
        for incarnation in roster:
            if not isinstance(incarnation, str):
                raise RaidError("Invalid member incarnation.")
            uuid.UUID(incarnation)
        if not isinstance(h.get("member_id"), str):
            raise RaidError("Invalid member identity.")
        uuid.UUID(h["member_id"])
        if h["member_id"] != roster[h["index"]]:
            raise RaidError("Member incarnation does not match its roster.")
    if len(bytes.fromhex(h["checkpoint_hash"])) != 32:
        raise RaidError("Invalid checkpoint checksum.")
    if h.get("state", "ready") not in ("ready", "rebuilding"):
        raise RaidError("Unsupported member state.")
    if h.get("state") == "rebuilding":
        if type(h.get("rebuild_next")) is not int or not 0 <= h["rebuild_next"] <= h["stripes"]:
            raise RaidError("Invalid rebuild progress.")
        if len(bytes.fromhex(h["rebuild_source_hash"])) != 32:
            raise RaidError("Invalid rebuild source identity.")


def inspect_member(path):
    """Inspect a member without modifying or locking it."""
    with open(path, "rb", buffering=0) as file:
        headers = _read_headers(file)
        h = max(headers, key=_header_order)
        if not _valid_member_size(h, os.fstat(file.fileno()).st_size) and h.get("state") != "rebuilding":
            raise RaidError("Member file length differs from its recorded capacity.")
        return dict(h)


def _record(seq, previous, event):
    payload = _json(event)
    if not 1 <= len(payload) <= MAX_RECORD:
        raise RaidError("Metadata record is too large.")
    digest = hashlib.sha256(struct.pack("<Q", seq) + previous + payload).digest()
    return LOG_STRUCT.pack(LOG_MAGIC, seq, len(payload), previous, digest) + payload, digest


def _read_record(file, offset, limit, seq, previous):
    if offset + LOG_STRUCT.size > limit:
        return None
    file.seek(offset)
    raw = file.read(LOG_STRUCT.size)
    if not raw or raw == bytes(len(raw)):
        return None
    if len(raw) != LOG_STRUCT.size:
        raise RaidError("Truncated metadata record.")
    magic, actual_seq, size, prior, digest = LOG_STRUCT.unpack(raw)
    if (magic != LOG_MAGIC or actual_seq != seq or prior != previous or
            not 1 <= size <= MAX_RECORD or offset + len(raw) + size > limit):
        raise RaidError("Invalid metadata chain.")
    payload = file.read(size)
    if len(payload) != size or hashlib.sha256(struct.pack("<Q", seq) + prior + payload).digest() != digest:
        raise RaidError("Metadata checksum mismatch.")
    try:
        event = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise RaidError("Invalid metadata encoding.") from exc
    if not isinstance(event, dict):
        raise RaidError("Invalid metadata event.")
    return raw + payload, digest, event


class Array:
    """One exclusively owned array session; all mutations are serialized."""

    def __init__(self):
        self._files = {}
        self._locks = {}
        self._paths = {}
        self._headers = {}
        self._expected_sizes = {}
        self._io = threading.RLock()
        self._db_lock = threading.RLock()
        self._operation = threading.Lock()
        self._reader_gate = threading.Condition(self._io)
        self._readers = 0
        self._rebuilding = False
        self._exclusive_pending = False
        self._scratch = ManagedScratch()
        self._db = None
        self._canonical = None
        self._closed = False
        self._read_only = False
        self._needs_recovery = False
        self._integrity_failed = False
        self._quarantined = []
        self._last_verification = None
        self._usage_cache = None
        self._active_io = None
        self._bad = set()
        self._header_bad = set()
        self._issues = []
        self.health_revision = 0
        self._busy = False
        self._loading = False
        self._fault_hook = None  # fault injection boundary; never persisted

    @classmethod
    def create(cls, paths, parity, member_size, chunk_size=262144, progress=None, cancel=None,
               preallocate=False):
        from .lifecycle import CreationJob

        paths = [Path(p).absolute() for p in paths]
        n = len(paths)
        ReedSolomon(n, parity)  # validate before creating any file
        if type(member_size) is not int or member_size < 2 * 1024**2:
            raise RaidError("Each member must be at least 2 MiB.")
        if type(chunk_size) is not int or not 4096 <= chunk_size <= 4 * 1024**2 or chunk_size % 4096:
            raise RaidError("Chunk size must be a multiple of 4096 between 4 KiB and 4 MiB.")
        report = capacity_report(paths)
        for path in paths:
            if path.exists() or path.is_symlink():
                raise RaidError(f"The member destination already exists: {path}")
        if member_size > report["recommended_size"]:
            raise RaidError("Requested capacity exceeds the safe available space on the selected volumes.")
        bank_size = max(65536, min(512 * 1024**2, member_size // 32 // 4096 * 4096))
        stripes = (member_size - 8192 - 2 * bank_size) // (chunk_size + SHARD_HEADER)
        if stripes < 1:
            raise RaidError("Members are too small for this chunk size and metadata.")
        first, digest = _record(1, ZERO_HASH, {"op": "init"})
        common = dict(version=1, codec="rs-gf256-v1", uuid=str(uuid.uuid4()),
                      n=n, parity=parity, member_size=member_size, chunk_size=chunk_size,
                      bank_size=bank_size, stripes=stripes, epoch=1, bank=0,
                      checkpoint_bytes=len(first), checkpoint_seq=1,
                      checkpoint_hash=digest.hex())
        if not preallocate:
            common.update(version=3, storage="dynamic", membership_epoch=0,
                          member_roster=[str(uuid.uuid4()) for _ in paths])
        job = CreationJob.begin(paths, common)
        try:
            job.run(progress=progress, cancel=cancel)
        except BaseException:
            # Failed allocation can be safely removed. Once publication starts,
            # keep the durable recovery record and finish creation on next run.
            with contextlib.suppress(Exception):
                job.discard()
            raise
        finally:
            job.close()
        return cls.open(paths)

    @classmethod
    def open(cls, paths, readonly=False, progress=None):
        array = cls()
        array._read_only = bool(readonly)
        identities = set()
        try:
            for path in paths:
                path = Path(path).absolute()
                # Detect aliases as well as repeated member identities.
                try:
                    st = os.stat(path)
                    identity = (st.st_dev, st.st_ino)
                except OSError as exc:
                    array._record_quarantine(path, str(exc))
                    continue
                if identity in identities:
                    raise RaidError("The same physical member file was selected more than once.")
                identities.add(identity)
                mode = "rb" if readonly else "r+b"
                try:
                    file = open(path, mode, buffering=0)
                except OSError as exc:
                    array._record_quarantine(path, str(exc))
                    continue
                try:
                    lock = MemberLock(file, writable=not readonly)
                    lock.acquire()
                    try:
                        headers = _read_headers(file)
                    except UnrecoverableError:
                        raise
                    except (RaidError, OSError) as exc:
                        array._record_quarantine(path, str(exc))
                        lock.release()
                        file.close()
                        continue
                    h = max(headers, key=_header_order)
                    if h.get("state") == "rebuilding":
                        raise RaidError("This replacement is incomplete. Open the surviving members and use Rebuild to resume it.")
                    size = os.fstat(file.fileno()).st_size
                    if not _valid_member_size(h, size):
                        array._record_quarantine(path, "Member is truncated or has changed size.", h["index"])
                        lock.release()
                        file.close()
                        continue
                    index = h["index"]
                    if array._headers and _geometry(h) != _geometry(next(iter(array._headers.values()))):
                        raise RaidError("Selected members belong to different arrays or layouts.")
                    if index in array._files:
                        raise RaidError("Two selected files represent the same member position.")
                    array._files[index] = file
                    array._locks[index] = lock
                    array._paths[index] = path
                    array._headers[index] = h
                    array._expected_sizes[index] = size
                except BaseException:
                    with contextlib.suppress(Exception):
                        lock.release()
                    file.close()
                    raise
            if not array._files:
                raise RaidError("No usable members were selected. Select surviving members or restore a backup.")
            array.h = max(array._headers.values(), key=lambda item: item["epoch"]).copy()
            array.n, array.parity = array.h["n"], array.h["parity"]
            array.k = array.n - array.parity
            if len(array._files) < array.k:
                raise RaidError(f"At least {array.k} distinct valid members are required.")
            array.codec = ReedSolomon(array.n, array.parity)
            array._load_catalog(progress)
            return array
        except BaseException:
            array.close()
            raise

    def _boundary(self, name):
        if self._fault_hook:
            self._fault_hook(name)

    def _record_quarantine(self, path, reason, index=None):
        """Remember excluded originals without modifying them or guessing a slot."""
        if not hasattr(self, "_quarantined"):
            self._quarantined = []
        item = dict(path=str(path), index=index, reason=str(reason))
        if item not in self._quarantined:
            self._quarantined.append(item)
        label = f"Member {index + 1}" if index is not None else Path(path).name
        self._issue(f"{label} was isolated: {reason}. Original preserved; choose a replacement.")

    def _quarantine_member(self, index, reason):
        """Remove a failed handle; its original file remains untouched."""
        with self._io:
            self._record_quarantine(self._paths.get(index, ""), reason, index)
            lock = self._locks.pop(index, None)
            if lock:
                with contextlib.suppress(Exception):
                    lock.release()
            file = self._files.pop(index, None)
            if file:
                with contextlib.suppress(Exception):
                    file.close()
            self._headers.pop(index, None)
            self._bad.discard(index)

    def _member_header(self, index, common=None, **fields):
        header = dict(self.h if common is None else common, index=index, **fields)
        if header["version"] >= 2:
            header["member_id"] = header["member_roster"][index]
        return header

    def _select_membership(self):
        """Accept only the current roster; disconnected obsolete copies stay untouched."""
        latest = max(self._headers.values(), key=lambda h: (h.get("membership_epoch", 0), h["version"]))
        identity = _membership_identity(latest)
        for h in self._headers.values():
            if h.get("membership_epoch", 0) == identity[0] and _membership_identity(h) != identity:
                raise UnrecoverableError("Conflicting member replacement histories; preserve all originals.")
        for index, h in tuple(self._headers.items()):
            if _member_id(h) != identity[1][index]:
                self._quarantine_member(index, "This incarnation was retired by a completed replacement")
        if len(self._files) < self.k:
            raise UnrecoverableError(f"At least {self.k} current, readable members are required after isolating failed or retired copies.")
        common = max(self._headers.values(), key=lambda h: h["epoch"]).copy()
        if latest["version"] >= 2:
            common.update(version=latest["version"], membership_epoch=identity[0], member_roster=list(identity[1]))
            common["member_id"] = common["member_roster"][common["index"]]
        self.h = common

    def _upgrade_membership_format(self):
        """Persist a version gate before retirement so legacy writers refuse the array."""
        generation, roster = _membership_identity(self.h)
        version = max(2, self.h["version"])
        common = dict(self.h, version=version, membership_epoch=generation, member_roster=list(roster))
        try:
            for index in self._files:
                h = dict(self._headers[index], version=version, membership_epoch=generation,
                         member_roster=list(roster), member_id=roster[index])
                for offset in (0, HEADER_SIZE):
                    self._write_member(index, offset, _header_bytes(h))
                    self._flush([index])
                self._headers[index] = h
            self.h = self._member_header(self.h["index"], common)
        except BaseException:
            self._needs_recovery = True
            raise

    def _issue(self, message, index=None):
        if message not in self._issues or (index is not None and index not in self._bad):
            self.health_revision += 1
        if message not in self._issues and len(self._issues) < 100:
            self._issues.append(message)
        if index is not None:
            self._bad.add(index)

    def _new_db(self):
        self._usage_cache = None
        if self._db:
            self._db.close()
        self._db = sqlite3.connect(str(Path(self._scratch.name) / "catalog.sqlite"),
                                   check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;
            PRAGMA cache_size=-8192;
            DROP TABLE IF EXISTS entries; DROP TABLE IF EXISTS extents;
            DROP TABLE IF EXISTS free_ranges;
            DROP TABLE IF EXISTS allocations;
            CREATE TABLE entries(id TEXT PRIMARY KEY,parent_id TEXT,name TEXT NOT NULL,
              is_dir INTEGER NOT NULL,size INTEGER NOT NULL,modified REAL NOT NULL,
              complete INTEGER NOT NULL,digest TEXT);
            CREATE INDEX children ON entries(parent_id,complete,name,id);
            CREATE TABLE extents(file_id TEXT NOT NULL,start INTEGER NOT NULL,
              count INTEGER NOT NULL,ordinal INTEGER NOT NULL);
            CREATE INDEX ranges ON extents(start);
            CREATE INDEX file_ranges ON extents(file_id,ordinal);
            CREATE TABLE free_ranges(start INTEGER PRIMARY KEY,count INTEGER NOT NULL);
            CREATE TABLE allocations(file_id TEXT NOT NULL,start INTEGER NOT NULL,
              count INTEGER NOT NULL,ordinal INTEGER NOT NULL);
        """)
        self._db.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)",
                         ("root", None, "RAIDiant", 1, 0, 0, 1, None))

    def _load_catalog(self, progress=None):
        try:
            return self._load_catalog_impl(progress)
        except BaseException:
            self._needs_recovery = True
            self._loading = False
            raise

    def _load_catalog_impl(self, progress=None):
        """Merge only identical/prefix metadata chains, never vote conflicting data."""
        with self._db_lock, self._io:
            for index, file in tuple(self._files.items()):
                try:
                    self._check_member_identity(index)
                    h = max(_read_headers(file), key=_header_order)
                except UnrecoverableError:
                    raise
                except (OSError, RaidError) as exc:
                    self._quarantine_member(index, str(exc))
                    continue
                if _geometry(h) != _geometry(self.h) or h["index"] != index:
                    raise UnrecoverableError("Member identity changed during the session; preserve all copies.")
                self._headers[index] = h
            if len(self._files) < self.k:
                raise UnrecoverableError(f"At least {self.k} readable members are required after isolating failed files.")
            self._select_membership()
            for h in self._headers.values():
                if h["epoch"] == self.h["epoch"] and _checkpoint_identity(h) != _checkpoint_identity(self.h):
                    raise UnrecoverableError("Conflicting metadata checkpoints; normal assembly is unsafe.")
            base = 8192 + self.h["bank"] * self.h["bank_size"]
            limit = base + self.h["bank_size"]
            readers = set()
            dirty = set()
            for index, file in tuple(self._files.items()):
                pos, seq, previous = 0, 1, ZERO_HASH
                try:
                    while pos < self.h["checkpoint_bytes"]:
                        record = _read_record(file, base + pos, limit, seq, previous)
                        if record is None:
                            raise RaidError("Incomplete checkpoint.")
                        raw, previous, _ = record
                        pos += len(raw)
                        seq += 1
                    if (pos != self.h["checkpoint_bytes"] or seq - 1 != self.h["checkpoint_seq"] or
                            previous.hex() != self.h["checkpoint_hash"]):
                        raise RaidError("Checkpoint checksum mismatch.")
                    readers.add(index)
                except OSError as exc:
                    self._quarantine_member(index, f"Metadata read failed: {exc}")
                except RaidError:
                    dirty.add(index)
            if len(self._files) < self.k:
                raise UnrecoverableError(f"Metadata failures leave fewer than {self.k} readable members.")
            if not readers:
                raise UnrecoverableError("No complete current metadata checkpoint survives.")
            self._new_db()
            self._loading = True
            if self._canonical:
                self._canonical.close()
            self._canonical = tempfile.TemporaryFile(dir=self._scratch.name)
            position, seq, previous = 0, 1, ZERO_HASH
            heads = {}
            while readers:
                records = {}
                for index in tuple(readers):
                    try:
                        record = _read_record(self._files[index], base + position, limit, seq, previous)
                    except OSError as exc:
                        self._quarantine_member(index, f"Metadata read failed: {exc}")
                        record = None
                    except RaidError:
                        record = None
                        dirty.add(index)
                    if record is None:
                        readers.remove(index)
                        heads[index] = position
                    else:
                        records[index] = record
                if len(self._files) < self.k:
                    raise UnrecoverableError(f"Metadata failures leave fewer than {self.k} readable members.")
                if not records:
                    break
                if len({r[1] for r in records.values()}) != 1:
                    raise UnrecoverableError("Members contain conflicting transaction histories. Preserve all copies.")
                raw, previous, event = next(iter(records.values()))
                try:
                    self._apply(event)
                except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                    raise UnrecoverableError("Invalid catalog transaction; normal assembly is unsafe.") from exc
                self._canonical.write(raw)
                position += len(raw)
                seq += 1
                if seq % 512 == 0:
                    _progress(progress, position, self.h["bank_size"], "Reading directory catalog")
            if position < self.h["checkpoint_bytes"]:
                raise UnrecoverableError("Metadata checkpoint is incomplete.")
            self._tail, self._seq, self._hash = position, seq - 1, previous
            self._metadata_bad = {i for i in self._files if heads.get(i, -1) != position or
                                  _checkpoint_identity(self._headers[i]) != _checkpoint_identity(self.h) or
                                  _membership_identity(self._headers[i]) != _membership_identity(self.h) or
                                  self._headers[i]["version"] != self.h["version"]} | (dirty & self._files.keys())
            incomplete = self._db.execute("SELECT COUNT(*) FROM entries WHERE complete=0").fetchone()[0]
            pending_allocation = self._db.execute("SELECT COUNT(*) FROM allocations").fetchone()[0]
            self._db.execute("DELETE FROM extents WHERE file_id IN (SELECT id FROM entries WHERE complete=0)")
            self._db.execute("DELETE FROM allocations")
            self._db.execute("DELETE FROM entries WHERE complete=0")
            self._validate_catalog()
            self._rebuild_free_ranges()
            self._loading = False
            self._needs_recovery = bool(self._metadata_bad or incomplete or pending_allocation)
            if self.dynamic:
                required = self._live_end()
                for index, size in self._expected_sizes.items():
                    if index not in self._files:
                        continue
                    if size < required:
                        self._issue(f"Member {index + 1} is shorter than its committed data; repair or replace it.", index)
                        self._needs_recovery = True
                    elif size > required:
                        self._needs_recovery = True
                        self._issue("Unused appended storage needs recovery before changing files.")
            if self._metadata_bad:
                self._issue("Member metadata needs synchronization; repair before changing files.")
            if incomplete:
                self._issue("An interrupted upload was discarded. Repair will checkpoint the recovered catalog.")

    def _validate_catalog(self):
        orphan = self._db.execute("""SELECT 1 FROM entries e LEFT JOIN entries p ON p.id=e.parent_id
            WHERE e.id<>'root' AND (p.id IS NULL OR p.is_dir<>1) LIMIT 1""").fetchone()
        if orphan:
            raise UnrecoverableError("Directory metadata has an invalid parent.")
        end = 0
        for row in self._db.execute("SELECT start,count,file_id FROM extents ORDER BY start"):
            if row["start"] < end or row["count"] < 1 or row["start"] + row["count"] > self.h["stripes"]:
                raise UnrecoverableError("Overlapping or out-of-range allocation metadata.")
            end = row["start"] + row["count"]
        # A cycle is impossible through supported operations; validate imported metadata anyway.
        reachable = self._db.execute("""WITH RECURSIVE tree(id) AS (
            SELECT 'root' UNION SELECT e.id FROM entries e JOIN tree t ON e.parent_id=t.id)
            SELECT COUNT(*) FROM tree""").fetchone()[0]
        if reachable != self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]:
            raise UnrecoverableError("Directory metadata contains a cycle.")
        for row in self._db.execute("SELECT id,size FROM entries WHERE is_dir=0"):
            ordinal = 0
            for extent in self._db.execute("SELECT count,ordinal FROM extents WHERE file_id=? ORDER BY ordinal", (row["id"],)):
                if extent["ordinal"] != ordinal:
                    raise UnrecoverableError("File extent sequence is incomplete.")
                ordinal += extent["count"]
            if ordinal != math.ceil(row["size"] / self.stripe_bytes):
                raise UnrecoverableError("File allocation does not match its size.")

    def _apply(self, event):
        self._usage_cache = None
        op = event["op"]
        if op == "init":
            return
        if op in ("entry", "snapshot_entry"):
            values = event["entry"]
            if len(values) != 8 or values[0] == "root":
                raise ValueError("Invalid entry")
            uuid.UUID(values[0])
            _valid_name(values[2])
            if values[3] not in (0, 1) or values[6] not in (0, 1) or type(values[4]) is not int or values[4] < 0:
                raise ValueError("Invalid entry values")
            self._db.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)", values)
        elif op in ("extent", "snapshot_extent"):
            values = event["extent"]
            if len(values) != 4 or any(type(v) is not int for v in values[1:]):
                raise ValueError("Invalid extent")
            identifier, start, count, ordinal = values
            if not self._loading:
                self._consume_free_range(start, count)
            previous = self._db.execute("SELECT rowid,* FROM extents WHERE file_id=? ORDER BY ordinal DESC LIMIT 1",
                                        (identifier,)).fetchone()
            if previous and previous["ordinal"] + previous["count"] == ordinal and previous["start"] + previous["count"] == start:
                self._db.execute("UPDATE extents SET count=count+? WHERE rowid=?", (count, previous["rowid"]))
            else:
                self._db.execute("INSERT INTO extents VALUES(?,?,?,?)", values)
            if op == "extent":
                self._db.execute("DELETE FROM allocations WHERE file_id=? AND start=?", (identifier, start))
        elif op == "allocate":
            values = event["extent"]
            if (not self.dynamic or len(values) != 4 or
                    any(type(v) is not int for v in values[1:])):
                raise ValueError("Invalid allocation intent")
            identifier, start, count, ordinal = values
            pending = self._db.execute("SELECT complete FROM entries WHERE id=?", (identifier,)).fetchone()
            if (pending is None or pending["complete"] or start < 0 or count < 1 or
                    start + count > self.h["stripes"] or ordinal < 0):
                raise ValueError("Invalid pending allocation")
            self._db.execute("INSERT INTO allocations VALUES(?,?,?,?)", values)
        elif op == "finish":
            if len(bytes.fromhex(event["digest"])) != 32:
                raise ValueError("Invalid content checksum")
            self._db.execute("UPDATE entries SET complete=1,size=?,digest=? WHERE id=? AND complete=0",
                             (event["size"], event["digest"], event["id"]))
        elif op in ("delete", "abandon"):
            if event["id"] == "root":
                raise ValueError("Cannot delete root")
            if not self._loading:
                for row in self._db.execute("""WITH RECURSIVE subtree(id) AS (SELECT ? UNION
                    SELECT e.id FROM entries e JOIN subtree s ON e.parent_id=s.id)
                    SELECT start,count FROM extents WHERE file_id IN (SELECT id FROM subtree)""", (event["id"],)):
                    self._release_free_range(row["start"], row["count"])
            self._db.execute("""WITH RECURSIVE subtree(id) AS (SELECT ? UNION
                SELECT e.id FROM entries e JOIN subtree s ON e.parent_id=s.id)
                DELETE FROM extents WHERE file_id IN (SELECT id FROM subtree)""", (event["id"],))
            self._db.execute("DELETE FROM allocations WHERE file_id=?", (event["id"],))
            self._db.execute("""WITH RECURSIVE subtree(id) AS (SELECT ? UNION
                SELECT e.id FROM entries e JOIN subtree s ON e.parent_id=s.id)
                DELETE FROM entries WHERE id IN (SELECT id FROM subtree)""", (event["id"],))
        elif op == "rename":
            _valid_name(event["name"])
            if event["id"] == "root":
                raise ValueError("Cannot rename root")
            self._db.execute("UPDATE entries SET name=?,modified=? WHERE id=?",
                             (event["name"], event["modified"], event["id"]))
        else:
            raise ValueError("Unknown metadata operation")

    @property
    def stripe_bytes(self):
        return self.k * self.h["chunk_size"]

    @property
    def dynamic(self):
        return self.h.get("storage") == "dynamic"

    @property
    def io_status(self):
        active = self._active_io
        return dict(active, seconds=time.monotonic() - active["started"]) if active else None

    @property
    def status(self):
        with self._db_lock:
            if self._usage_cache is None:
                used = self._db.execute("SELECT COALESCE(SUM(size),0) FROM entries WHERE complete=1 AND is_dir=0").fetchone()[0]
                allocated = self._db.execute("SELECT COALESCE(SUM(count),0) FROM extents").fetchone()[0]
                self._usage_cache = used, allocated
            used, allocated = self._usage_cache
        missing = sorted(set(range(self.n)) - self._files.keys())
        state = ("recovery_required" if self._needs_recovery or self._bad or self._integrity_failed else
                 "degraded" if missing else "read_only" if self._read_only or self._busy else "healthy")
        return dict(state=state, writable=state == "healthy" and not self._closed,
                    explicit_readonly=self._read_only, repairable=not self._read_only,
                    readable=not self._closed and not self._needs_recovery and len(self._files) >= self.k,
                    bad_members=sorted(self._bad | self._metadata_bad),
                    quarantined=list(self._quarantined), rebuilding=self._rebuilding,
                    integrity_failed=self._integrity_failed, last_verification=self._last_verification,
                    current_io=self.io_status,
                    n=self.n, parity=self.parity, k=self.k, uuid=self.h["uuid"],
                    member_size=self.h["member_size"], capacity=self.h["stripes"] * self.stripe_bytes,
                    used=used, free=(self.h["stripes"] - allocated) * self.stripe_bytes,
                    missing=missing, issues=list(self._issues), members=[
                        dict(index=i, path=str(self._paths.get(i, "")), state="missing" if i in missing else
                             "needs repair" if i in self._bad or i in self._metadata_bad else "present")
                        for i in range(self.n)])

    def try_status(self):
        """Nonblocking desktop snapshot during catalog recovery/publication."""
        if not self._db_lock.acquire(blocking=False):
            return None
        try:
            return self.status
        finally:
            self._db_lock.release()

    def try_list_dir(self, parent_id="root", offset=0, limit=500):
        if not self._db_lock.acquire(blocking=False):
            return None
        try:
            return self.list_dir(parent_id, offset, limit)
        finally:
            self._db_lock.release()

    def _require_write(self):
        if self._closed:
            raise RaidError("Array is closed.")
        if not self.status["writable"]:
            raise RaidError("This array is read-only. Repair or rebuild missing members before changing files.")

    @contextlib.contextmanager
    def _io_activity(self, index, operation):
        # A watchdog can report slow native I/O without killing a thread in the
        # middle of a durability boundary. OS/device timeouts still apply.
        self._active_io = dict(index=index, operation=operation, started=time.monotonic())
        try:
            yield
        finally:
            self._active_io = None

    def _begin_rebuild_reads(self):
        with self._reader_gate:
            self._exclusive_pending = False
            self._rebuilding = True

    def _end_rebuild_reads(self):
        with self._reader_gate:
            self._exclusive_pending = True
            self._rebuilding = False
            while self._readers:
                self._reader_gate.wait(timeout=0.25)
            self._exclusive_pending = False

    @contextlib.contextmanager
    def _export_access(self):
        # An ordinary export owns the operation lock. During reconstruction,
        # the immutable catalog and source handles may be shared until publish.
        owns_operation = self._operation.acquire(blocking=False)
        if not owns_operation:
            with self._reader_gate:
                if not self._rebuilding or self._exclusive_pending:
                    raise RaidError("Another exclusive storage operation is in progress.")
                self._readers += 1
        try:
            if self._closed:
                raise RaidError("Array is closed.")
            if self._needs_recovery:
                raise RecoveryRequired("Repair the interrupted transaction state before exporting files.")
            yield
        finally:
            if owns_operation:
                self._operation.release()
            else:
                with self._reader_gate:
                    self._readers -= 1
                    self._reader_gate.notify_all()

    @contextlib.contextmanager
    def _mutating(self):
        if not self._operation.acquire(blocking=False):
            raise RaidError("Another storage operation is in progress.")
        try:
            self._require_write()
            yield
        finally:
            self._operation.release()

    def _write_member(self, index, offset, data):
        try:
            with self._io, self._io_activity(index, "writing"):
                file = self._files[index]
                self._check_member_identity(index)
                file.seek(offset)
                _write_all(file, data)
            self._boundary("member_write")
        except OSError as exc:
            self._needs_recovery = True
            self._issue(f"Member {index + 1} write failed: {exc}", index)
            raise RecoveryRequired("A member write failed. Mutations stopped; repair or replace the member.") from exc

    def _check_member_identity(self, index):
        opened = os.fstat(self._files[index].fileno())
        current = os.stat(self._paths[index])
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("Member path was replaced during this session")
        if opened.st_size != self._expected_sizes.get(index, self.h["member_size"]):
            raise OSError("Member size changed during this session")

    def _flush(self, indices=None):
        for index in list(self._files if indices is None else indices):
            try:
                with self._io, self._io_activity(index, "flushing"):
                    self._check_member_identity(index)
                    durable_flush(self._files[index])
                self._boundary("member_flush")
            except OSError as exc:
                self._needs_recovery = True
                self._issue(f"Member {index + 1} synchronization failed: {exc}", index)
                raise RecoveryRequired("A member could not persist data. Repair is required.") from exc

    def _append(self, event):
        raw, digest = _record(self._seq + 1, self._hash, event)
        # Keep a small emergency reserve so a full catalog can still delete an
        # entry. Ordinary operations compact before consuming this reserve.
        reserve = 0 if event["op"] in ("delete", "abandon") else max(8192, self.h["bank_size"] // 8)
        if self._tail + len(raw) + LOG_STRUCT.size + reserve > self.h["bank_size"]:
            self._checkpoint()
            raw, digest = _record(self._seq + 1, self._hash, event)
        if self._tail + len(raw) + LOG_STRUCT.size + reserve > self.h["bank_size"]:
            raise RaidError("The array's metadata region is full. Delete entries to reclaim metadata capacity.")
        offset = 8192 + self.h["bank"] * self.h["bank_size"] + self._tail
        try:
            for index in self._files:
                self._write_member(index, offset, raw + bytes(LOG_STRUCT.size))
                self._boundary("metadata_member_written")
            self._flush()
            self._boundary("metadata_durable")
            with self._db_lock:
                self._apply(event)
            self._canonical.seek(self._tail)
            self._canonical.write(raw)
            self._tail += len(raw)
            self._seq += 1
            self._hash = digest
        except BaseException:
            self._needs_recovery = True
            raise

    def _checkpoint(self):
        # Construct and size the entire checkpoint on disk before modifying a member.
        with tempfile.TemporaryFile(dir=self._scratch.name) as snapshot:
            seq, previous, length = 0, ZERO_HASH, 0
            def add(event):
                nonlocal seq, previous, length
                seq += 1
                raw, previous = _record(seq, previous, event)
                snapshot.write(raw)
                length += len(raw)
            add({"op": "init"})
            with self._db_lock:
                for row in self._db.execute("SELECT * FROM entries WHERE id<>'root' ORDER BY id"):
                    add({"op": "snapshot_entry", "entry": list(row)})
                for row in self._db.execute("SELECT * FROM extents ORDER BY start"):
                    add({"op": "snapshot_extent", "extent": list(row)})
                for row in self._db.execute("SELECT * FROM allocations ORDER BY start"):
                    add({"op": "allocate", "extent": list(row)})
            if length + LOG_STRUCT.size >= self.h["bank_size"]:
                raise RaidError("Metadata capacity is exhausted; this operation cannot be committed.")
            new = dict(self.h, bank=1 - self.h["bank"], epoch=self.h["epoch"] + 1,
                       checkpoint_bytes=length, checkpoint_seq=seq, checkpoint_hash=previous.hex())
            try:
                base = 8192 + new["bank"] * new["bank_size"]
                snapshot.seek(0)
                offset = 0
                while chunk := snapshot.read(1024**2):
                    for index in self._files:
                        self._write_member(index, base + offset, chunk)
                    offset += len(chunk)
                for index in self._files:
                    self._write_member(index, base + length, bytes(LOG_STRUCT.size))
                self._flush()
                self._boundary("checkpoint_data_durable")
                for index in self._files:
                    h = self._member_header(index, new)
                    self._write_member(index, (new["epoch"] % 2) * HEADER_SIZE, _header_bytes(h))
                    # Once upgraded, erase the legacy writer's fallback header.
                    if h["version"] >= 2:
                        self._flush([index])
                        self._write_member(index, (1 - new["epoch"] % 2) * HEADER_SIZE, _header_bytes(h))
                    self._headers[index] = h
                    self._boundary("checkpoint_header_written")
                self._flush()
                self.h = new
                self._tail, self._seq, self._hash = length, seq, previous
                self._canonical.seek(0)
                self._canonical.truncate()
                snapshot.seek(0)
                while chunk := snapshot.read(1024**2):
                    self._canonical.write(chunk)
            except BaseException:
                self._needs_recovery = True
                raise

    def get_entry(self, entry_id):
        with self._db_lock:
            row = self._db.execute("SELECT * FROM entries WHERE id=? AND complete=1", (entry_id,)).fetchone()
        if row is None:
            raise RaidError("The selected entry no longer exists.")
        return dict(row)

    def list_dir(self, parent_id="root", offset=0, limit=500):
        if not self.get_entry(parent_id)["is_dir"]:
            raise RaidError("Not a directory.")
        with self._db_lock:
            return [dict(r) for r in self._db.execute("""SELECT * FROM entries WHERE parent_id=?
                AND complete=1 ORDER BY is_dir DESC,name,id LIMIT ? OFFSET ?""",
                (parent_id, min(max(int(limit), 1), 2000), max(int(offset), 0)))]

    def _unique_name(self, parent_id, name):
        _valid_name(name)
        candidate, number = name, 2
        with self._db_lock:
            while self._db.execute("SELECT 1 FROM entries WHERE parent_id=? AND name=?", (parent_id, candidate)).fetchone():
                candidate = f"{name} ({number})"
                number += 1
        return candidate

    def _mkdir(self, name, parent_id):
        if not self.get_entry(parent_id)["is_dir"]:
            raise RaidError("Destination must be a directory.")
        identifier = str(uuid.uuid4())
        self._append({"op": "entry", "entry": [identifier, parent_id,
                     self._unique_name(parent_id, name), 1, 0, time.time(), 1, None]})
        return identifier

    def mkdir(self, name, parent_id="root"):
        with self._mutating():
            return self._mkdir(name, parent_id)

    def rename(self, entry_id, name):
        _valid_name(name)
        with self._mutating():
            entry = self.get_entry(entry_id)
            if entry_id == "root":
                raise RaidError("The array root cannot be renamed.")
            with self._db_lock:
                clash = self._db.execute("SELECT 1 FROM entries WHERE parent_id=? AND name=? AND id<>?",
                                         (entry["parent_id"], name, entry_id)).fetchone()
            if clash:
                raise RaidError("An entry with that exact name already exists in this directory.")
            self._append({"op": "rename", "id": entry_id, "name": name, "modified": time.time()})

    def delete(self, entry_id, progress=None, cancel=None):
        with self._mutating():
            self.get_entry(entry_id)
            if entry_id == "root":
                raise RaidError("The array root cannot be deleted.")
            _cancel(cancel)
            self._append({"op": "delete", "id": entry_id})
            self._trim_unused_tail()
            _progress(progress, 1, 1, "Entry deleted")

    def _free_run(self, wanted):
        with self._db_lock:
            row = self._db.execute("SELECT start,count FROM free_ranges ORDER BY start LIMIT 1").fetchone()
        if row:
            return row["start"], min(wanted, row["count"])
        raise RaidError("The array is full. Previously completed files are retained.")

    def _rebuild_free_ranges(self):
        """One streaming pass on open; ordinary allocation uses the B-tree."""
        self._db.execute("DELETE FROM free_ranges")
        position = 0
        for row in self._db.execute("SELECT start,count FROM extents ORDER BY start"):
            if row["start"] > position:
                self._db.execute("INSERT INTO free_ranges VALUES(?,?)", (position, row["start"] - position))
            position = row["start"] + row["count"]
        if position < self.h["stripes"]:
            self._db.execute("INSERT INTO free_ranges VALUES(?,?)", (position, self.h["stripes"] - position))

    def _consume_free_range(self, start, count):
        row = self._db.execute("SELECT start,count FROM free_ranges WHERE start<=? ORDER BY start DESC LIMIT 1",
                               (start,)).fetchone()
        if row is None or start + count > row["start"] + row["count"] or count < 1:
            raise RaidError("The allocator detected an overlapping extent.")
        self._db.execute("DELETE FROM free_ranges WHERE start=?", (row["start"],))
        if start > row["start"]:
            self._db.execute("INSERT INTO free_ranges VALUES(?,?)", (row["start"], start - row["start"]))
        end = start + count
        if end < row["start"] + row["count"]:
            self._db.execute("INSERT INTO free_ranges VALUES(?,?)", (end, row["start"] + row["count"] - end))

    def _release_free_range(self, start, count):
        previous = self._db.execute("SELECT start,count FROM free_ranges WHERE start<? ORDER BY start DESC LIMIT 1",
                                    (start,)).fetchone()
        if previous and previous["start"] + previous["count"] == start:
            self._db.execute("DELETE FROM free_ranges WHERE start=?", (previous["start"],))
            start, count = previous["start"], previous["count"] + count
        following = self._db.execute("SELECT start,count FROM free_ranges WHERE start=?", (start + count,)).fetchone()
        if following:
            self._db.execute("DELETE FROM free_ranges WHERE start=?", (following["start"],))
            count += following["count"]
        self._db.execute("INSERT INTO free_ranges VALUES(?,?)", (start, count))

    def _shard_bytes(self, token, stripe, role, data):
        prefix = struct.pack("<8s16sQI", SHARD_MAGIC, token, stripe, role)
        digest = hashlib.sha256(prefix + data).digest()
        return SHARD_STRUCT.pack(SHARD_MAGIC, token, stripe, role, digest).ljust(SHARD_HEADER, b"\0") + data

    def _stripe_offset(self, stripe):
        return 8192 + 2 * self.h["bank_size"] + stripe * (self.h["chunk_size"] + SHARD_HEADER)

    def _live_end(self):
        """Highest committed stripe; interior free space remains reusable."""
        with self._db_lock:
            end = self._db.execute("""SELECT COALESCE(MAX(x.start+x.count),0)
                FROM extents x JOIN entries e ON e.id=x.file_id WHERE e.complete=1""").fetchone()[0]
        return self._stripe_offset(end)

    def _reserve_member(self, index, end):
        if not self.dynamic or end <= self._expected_sizes[index]:
            return
        with self._io, self._io_activity(index, "allocating"):
            self._check_member_identity(index)
            try:
                extend_file(self._files[index], end)
            except OSError as exc:
                self._needs_recovery = True
                self._issue(f"Member {index + 1} could not allocate storage: {exc}", index)
                reason = ("Physical storage is full" if exc.errno in (errno.ENOSPC, errno.EDQUOT)
                          or getattr(exc, "winerror", None) in (39, 112) else "Member allocation failed")
                raise RecoveryRequired(f"{reason} on member {index + 1}. Upload stopped; recovery will discard the unfinished file.") from exc
            finally:
                # This operation may have extended only part of the tail. It is
                # our own known change, not permission to accept later changes.
                self._expected_sizes[index] = os.fstat(self._files[index].fileno()).st_size
        self._boundary("allocation_member_durable")

    def _prepare_extent(self, identifier, start, count, ordinal):
        """Journal allocation intent before physically extending any member."""
        if not self.dynamic:
            return
        with self._db_lock:
            row = self._db.execute("SELECT start,count FROM free_ranges WHERE start<=? ORDER BY start DESC LIMIT 1",
                                   (start,)).fetchone()
            if row is None or count < 1 or start + count > row["start"] + row["count"]:
                raise RaidError("The allocator detected an overlapping extent.")
        self._append({"op": "allocate", "extent": [identifier, start, count, ordinal]})
        self._boundary("allocation_intent_durable")
        for index in self._files:
            self._reserve_member(index, self._stripe_offset(start + count))

    def _trim_unused_tail(self):
        """Run only after the catalog forgetting unfinished extents is durable."""
        if not self.dynamic:
            return
        required = self._live_end()
        for index, file in self._files.items():
            with self._io, self._io_activity(index, "reclaiming"):
                self._check_member_identity(index)
                if self._expected_sizes[index] < required:
                    raise RecoveryRequired(f"Member {index + 1} needs reconstruction before storage can be reclaimed.")
                if self._expected_sizes[index] == required:
                    continue
                try:
                    file.truncate(required)
                    durable_flush(file)
                except OSError as exc:
                    self._needs_recovery = True
                    self._issue(f"Member {index + 1} storage cleanup failed: {exc}", index)
                    raise RecoveryRequired("Storage cleanup could not finish. Repair before changing files.") from exc
                finally:
                    self._expected_sizes[index] = os.fstat(file.fileno()).st_size
            self._boundary("tail_reclaimed")

    def _abandon_upload(self, identifier):
        """Never undo a finish record which reached disk before an exception."""
        if self.dynamic and self._needs_recovery:
            self._load_catalog()
            row = self._db.execute("SELECT complete FROM entries WHERE id=?", (identifier,)).fetchone()
            # Replay may already have dropped the incomplete entry. A complete
            # entry is retained, even if its acknowledgement was interrupted.
            if row is not None and not row["complete"]:
                self._append({"op": "abandon", "id": identifier})
            self._checkpoint()
            self._trim_unused_tail()
            self._metadata_bad.clear()
            # Keep the failure visible/read-only until an explicit repair.
            self._needs_recovery = True
        elif not self._needs_recovery:
            self._append({"op": "abandon", "id": identifier})
            self._trim_unused_tail()

    def _write_stripe(self, stripe, token, content):
        chunk = self.h["chunk_size"]
        content = content.ljust(self.stripe_bytes, b"\0")
        encoded = self.codec.encode([content[i * chunk:(i + 1) * chunk] for i in range(self.k)])
        for role, data in enumerate(encoded):
            index = (role + stripe) % self.n
            self._write_member(index, self._stripe_offset(stripe), self._shard_bytes(token, stripe, role, data))
        self._boundary("stripe_written")

    def _read_stripe(self, stripe, token, all_shards=False):
        valid, bad = {}, set()
        order = list(range(self.k)) + list(range(self.k, self.n))
        for role in order:
            index = (role + stripe) % self.n
            if index not in self._files:
                bad.add(index)
                continue
            try:
                with self._io, self._io_activity(index, "reading"):
                    self._check_member_identity(index)
                    file = self._files[index]
                    file.seek(self._stripe_offset(stripe))
                    raw = file.read(self.h["chunk_size"] + SHARD_HEADER)
                if len(raw) != self.h["chunk_size"] + SHARD_HEADER:
                    raise ValueError("short read")
                magic, actual_token, actual_stripe, actual_role, digest = SHARD_STRUCT.unpack(raw[:SHARD_STRUCT.size])
                data = raw[SHARD_HEADER:]
                prefix = struct.pack("<8s16sQI", magic, actual_token, actual_stripe, actual_role)
                if ((magic, actual_token, actual_stripe, actual_role) != (SHARD_MAGIC, token, stripe, role) or
                        hashlib.sha256(prefix + data).digest() != digest):
                    raise ValueError("checksum or identity mismatch")
                valid[role] = data
            except (OSError, ValueError, struct.error) as exc:
                bad.add(index)
                self._issue(f"Member {index + 1} has unreadable data at stripe {stripe}: {exc}", index)
            if not all_shards and len(valid) == self.k:
                break
        if len(valid) < self.k:
            self._integrity_failed = True
            raise UnrecoverableError(f"Stripe {stripe} has only {len(valid)} verified shards; {self.k} are needed. No data was guessed.")
        if not all_shards and all(i in valid for i in range(self.k)):
            return [valid[i] for i in range(self.k)], bad
        restored = self.codec.reconstruct(valid)
        if any(restored[i] != data for i, data in valid.items()):
            self._integrity_failed = True
            self._issue(f"Stripe {stripe} contains ambiguous data/parity inconsistency.")
            raise UnrecoverableError(f"Stripe {stripe} has conflicting data with valid checksums; automatic repair is unsafe.")
        return restored, bad

    def _import_file(self, path, parent_id, progress, cancel):
        path = Path(path)
        if _is_link(path) or not path.is_file() or is_hidden(path):
            return False
        if any(os.path.samefile(path, member) for member in self._paths.values()):
            raise RaidError("An active member file cannot be imported into its own array.")
        identifier = str(uuid.uuid4())
        token = uuid.UUID(identifier).bytes
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb", buffering=0) as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                return False
            if math.ceil(before.st_size / self.stripe_bytes) * self.stripe_bytes > self.status["free"]:
                raise RaidError(f"Not enough array space for {path.name}.")
            self._append({"op": "entry", "entry": [identifier, parent_id,
                         self._unique_name(parent_id, path.name), 0, 0, before.st_mtime, 0, None]})
            try:
                total, ordinal = 0, 0
                content_hash = hashlib.sha256()
                batch = max(1, (16 * 1024**2) // self.stripe_bytes)
                while total < before.st_size:
                    _cancel(cancel)
                    wanted = min(batch, math.ceil((before.st_size - total) / self.stripe_bytes))
                    start, count = self._free_run(wanted)
                    self._prepare_extent(identifier, start, count, ordinal)
                    for delta in range(count):
                        _cancel(cancel)
                        amount = min(self.stripe_bytes, before.st_size - total)
                        data = _read_exact(source, amount)
                        if len(data) != amount:
                            raise RaidError("The source file changed or became unreadable during import.")
                        self._write_stripe(start + delta, token, data)
                        content_hash.update(data)
                        total += len(data)
                        _progress(progress, total, before.st_size, f"Importing {path.name}")
                    self._flush()
                    self._boundary("data_durable")
                    self._append({"op": "extent", "extent": [identifier, start, count, ordinal]})
                    ordinal += count
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or source.read(1):
                    raise RaidError("The source file changed during import; its incomplete copy was discarded.")
                self._boundary("before_file_commit")
                self._append({"op": "finish", "id": identifier, "size": total, "digest": content_hash.hexdigest()})
                return True
            except BaseException:
                with contextlib.suppress(Exception):
                    self._abandon_upload(identifier)
                raise

    def upload_files(self, paths, parent_id="root", progress=None, cancel=None):
        with self._mutating():
            if not self.get_entry(parent_id)["is_dir"]:
                raise RaidError("Destination must be a directory.")
            count, skipped = 0, 0
            for path in paths:
                _cancel(cancel)
                if self._import_file(path, parent_id, progress, cancel):
                    count += 1
                else:
                    skipped += 1
            return dict(files=count, skipped=skipped)

    def upload_folder(self, path, parent_id="root", progress=None, cancel=None):
        path = Path(path)
        if _is_link(path) or not path.is_dir() or is_hidden(path):
            raise RaidError("Choose a visible, ordinary folder; links are not followed.")
        with self._mutating():
            destination = self._mkdir(path.name, parent_id)
            count, skipped = 0, 0
            # Only the current directory's iterator and ancestry are resident.
            stack = [(os.scandir(path), destination)]
            try:
                while stack:
                    _cancel(cancel)
                    iterator, parent = stack[-1]
                    try:
                        child = next(iterator)
                    except StopIteration:
                        iterator.close()
                        stack.pop()
                        continue
                    item = Path(child.path)
                    if _is_link(item) or is_hidden(item):
                        skipped += 1
                    elif child.is_dir(follow_symlinks=False):
                        child_id = self._mkdir(child.name, parent)
                        stack.append((os.scandir(item), child_id))
                    elif child.is_file(follow_symlinks=False):
                        count += int(self._import_file(item, parent, progress, cancel))
                    else:
                        skipped += 1
            finally:
                for iterator, _ in stack:
                    iterator.close()
            return dict(files=count, skipped=skipped, folder_id=destination)

    def _extents(self, entry_id):
        ordinal = -1
        while True:
            with self._db_lock:
                row = self._db.execute("SELECT * FROM extents WHERE file_id=? AND ordinal>? ORDER BY ordinal LIMIT 1",
                                       (entry_id, ordinal)).fetchone()
            if row is None:
                return
            ordinal = row["ordinal"]
            yield row

    def _export_file(self, entry, parent, progress, cancel):
        path = unique_export_path(Path(parent), entry["name"])
        with ExportStage(parent) as stage:
            with stage.target as target:
                remaining, done = entry["size"], 0
                content_hash = hashlib.sha256()
                for extent in self._extents(entry["id"]):
                    for stripe in range(extent["start"], extent["start"] + extent["count"]):
                        _cancel(cancel)
                        data, _ = self._read_stripe(stripe, uuid.UUID(entry["id"]).bytes)
                        for shard in data[:self.k]:
                            part = shard[:min(remaining, len(shard))]
                            _write_all(target, part)
                            content_hash.update(part)
                            remaining -= len(part)
                            done += len(part)
                        _progress(progress, done, entry["size"], f"Exporting {entry['name']}")
                if remaining or content_hash.hexdigest() != entry["digest"]:
                    self._integrity_failed = True
                    self._issue(f"Content checksum failed for {entry['name']}.")
                    raise UnrecoverableError("File content checksum mismatch. No completed host file was published.")
                durable_flush(target)
            try:
                stage.publish(path)
            except FileExistsError:
                raise RaidError("The export destination appeared during copying; no existing file was overwritten.")
            return path

    def export(self, entry_id, destination_dir, progress=None, cancel=None):
        with self._export_access():
            return self._export(entry_id, destination_dir, progress, cancel)

    def _export(self, entry_id, destination_dir, progress=None, cancel=None):
        parent = Path(destination_dir).resolve()
        if not parent.is_dir():
            raise RaidError("Choose an existing destination directory.")
        entry = self.get_entry(entry_id)
        if not entry["is_dir"]:
            return self._export_file(entry, parent, progress, cancel)
        root = unique_export_path(parent, entry["name"])
        root.mkdir()
        # Each directory is read in bounded pages; only traversal ancestry and
        # pending siblings from these pages are retained in memory.
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", dir=self._scratch.name) as mapping:
            mapping.write("Original name\tExported relative path\n")
            stack = [(entry_id, root, 0)]
            while stack:
                _cancel(cancel)
                identifier, directory, offset = stack.pop()
                children = self.list_dir(identifier, offset=offset, limit=100)
                if len(children) == 100:
                    stack.append((identifier, directory, offset + 100))
                for child in children:
                    if child["is_dir"]:
                        target = unique_export_path(directory, child["name"])
                        target.mkdir()
                        stack.append((child["id"], target, 0))
                    else:
                        target = self._export_file(child, directory, progress, cancel)
                    mapping.write(json.dumps(child["name"], ensure_ascii=False) + "\t" +
                                  json.dumps(str(target.relative_to(root)), ensure_ascii=False) + "\n")
            map_path = unique_export_path(root, "RAIDiant export names.tsv")
            mapping.seek(0)
            with open(map_path, "x", encoding="utf-8") as out:
                while text := mapping.read(65536):
                    out.write(text)
        return root

    def _all_stripes(self):
        start = -1
        while True:
            with self._db_lock:
                row = self._db.execute("""SELECT x.* FROM extents x JOIN entries e ON e.id=x.file_id
                    WHERE e.complete=1 AND x.start>? ORDER BY x.start LIMIT 1""", (start,)).fetchone()
            if row is None:
                return
            start = row["start"]
            for stripe in range(start, start + row["count"]):
                yield stripe, uuid.UUID(row["file_id"]).bytes

    def _probe_members(self):
        for index in tuple(self._files):
            try:
                with self._io, self._io_activity(index, "checking member"):
                    self._check_member_identity(index)
                    headers = _read_headers(self._files[index])
                newest = max(headers, key=_header_order)
                if _geometry(newest) != _geometry(self.h):
                    raise RaidError("Member identity or geometry changed")
                if len(headers) < 2 or _checkpoint_identity(newest) != _checkpoint_identity(self.h):
                    self._header_bad.add(index)
                    self._metadata_bad.add(index)
                    self._needs_recovery = True
                    self._issue(f"Member {index + 1} header copies need recovery.", index)
                else:
                    self._header_bad.discard(index)
            except (OSError, RaidError) as exc:
                self._header_bad.add(index)
                self._issue(f"Member {index + 1} health check failed: {exc}", index)

    def health_check(self):
        """Probe identity, length and both headers; does not replace a scrub."""
        if not self._operation.acquire(blocking=False):
            return self.status
        try:
            if self._closed:
                raise RaidError("Array is closed.")
            self._probe_members()
            return self.status
        finally:
            self._operation.release()

    def _file_entries(self):
        last = ""
        while True:
            with self._db_lock:
                row = self._db.execute("SELECT * FROM entries WHERE complete=1 AND is_dir=0 AND id>? ORDER BY id LIMIT 1", (last,)).fetchone()
            if row is None:
                return
            last = row["id"]
            yield dict(row)

    def _entry_path(self, entry):
        parts = [entry["name"]]
        parent = entry["parent_id"]
        while parent and parent != "root":
            item = self.get_entry(parent)
            parts.append(item["name"])
            parent = item["parent_id"]
        # JSON arrays retain exact names even when names contain slashes.
        return list(reversed(parts))

    def _scrub(self, progress=None, cancel=None, repair=False):
        with self._db_lock:
            total = self._db.execute("SELECT COALESCE(SUM(count),0) FROM extents").fetchone()[0]
        checked = repaired = damaged = files_checked = damaged_files = 0
        summaries = []
        report_path = Path(self._scratch.name) / f"integrity-{uuid.uuid4().hex}.jsonl"
        with report_path.open("x", encoding="utf-8") as report:
            def record(value):
                report.write(json.dumps(value, ensure_ascii=False) + "\n")
            record(dict(type="array", uuid=self.h["uuid"], checked_at=time.time(),
                        metadata_issues=sorted(self._metadata_bad | self._header_bad),
                        missing_members=sorted(set(range(self.n)) - self._files.keys()),
                        quarantined=self._quarantined))
            for entry in self._file_entries():
                _cancel(cancel)
                remaining = entry["size"]
                content_hash = hashlib.sha256()
                failed = False
                token = uuid.UUID(entry["id"]).bytes
                for extent in self._extents(entry["id"]):
                    for stripe in range(extent["start"], extent["start"] + extent["count"]):
                        _cancel(cancel)
                        amount = min(remaining, self.stripe_bytes)
                        try:
                            shards, bad = self._read_stripe(stripe, token, all_shards=True)
                            damaged += len(bad)
                            if bad & self._files.keys():
                                record(dict(type="stripe", file_id=entry["id"], stripe=stripe,
                                            members=sorted(bad & self._files.keys()), recoverable=True))
                            for shard in shards[:self.k]:
                                part = shard[:min(amount, len(shard))]
                                content_hash.update(part)
                                amount -= len(part)
                            if repair:
                                for index in bad & self._files.keys():
                                    role = (index - stripe) % self.n
                                    self._reserve_member(index, self._stripe_offset(stripe + 1))
                                    self._write_member(index, self._stripe_offset(stripe), self._shard_bytes(token, stripe, role, shards[role]))
                                    repaired += 1
                        except UnrecoverableError as exc:
                            failed = True
                            record(dict(type="stripe", file_id=entry["id"], stripe=stripe,
                                        recoverable=False, error=str(exc)))
                        remaining -= min(remaining, self.stripe_bytes)
                        checked += 1
                        _progress(progress, checked, total, f"{'Repairing' if repair else 'Verifying'} {entry['name']}")
                if not failed and (remaining or content_hash.hexdigest() != entry["digest"]):
                    failed = True
                    record(dict(type="checksum", file_id=entry["id"], error="Whole-file checksum mismatch"))
                files_checked += 1
                if failed:
                    damaged_files += 1
                    detail = dict(file_id=entry["id"], path=self._entry_path(entry))
                    record(dict(type="damaged_file", **detail))
                    if len(summaries) < 20:
                        summaries.append(detail)
            if repair:
                self._flush()
            result = dict(stripes=checked, files_checked=files_checked, damaged_shards=damaged,
                          repaired_shards=repaired, damaged_file_count=damaged_files,
                          damaged_files=summaries, metadata_issues=len(self._metadata_bad | self._header_bad),
                          report_path=str(report_path), checked_at=time.time())
            record(dict(type="summary", **result))
            report.flush()
        self._last_verification = result
        self._integrity_failed = damaged_files > 0
        if damaged_files:
            self._issue(f"{damaged_files} file(s) contain unrecoverable or inconsistent data. See the integrity report.")
        return result

    def scrub(self, progress=None, cancel=None, repair=False):
        if repair:
            return self.repair(progress, cancel)
        if not self._operation.acquire(blocking=False):
            raise RaidError("Another storage operation is in progress.")
        self._busy = True
        try:
            self._probe_members()
            self._load_catalog(progress)
            return self._scrub(progress, cancel)
        finally:
            self._busy = False
            self._operation.release()

    def repair(self, progress=None, cancel=None):
        if self._read_only:
            raise RaidError("Reopen with write access to repair member files.")
        if not self._operation.acquire(blocking=False):
            raise RaidError("Another storage operation is in progress.")
        self._busy = True
        verified_metadata = False
        metadata_pending = True
        completed_verification = False
        try:
            self._load_catalog(progress)
            verified_metadata = True
            metadata_pending = self._needs_recovery
            result = self._scrub(progress, cancel, repair=True)
            completed_verification = True
            if result["damaged_file_count"]:
                raise UnrecoverableError(f"Repair could not recover {result['damaged_file_count']} file(s). Unrecoverable content was not guessed. Report: {result['report_path']}")
            _cancel(cancel)
            self._checkpoint()
            # Restore both copies, including an invalid slot which was not the
            # next alternating checkpoint slot.
            for index in self._files:
                header = (self._member_header(index) if hasattr(self, "_member_header")
                          else dict(self.h, index=index))
                self._write_member(index, 0, _header_bytes(header) * 2)
            self._flush()
            self._trim_unused_tail()
            self._metadata_bad.clear()
            self._header_bad.clear()
            self._bad.clear()
            self._needs_recovery = False
            self._integrity_failed = False
            self._issues.clear()
            return result
        except UnrecoverableError:
            # If the catalog and completed scrub are trustworthy, a damaged
            # file does not prevent checksum-verified exports of other files.
            self._needs_recovery = metadata_pending if verified_metadata and completed_verification else True
            raise
        except BaseException:
            self._needs_recovery = True
            raise
        finally:
            self._busy = False
            self._operation.release()

    def rebuild(self, replacements, progress=None, cancel=None):
        if self._read_only:
            raise RaidError("Reopen with write access to rebuild members.")
        if not replacements or not set(replacements) <= set(range(self.n)):
            raise RaidError("Choose replacement files for valid member positions.")
        if not self._operation.acquire(blocking=False):
            raise RaidError("Another storage operation is in progress.")
        self._busy = True
        targets, target_locks, paths, checkpoints = {}, {}, {}, {}
        attached = False
        try:
            self._load_catalog(progress)
            self._upgrade_membership_format()
            roster = list(self.h["member_roster"])
            for index in replacements:
                roster[index] = uuid.uuid4().hex
            planned = dict(self.h, membership_epoch=self.h["membership_epoch"] + 1,
                           member_roster=roster)
            replacement_size = self._live_end() if self.dynamic else self.h["member_size"]
            required = 0
            pending_headers = []
            for index, name in replacements.items():
                path = Path(name).absolute()
                if path.exists():
                    with open(path, "rb", buffering=0) as file:
                        h = max(_read_headers(file), key=_header_order)
                        if _geometry(h) != _geometry(self.h) or h["index"] != index:
                            raise RaidError("An existing replacement must belong to this array and member position.")
                        if h.get("state", "ready") == "ready":
                            self._validate_ready_replacement(file, h, index, set(replacements))
                        elif h.get("state") != "rebuilding":
                            raise RaidError("An existing replacement must be an incomplete rebuild for this array and member.")
                    pending_headers.append(h)
                    required = max(required, replacement_size - path.stat().st_size)
                else:
                    required = replacement_size
            # A resumed reconstruction keeps its incarnation IDs. If an older
            # plan no longer matches the survivors, retain verified data progress
            # but assign a new roster before any replacement becomes ready.
            if pending_headers:
                candidate = pending_headers[0]
                generation, candidate_roster = _membership_identity(candidate)
                if (candidate["version"] >= 2 and
                        generation in (self.h["membership_epoch"], self.h["membership_epoch"] + 1) and
                        (generation != self.h["membership_epoch"] or
                         candidate_roster == tuple(self.h["member_roster"])) and
                        all(_membership_identity(h) == (generation, candidate_roster) for h in pending_headers) and
                        all(candidate_roster[i] == self.h["member_roster"][i]
                            for i in set(range(self.n)) - replacements.keys())):
                    planned.update(membership_epoch=generation, member_roster=list(candidate_roster))
            report = capacity_report(list(replacements.values()))
            if required > report["recommended_size"]:
                raise RaidError("Not enough safe space for the replacement members.")
            for index, name in replacements.items():
                _cancel(cancel)
                path = Path(name).absolute()
                existed = path.exists()
                file = open(path, "r+b" if existed else "x+b", buffering=0)
                targets[index], paths[index] = file, path
                lock = MemberLock(file, writable=True)
                lock.acquire()
                target_locks[index] = lock
                checkpoint = 0
                if existed:
                    old = max(_read_headers(file), key=_header_order)
                    if old.get("state", "ready") == "ready":
                        # Revalidate under the exclusive target lock. A ready
                        # target may have changed since the initial inspection.
                        self._validate_ready_replacement(file, old, index, set(replacements))
                        checkpoint = self.h["stripes"]
                    elif old.get("rebuild_source_hash") == self._hash.hex() and old.get("rebuild_source_epoch") == self.h["epoch"]:
                        checkpoint = old["rebuild_next"]
                checkpoints[index] = checkpoint
                stage = self._member_header(index, planned, state="rebuilding", rebuild_next=checkpoint,
                             rebuild_source_hash=self._hash.hex(), rebuild_source_epoch=self.h["epoch"])
                for offset in (0, HEADER_SIZE):
                    file.seek(offset)
                    _write_all(file, _header_bytes(stage))
                    durable_flush(file)
                sync_directory(path.parent)
                def update(done, total=None, *unused, idx=index):
                    _cancel(cancel)
                    _progress(progress, done, replacement_size, f"Preparing replacement member {idx + 1}")
                allocation_size = (max(os.fstat(file.fileno()).st_size, self._stripe_offset(0))
                                   if self.dynamic else self.h["member_size"])
                allocate_file(file, allocation_size, progress=update)
            with self._db_lock:
                total = self._db.execute("SELECT COALESCE(SUM(count),0) FROM extents").fetchone()[0]
            completed = 0
            since_checkpoint = 0
            next_stripe = 0
            def save_progress(next_stripe):
                # Durable data must precede its progress marker. Writing both
                # copies separately retains an older valid marker on a torn write.
                for index, target in targets.items():
                    durable_flush(target)
                    stage = self._member_header(index, planned, state="rebuilding", rebuild_next=next_stripe,
                                 rebuild_source_hash=self._hash.hex(), rebuild_source_epoch=self.h["epoch"])
                    for offset in (0, HEADER_SIZE):
                        target.seek(offset)
                        _write_all(target, _header_bytes(stage))
                        durable_flush(target)
                self._boundary("rebuild_checkpoint_durable")
            self._begin_rebuild_reads()
            for stripe, token in self._all_stripes():
                _cancel(cancel)
                needed = []
                for index, target in targets.items():
                    role = (index - stripe) % self.n
                    if stripe < checkpoints[index]:
                        target.seek(self._stripe_offset(stripe))
                        raw = target.read(self.h["chunk_size"] + SHARD_HEADER)
                        if _valid_shard(raw, token, stripe, role, self.h["chunk_size"]):
                            continue
                    needed.append(index)
                if needed:
                    shards, bad = self._read_stripe(stripe, token, all_shards=True)
                    for index in needed:
                        role = (index - stripe) % self.n
                        if self.dynamic and os.fstat(targets[index].fileno()).st_size < self._stripe_offset(stripe + 1):
                            extend_file(targets[index], self._stripe_offset(stripe + 1))
                        targets[index].seek(self._stripe_offset(stripe))
                        _write_all(targets[index], self._shard_bytes(token, stripe, role, shards[role]))
                    if (bad & self._files.keys()) - replacements.keys():
                        raise RecoveryRequired("A surviving member has damaged shards. Repair or replace it before rebuilding.")
                completed += 1
                since_checkpoint += 1
                next_stripe = stripe + 1
                _progress(progress, completed, total, "Rebuilding replacement members")
                self._boundary("rebuild_stripe_written")
                if since_checkpoint * self.h["chunk_size"] >= 16 * 1024**2:
                    save_progress(next_stripe)
                    since_checkpoint = 0
            save_progress(next_stripe)
            self._boundary("rebuild_data_durable")
            verification = self._scrub(progress, cancel, repair=False)
            if verification.get("damaged_file_count", 0):
                raise UnrecoverableError("Rebuild cannot safely proceed: some files could not be verified. Review the damage report.")
            self._end_rebuild_reads()
            self._needs_recovery = True
            # Copy the current canonical metadata before publishing member headers.
            for index, file in targets.items():
                if self.dynamic and os.fstat(file.fileno()).st_size > replacement_size:
                    file.truncate(replacement_size)
                    durable_flush(file)
                self._canonical.seek(0)
                file.seek(8192 + self.h["bank"] * self.h["bank_size"])
                while chunk := self._canonical.read(1024**2):
                    _write_all(file, chunk)
                _write_all(file, bytes(LOG_STRUCT.size))
                durable_flush(file)
                header = _header_bytes(self._member_header(index, planned, state="ready",
                    rebuild_source_hash=self._hash.hex(), rebuild_source_epoch=self.h["epoch"],
                    rebuild_next=self.h["stripes"]))
                for offset in (0, HEADER_SIZE):
                    file.seek(offset)
                    _write_all(file, header)
                    durable_flush(file)
                sync_directory(paths[index].parent)
                self._boundary("rebuild_header_durable")
                self._boundary("replacement_ready_durable")
            # Targets are complete before retirement is published. Survivors
            # record the same roster; old originals are never rewritten.
            for index in self._files.keys() - targets.keys():
                h = self._member_header(index, planned, state="ready")
                for offset in (0, HEADER_SIZE):
                    self._write_member(index, offset, _header_bytes(h))
                    self._flush([index])
                self._headers[index] = h
                self._boundary("membership_header_durable")
            # Retain source handles throughout reconstruction, then release only
            # those explicitly replaced. Original member files are preserved.
            for index in targets.keys() & self._files.keys():
                self._locks[index].release()
                self._files[index].close()
            self._files.update(targets)
            self._locks.update(target_locks)
            self._paths.update(paths)
            self._expected_sizes.update({i: os.fstat(file.fileno()).st_size for i, file in targets.items()})
            self.h = planned
            self._headers.update({i: self._member_header(i, planned) for i in targets})
            attached = True
            self._load_catalog(progress)
            if self._needs_recovery:
                self._checkpoint()
                self._trim_unused_tail()
                self._metadata_bad.clear()
                self._needs_recovery = False
            self._bad.difference_update(targets)
            self._header_bad.difference_update(targets)
            if not self._bad:
                self._issues.clear()
        finally:
            self._end_rebuild_reads()
            if not attached:
                for lock in target_locks.values():
                    with contextlib.suppress(Exception):
                        lock.release()
                for file in targets.values():
                    file.close()
                # Incomplete replacements have self-identifying headers and are
                # retained for resumable rebuilding, including after cancellation.
            self._busy = False
            self._operation.release()

    def _validate_ready_replacement(self, file, header, index, replacing):
        """Recognize a finished target whose roster publication was interrupted.

        Never overwrite a ready member containing a newer/divergent catalog. A
        matching source hash is verified from the actual complete log, including
        records after its checkpoint, rather than trusting an old progress tag.
        """
        generation, roster = _membership_identity(header)
        current, current_roster = _membership_identity(self.h)
        if (_geometry(header) != _geometry(self.h) or header["index"] != index or
                header["version"] < 2 or generation < 1 or generation not in (current, current + 1) or
                (generation == current and (index in self._files or roster != current_roster)) or
                (generation == current + 1 and roster[index] == current_roster[index]) or
                any(roster[i] != current_roster[i] for i in set(range(self.n)) - replacing) or
                _checkpoint_identity(header) != _checkpoint_identity(self.h)):
            raise RaidError("This ready member is not a matching interrupted replacement. Preserve it and open the matching member set.")
        if (header.get("rebuild_source_hash", self._hash.hex()) != self._hash.hex() or
                header.get("rebuild_source_epoch", self.h["epoch"]) != self.h["epoch"]):
            raise RaidError("The ready replacement was built from another catalog; preserve it.")
        base = 8192 + header["bank"] * header["bank_size"]
        position, seq, previous = 0, 1, ZERO_HASH
        while True:
            record = _read_record(file, base + position, base + header["bank_size"], seq, previous)
            if record is None:
                break
            raw, previous, _event = record
            position += len(raw)
            seq += 1
        if position != self._tail or seq - 1 != self._seq or previous != self._hash:
            raise RaidError("The ready replacement contains a different transaction history; preserve it and assemble its matching members.")

    def close(self):
        if self._closed:
            return
        if not self._operation.acquire(blocking=False):
            raise RaidError("Wait for the active storage operation to stop before closing the array.")
        self._closed = True
        for lock in self._locks.values():
            with contextlib.suppress(Exception):
                lock.release()
        for file in self._files.values():
            with contextlib.suppress(Exception):
                file.close()
        if self._db:
            self._db.close()
        if self._canonical:
            self._canonical.close()
        try:
            self._scratch.cleanup()
        finally:
            self._operation.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _write_all(file, data):
    view = memoryview(data)
    while view:
        written = file.write(view)
        if written is None or written <= 0:
            raise OSError("Short write; no progress")
        view = view[written:]


def _read_exact(file, size):
    result = bytearray()
    while len(result) < size:
        block = file.read(size - len(result))
        if not block:
            break
        result.extend(block)
    return bytes(result)


def _valid_name(name):
    if not isinstance(name, str) or not name or "\0" in name or len(name.encode("utf-8")) > 4096:
        raise RaidError("A name must contain 1–4096 UTF-8 bytes and cannot contain a NUL character.")


def _valid_shard(raw, token, stripe, role, chunk_size):
    if len(raw) != chunk_size + SHARD_HEADER:
        return False
    magic, actual_token, actual_stripe, actual_role, digest = SHARD_STRUCT.unpack(raw[:SHARD_STRUCT.size])
    prefix = struct.pack("<8s16sQI", magic, actual_token, actual_stripe, actual_role)
    return ((magic, actual_token, actual_stripe, actual_role) == (SHARD_MAGIC, token, stripe, role)
            and hashlib.sha256(prefix + raw[SHARD_HEADER:]).digest() == digest)


def _is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _geometry(h):
    return tuple(h[k] for k in ("codec", "uuid", "n", "parity", "member_size", "chunk_size", "bank_size", "stripes")) + (h.get("storage", "fixed"),)


def _valid_member_size(header, size):
    if header.get("storage") == "dynamic":
        return 8192 + 2 * header["bank_size"] <= size <= header["member_size"]
    return size == header["member_size"]


def _membership_identity(h):
    roster = h.get("member_roster")
    if roster is None:
        namespace = uuid.UUID(h["uuid"])
        roster = [uuid.uuid5(namespace, f"legacy-member-{i}").hex for i in range(h["n"])]
    return h.get("membership_epoch", 0), tuple(roster)


def _member_id(h):
    return h.get("member_id", _membership_identity(h)[1][h["index"]])


def _checkpoint_identity(h):
    return tuple(h[k] for k in ("epoch", "bank", "checkpoint_bytes", "checkpoint_seq", "checkpoint_hash"))
