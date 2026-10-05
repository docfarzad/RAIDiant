"""Opt-in FTP access to the virtual array, never to the host filesystem.

FTP is plaintext. The desktop defaults to loopback and explicitly offers LAN
access. Uploads are published only after the storage stream commits, before the
226 reply. Disconnect, ABOR, timeout and server shutdown close/abandon streams.
"""
from __future__ import annotations

from collections import deque
import contextlib
import errno
import logging
import os
import posixpath
import secrets
import socket
import stat as statmod
import threading
import time
from urllib.parse import quote, unquote

from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
from pyftpdlib.filesystems import AbstractedFS, FilesystemError
from pyftpdlib.handlers import DTPHandler, FTPHandler
from pyftpdlib.handlers.ftp.producers import BufferedIteratorProducer
from pyftpdlib.ioloop import IOLoop
from pyftpdlib.servers import FTPServer

from .engine import RaidError, RecoveryRequired, UnrecoverableError
from .streams import open_download, open_upload


def generate_credentials():
    """Return independent four-digit strings, retaining leading zeroes."""
    return f"{secrets.randbelow(10000):04d}", f"{secrets.randbelow(10000):04d}"


def lan_addresses():
    """Best-effort local IPv4 addresses, without external network probes."""
    addresses = set()
    with contextlib.suppress(OSError):
        addresses.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    return sorted(address for address in addresses if not address.startswith("127.") and address != "0.0.0.0")


def encode_name(name):
    """Reversible FTP component; protocol/path delimiters are percent escaped."""
    if name in (".", ".."):
        return "%2E" * len(name)
    result = []
    for index, char in enumerate(name):
        if (char in "%/\\" or ord(char) < 32 or ord(char) == 127 or
                (char == " " and index in (0, len(name) - 1)) or
                (char == "-" and index == 0)):
            result.append("%2D" if char == "-" else quote(char, safe=""))
        else:
            result.append(char)
    return "".join(result)


def decode_name(component):
    try:
        result = unquote(component, encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise FilesystemError("Invalid encoded filename") from exc
    if not result or "\0" in result or encode_name(result) != component:
        raise FilesystemError("Use the filename exactly as shown in the FTP listing")
    return result


class _Authorizer(DummyAuthorizer):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.failures = deque(maxlen=100)
        # Deliberately avoid add_user(), which requires a real host directory.
        self.user_table[service.username] = dict(pwd=service.password, home="/", perm="elrdfmw",
            operms={}, msg_login="RAIDiant array connected.", msg_quit="Goodbye.")

    def validate_authentication(self, username, password, handler):
        now = time.monotonic()
        while self.failures and self.failures[0][0] <= now - 60:
            self.failures.popleft()
        ip = handler.remote_ip
        if len(self.failures) >= 20 or sum(item[1] == ip for item in self.failures) >= 5:
            raise AuthenticationFailed("Too many login attempts; wait one minute")
        valid_user = secrets.compare_digest(username.encode("utf-8"), self.service.username.encode("ascii"))
        valid_pass = secrets.compare_digest(password.encode("utf-8"), self.service.password.encode("ascii"))
        if not (valid_user and valid_pass):
            self.failures.append((now, ip))
            raise AuthenticationFailed("Authentication failed")

    def has_perm(self, username, perm, path=None):
        return username == self.service.username and perm in "elrdfmw"


class _Stream:
    """Adapt engine errors and accounting to pyftpdlib's file-like protocol."""
    def __init__(self, stream, name, service, uploading):
        self.stream, self.name, self.service = stream, name, service
        self.uploading, self.closed, self.finished = uploading, False, False
        service._transfer(1)

    def _call(self, method, *args):
        try:
            return getattr(self.stream, method)(*args)
        except (RaidError, OSError) as exc:
            self.service._storage_error(exc)
            raise OSError(errno.EIO, str(exc)) from exc

    def read(self, size):
        return self._call("read", size)

    def write(self, data):
        return self._call("write", data)

    def tell(self):
        return self.stream.tell()

    def finish(self):
        result = self._call("finish")
        self.finished = True
        self.service._emit("changed", "FTP upload completed")
        return result

    def close(self):
        if not self.closed:
            try:
                self._call("close")
            finally:
                self.closed = True
                self.service._transfer(-1)


class ArrayFilesystem(AbstractedFS):
    """All paths are virtual POSIX paths; every host filesystem hook is replaced."""
    def __init__(self, root, cmd_channel):
        super().__init__("/", cmd_channel)
        self.service = cmd_channel.service
        self.array = self.service.array

    def ftpnorm(self, path):
        # Never reinterpret a backslash or drive prefix as a host path.
        path = path if path.startswith("/") else posixpath.join(self.cwd, path)
        return "/" + posixpath.normpath("/" + path.lstrip("/")).lstrip("/")

    ftp2fs = ftpnorm
    fs2ftp = ftpnorm
    realpath = ftpnorm

    def validpath(self, path):
        return path.startswith("/") and "\0" not in path and "\\" not in path

    def _resolve_unlocked(self, path):
        entry = self.array.get_entry("root")
        for component in self.ftpnorm(path).split("/")[1:]:
            if not component:
                continue
            name = decode_name(component)
            if not entry["is_dir"]:
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory")
            with self.array._db_lock:
                row = self.array._db.execute("SELECT * FROM entries WHERE parent_id=? AND name=? AND complete=1",
                                              (entry["id"], name)).fetchone()
            if row is None:
                raise FileNotFoundError(errno.ENOENT, "No such array entry")
            entry = dict(row)
        return entry

    def resolve(self, path):
        with self.array._export_access():
            return self._resolve_unlocked(path)

    def _parent_unlocked(self, path):
        parent, component = posixpath.split(self.ftpnorm(path))
        if not component:
            raise FilesystemError("The array root cannot be changed")
        entry = self._resolve_unlocked(parent)
        if not entry["is_dir"]:
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory")
        return entry, decode_name(component)

    def open(self, path, mode):
        if mode == "rb":
            entry = self.resolve(path)
            if entry["is_dir"]:
                raise IsADirectoryError(errno.EISDIR, "Choose a file")
            stream = open_download(self.array, entry["id"])
        elif mode == "wb":
            with self.array._export_access():
                parent, name = self._parent_unlocked(path)
            stream = open_upload(self.array, name, parent["id"])
        else:
            raise FilesystemError("Append and resumed uploads are unavailable")
        return _Stream(stream, path, self.service, mode == "wb")

    def chdir(self, path):
        if not self.resolve(path)["is_dir"]:
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory")
        self.cwd = self.ftpnorm(path)

    def listdir(self, path):
        parent = self.resolve(path)
        if not parent["is_dir"]:
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory")
        def entries():
            offset = 0
            while True:
                with self.array._export_access():
                    page = self.array.list_dir(parent["id"], offset, 200)
                for child in page:
                    yield encode_name(child["name"])
                if len(page) < 200:
                    return
                offset += len(page)
        return entries()

    listdirinfo = listdir

    def mkdir(self, path):
        with self.array._mutating():
            parent, name = self._parent_unlocked(path)
            with self.array._db_lock:
                if self.array._db.execute("SELECT 1 FROM entries WHERE parent_id=? AND name=?", (parent["id"], name)).fetchone():
                    raise FileExistsError(errno.EEXIST, "An entry already exists")
            self.array._mkdir(name, parent["id"])
        self.service._emit("changed", "FTP folder created")

    def rmdir(self, path):
        with self.array._mutating():
            entry = self._resolve_unlocked(path)
            if entry["id"] == "root":
                raise FilesystemError("The array root cannot be removed")
            if not entry["is_dir"]:
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory")
            if self.array.list_dir(entry["id"], limit=1):
                raise OSError(errno.ENOTEMPTY, "Directory is not empty")
            self.array._append({"op": "delete", "id": entry["id"]})
        self.service._emit("changed", "FTP folder deleted")

    def remove(self, path):
        entry = self.resolve(path)
        if entry["is_dir"]:
            raise IsADirectoryError(errno.EISDIR, "Use RMD for an empty directory")
        self.array.delete(entry["id"])
        self.service._emit("changed", "FTP file deleted")

    def rename(self, src, dst):
        with self.array._export_access():
            entry = self._resolve_unlocked(src)
            parent, name = self._parent_unlocked(dst)
        if entry["parent_id"] != parent["id"]:
            raise FilesystemError("Rename within the same folder; moving between folders is unavailable")
        self.array.rename(entry["id"], name)
        self.service._emit("changed", "FTP entry renamed")

    def stat(self, path):
        entry = self.resolve(path)
        mode = (statmod.S_IFDIR | 0o755) if entry["is_dir"] else (statmod.S_IFREG | 0o644)
        modified = entry["modified"]
        return os.stat_result((mode, 0, 0, 1, 0, 0, entry["size"], modified, modified, modified))

    lstat = stat

    def isdir(self, path):
        try:
            return bool(self.resolve(path)["is_dir"])
        except (FileNotFoundError, NotADirectoryError):
            return False

    def isfile(self, path):
        try:
            return not self.resolve(path)["is_dir"]
        except (FileNotFoundError, NotADirectoryError):
            return False

    def islink(self, path):
        return False

    def lexists(self, path):
        try:
            self.resolve(path)
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False

    def getsize(self, path):
        return self.resolve(path)["size"]

    def getmtime(self, path):
        return self.resolve(path)["modified"]

    def _unsupported(self, *args, **kwargs):
        raise FilesystemError("This operation is unavailable on the virtual array")

    mkstemp = chmod = utime = readlink = _unsupported

    def format_list(self, basedir, listing, ignore_err=True):
        for name in listing:
            entry = self.resolve(posixpath.join(basedir, name))
            kind = "drwxr-xr-x" if entry["is_dir"] else "-rw-r--r--"
            date = time.strftime("%b %d %H:%M", time.gmtime(entry["modified"]))
            yield f"{kind} 1 array array {entry['size']} {date} {name}\r\n".encode("utf-8")

    def format_mlsx(self, basedir, listing, perms, facts, ignore_err=True):
        for name in listing:
            entry = self.resolve(posixpath.join(basedir, name))
            values = dict(type="dir" if entry["is_dir"] else "file", size=str(entry["size"]),
                modify=time.strftime("%Y%m%d%H%M%S", time.gmtime(entry["modified"])), unique=entry["id"])
            values["perm"] = ("elcmp" if entry["is_dir"] else "rdfw") if self.array.status["writable"] else ("el" if entry["is_dir"] else "r")
            details = "".join(f"{key}={value};" for key, value in values.items() if key in facts)
            yield f"{details} {name}\r\n".encode("utf-8")


class _ArrayDTP(DTPHandler):
    timeout = 60

    def handle_read(self):
        # pyftpdlib's default recv treats a TCP reset as a normal upload EOF.
        # Only an orderly EOF commits; socket errors always abandon.
        try:
            chunk = self.socket.recv(self.ac_in_buffer_size)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._resp = ("426 Data connection failed; upload abandoned.", logging.info)
            self.close()
            return
        if not chunk:
            self._upload_eof = True
            self.handle_close()
            return
        self.tot_bytes_received += len(chunk)
        if self._data_wrapper is not None:
            chunk = self._data_wrapper(chunk)
        try:
            self.file_obj.write(chunk)
        except (OSError, RaidError) as exc:
            self._resp = ("426 " + _reply_text(exc) + "; upload abandoned.", logging.warning)
            self.close()

    handle_read_event = handle_read

    def handle_close(self):
        if not self._closed and self.receive:
            if not getattr(self, "_upload_eof", False):
                self._resp = ("426 Transfer interrupted; upload abandoned.", logging.info)
                self.close()
                return
            try:
                if self.cmd_channel.service._stop.is_set() or not self.cmd_channel.connected:
                    raise OSError("FTP connection stopped")
                self.file_obj.finish()
            except (OSError, RaidError) as exc:
                self._resp = ("426 " + _reply_text(exc) + "; upload not completed.", logging.warning)
                self.close()
                return
        super().handle_close()

    def close(self):
        if not self._closed and self.file_obj is not None and not self.file_obj.closed:
            try:
                self.file_obj.close()
            except Exception as exc:
                self.transfer_finished = False
                self._resp = ("426 " + _reply_text(exc) + "; recovery may be required.", logging.warning)
        super().close()


def _reply_text(error):
    return str(error).replace("\r", " ").replace("\n", " ")[:350]


class _ArrayHandler(FTPHandler):
    abstracted_fs = ArrayFilesystem
    dtp_handler = _ArrayDTP
    use_sendfile = False
    auth_failed_timeout = 2
    max_login_attempts = 3
    timeout = 120
    banner = "RAIDiant FTP. Trusted networks only; this connection is not encrypted."
    # Unsupported commands must not advertise capabilities or reach host hooks.
    proto_cmds = {key: value for key, value in FTPHandler.proto_cmds.items()
                  if key not in {"APPE", "REST", "STOU", "SITE CHMOD", "MFMT"}}

    def pre_process_command(self, line, cmd, arg):
        try:
            return super().pre_process_command(line, cmd, arg)
        except (RaidError, OSError, FilesystemError) as exc:
            self.service._storage_error(exc)
            self.respond("550 " + _reply_text(exc))

    def on_connect(self):
        self.service._connection(1)

    def on_disconnect(self):
        self.service._connection(-1)

    def ftp_CWD(self, path):
        # Do not use FTPHandler's process-wide os.chdir compatibility behavior.
        self.fs.chdir(path)
        quoted = self.fs.cwd.replace('"', '""')
        self.respond(f'250 "{quoted}" is the current directory.')
        return path

    def _listing(self, path, command):
        if self.fs.isdir(path):
            basedir, listing = path, self.fs.listdir(path)
        else:
            self.fs.stat(path)
            basedir, name = posixpath.split(path)
            listing = [name]
        if command == "NLST":
            iterator = ((name + "\r\n").encode("utf-8") for name in listing)
        elif command == "MLSD":
            iterator = self.fs.format_mlsx(basedir, listing, self.authorizer.get_perms(self.username), self._current_facts)
        else:
            iterator = self.fs.format_list(basedir, listing)
        self.push_dtp_data(BufferedIteratorProducer(iterator), isproducer=True, cmd=command)
        return path

    def ftp_LIST(self, path):
        return self._listing(path, "LIST")

    def ftp_NLST(self, path):
        return self._listing(path, "NLST")

    def ftp_MLSD(self, path):
        if not self.fs.isdir(path):
            self.respond("501 No such directory.")
            return
        return self._listing(path, "MLSD")

    def ftp_ABOR(self, line):
        # Also close streams queued by STOR before the data socket connected.
        queued = self._in_dtp_queue
        self._in_dtp_queue = None
        if queued is not None:
            queued[0].close()
        return super().ftp_ABOR(line)


class FTPService:
    """One explicitly started server. Stop it before closing the array session."""
    def __init__(self, array, username, password, host="127.0.0.1", port=2121, on_event=None):
        for credential in (username, password):
            if not isinstance(credential, str) or len(credential) != 4 or not credential.isascii() or not credential.isdigit():
                raise ValueError("FTP username and password must each contain four digits")
        if host not in ("127.0.0.1", "0.0.0.0"):
            raise ValueError("Choose local-only (127.0.0.1) or LAN (0.0.0.0) access")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("FTP port must be from 1 to 65535")
        self.array, self.username, self.password = array, username, password
        self.host, self.port, self.on_event = host, port, on_event
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread = self._server = None
        self._connections = self._transfers = 0
        self._last_error = None
        self._local_addresses = ["127.0.0.1"] if host == "127.0.0.1" else []

    @property
    def address(self):
        return (self.host, self.port)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def status(self):
        with self._lock:
            return dict(running=self.running, connections=self._connections, transfers=self._transfers,
                        last_error=self._last_error, host=self.host, port=self.port,
                        local_addresses=list(self._local_addresses))

    def _emit(self, kind, message):
        if self.on_event:
            with contextlib.suppress(Exception):
                self.on_event(dict(type=kind, message=message))

    def _storage_error(self, error):
        if isinstance(error, (RecoveryRequired, UnrecoverableError)):
            with self._lock:
                self._last_error = str(error)
            self._emit("error", str(error))

    def _connection(self, delta):
        with self._lock:
            self._connections = max(0, self._connections + delta)

    def _transfer(self, delta):
        with self._lock:
            self._transfers = max(0, self._transfers + delta)
        self._emit("transfer", "FTP transfer started" if delta > 0 else "FTP transfer ended")

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            raise RaidError("FTP is already running")
        if self.array._closed:
            raise RaidError("Open an array before starting FTP")
        self._stop.clear()
        self._local_addresses = ["127.0.0.1"] if self.host == "127.0.0.1" else lan_addresses()
        service = self
        class Handler(_ArrayHandler):
            pass
        Handler.service, Handler.authorizer = service, _Authorizer(service)
        loop = IOLoop()
        try:
            server = FTPServer((self.host, self.port), Handler, ioloop=loop)
        except BaseException:
            loop.close()
            raise
        server.max_cons, server.max_cons_per_ip = 12, 6
        self.port = server.socket.getsockname()[1]
        self._server = server
        def run():
            try:
                while not self._stop.is_set():
                    loop.loop(timeout=0.1, blocking=False)
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                self._emit("error", "FTP server stopped: " + str(exc))
            finally:
                server.close_all()
                with self._lock:
                    self._connections = self._transfers = 0
                self._emit("transfer", "FTP stopped")
        self._thread = threading.Thread(target=run, name="RAIDiant FTP", daemon=True)
        self._thread.start()
        return self.address

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        self._server = None

