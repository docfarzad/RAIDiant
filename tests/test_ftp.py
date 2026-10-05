"""Real loopback FTP tests for transactions, isolation and interrupted transfers."""
import errno
import ftplib
import io
import os
import socket
import struct
import threading
import time

import pytest

from raidiant import engine, ftp_service
from raidiant.engine import Array, RaidError
from raidiant.ftp_service import FTPService, decode_name, encode_name, generate_credentials
from raidiant.streams import open_upload


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIDIANT_STATE_DIR", str(tmp_path / "state"))
    paths = [tmp_path / f"member-{i}.r5m" for i in range(3)]
    array = Array.create(paths, 1, 32 * 1024**2, chunk_size=4096)
    service = FTPService(array, "0123", "4567", port=0)
    service.start()
    client = connect(service)
    try:
        yield array, service, client, paths
    finally:
        client.close()
        service.stop()
        array.close()


def connect(service):
    client = ftplib.FTP()
    client.connect("127.0.0.1", service.status["port"], timeout=5)
    client.login("0123", "4567")
    return client


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


def test_roundtrip_folders_rename_and_no_overwrite(running):
    array, service, client, _ = running
    payload = os.urandom(71011)
    client.mkd("folder")
    client.cwd("folder")
    client.storbinary("STOR test.bin", io.BytesIO(payload))
    assert client.size("test.bin") == len(payload)
    assert client.nlst() == ["test.bin"]
    assert dict(client.mlsd())["test.bin"]["size"] == str(len(payload))
    with pytest.raises(ftplib.error_perm):
        client.storbinary("STOR test.bin", io.BytesIO(b"overwrite"))
    received = bytearray()
    client.retrbinary("RETR test.bin", received.extend)
    assert received == payload
    client.rename("test.bin", "renamed.bin")
    client.delete("renamed.bin")
    client.cwd("/")
    client.rmd("folder")
    assert array.list_dir() == []


def test_active_upload_blocks_conflicting_operations_and_stop_aborts(running):
    array, service, client, paths = running
    before = [p.stat().st_size for p in paths]
    client.voidcmd("TYPE I")
    data = client.transfercmd("STOR unfinished.bin")
    try:
        # Force storage writes rather than testing an empty in-memory stream.
        data.sendall(b"x" * (17 * 1024**2))
        wait_for(lambda: paths[0].stat().st_size > before[0])
        assert service.status["transfers"] == 1
        for operation in (array.scrub, array.repair, lambda: array.mkdir("blocked"),
                          lambda: array.rebuild({0: paths[0].parent / "replacement.r5m"}), array.close):
            with pytest.raises(RaidError, match="operation"):
                operation()
        assert array.list_dir() == []
        service.stop()
    finally:
        data.close()
    assert service.status["transfers"] == 0
    assert array.list_dir() == []
    assert [p.stat().st_size for p in paths] == before
    assert array.status["writable"]


def test_abor_and_socket_reset_never_publish(running):
    array, service, client, _ = running
    client.voidcmd("TYPE I")
    data = client.transfercmd("STOR cancelled.bin")
    data.sendall(b"partial")
    # Normal ABOR uses the control socket; don't finish the data stream first.
    client.putcmd("ABOR")
    assert client.getline().startswith("426")
    assert client.getline().startswith("226")
    data.close()
    wait_for(lambda: not service.status["transfers"])
    assert array.list_dir() == []
    data = client.transfercmd("STOR reset.bin")
    data.sendall(b"partial")
    linger = struct.pack("hh" if os.name == "nt" else "ii", 1, 0)
    data.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    data.close()
    with pytest.raises(ftplib.error_temp, match="426"):
        client.voidresp()
    wait_for(lambda: not service.status["transfers"])
    assert array.list_dir() == []
    array.mkdir("lease released")


def test_storage_failure_returns_failure_not_success(running, monkeypatch):
    array, service, client, paths = running
    before = [p.stat().st_size for p in paths]
    original = engine.extend_file
    def fail(file, size):
        if file is array._files[1]:
            raise OSError(errno.ENOSPC, "No physical space")
        return original(file, size)
    monkeypatch.setattr(engine, "extend_file", fail)
    with pytest.raises(ftplib.error_temp, match="426"):
        client.storbinary("STOR no-space.bin", io.BytesIO(b"x" * 100000))
    assert array.list_dir() == []
    assert not array.status["writable"]
    assert [p.stat().st_size for p in paths] == before
    monkeypatch.setattr(engine, "extend_file", original)
    array.repair()
    assert array.status["writable"]


def test_degraded_download_only_and_recovery_blocking(tmp_path):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(3)]
    with Array.create(paths, 1, 2 * 1024**2, chunk_size=4096) as array:
        with open_upload(array, "file.bin") as target:
            target.write(b"survives a missing member")
            target.finish()
    with Array.open(paths[:2]) as array:
        service = FTPService(array, "0123", "4567", port=0)
        service.start()
        try:
            with connect(service) as client:
                result = bytearray()
                client.retrbinary("RETR file.bin", result.extend)
                assert result == b"survives a missing member"
                for command in ("STOR forbidden", "DELE file.bin", "MKD blocked"):
                    with pytest.raises(ftplib.error_perm):
                        client.sendcmd(command)
                array._needs_recovery = True
                with pytest.raises(ftplib.error_perm):
                    client.retrbinary("RETR file.bin", lambda _: None)
        finally:
            service.stop()


def test_exclusive_operation_blocks_new_ftp_transfers(running):
    array, _service, client, _ = running
    with array._mutating():
        with pytest.raises(ftplib.error_perm):
            client.storbinary("STOR blocked", io.BytesIO(b"bytes"))
    client.storbinary("STOR allowed", io.BytesIO(b"bytes"))


def test_virtual_paths_and_unusual_names(running, tmp_path):
    array, _service, client, _ = running
    unusual = '../x\\y%\r\nname'
    encoded = encode_name(unusual)
    assert decode_name(encoded) == unusual
    client.storbinary("STOR " + encoded, io.BytesIO(b"virtual only"))
    assert array.list_dir()[0]["name"] == unusual
    assert encoded in client.nlst()
    with pytest.raises(ftplib.error_perm):
        client.retrbinary("RETR /../../etc/passwd", lambda _: None)
    for command in ("APPE whatever", "REST 1", "STOU", "SITE CHMOD 777 anything"):
        with pytest.raises(ftplib.error_perm):
            client.sendcmd(command)


def test_authentication_and_four_digit_credentials(running):
    _, service, _, _ = running
    user, password = generate_credentials()
    assert len(user) == len(password) == 4 and user.isascii() and user.isdigit() and password.isdigit()
    # Exercise bounded global/IP authentication accounting without timing delays.
    authorizer = ftp_service._Authorizer(service)
    handler = type("Handler", (), {"remote_ip": "127.0.0.1"})()
    for _ in range(5):
        with pytest.raises(ftp_service.AuthenticationFailed):
            authorizer.validate_authentication("wrong", "bad", handler)
    with pytest.raises(ftp_service.AuthenticationFailed, match="Too many"):
        authorizer.validate_authentication("0123", "4567", handler)


def test_lan_addresses_are_local_candidates(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [(2, 1, 6, "", (a, 0)) for a in
                         ("127.0.0.1", "192.168.1.4", "10.0.0.2", "192.168.1.4")])
    assert ftp_service.lan_addresses() == ["10.0.0.2", "192.168.1.4"]


def test_ftp_download_during_rebuild_drains_before_publication(tmp_path, monkeypatch):
    paths = [tmp_path / f"member-{i}.r5m" for i in range(3)]
    payload = os.urandom(151113)
    with Array.create(paths, 1, 4 * 1024**2, chunk_size=4096) as array:
        with open_upload(array, "shared.bin") as stream:
            stream.write(payload)
            stream.finish()
    rebuilding, allow_rebuild = threading.Event(), threading.Event()
    reading, allow_read = threading.Event(), threading.Event()
    errors, received = [], bytearray()
    original = ftp_service.open_download
    class SlowReader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size):
            reading.set()
            assert allow_read.wait(10)
            return self.stream.read(size)
    monkeypatch.setattr(ftp_service, "open_download", lambda *args: SlowReader(original(*args)))
    with Array.open(paths[1:]) as array:
        def boundary(name):
            if name == "rebuild_stripe_written" and not rebuilding.is_set():
                rebuilding.set()
                assert allow_rebuild.wait(10)
        array._fault_hook = boundary
        def rebuild():
            try:
                array.rebuild({0: tmp_path / "replacement.r5m"})
            except BaseException as exc:
                errors.append(exc)
        service = FTPService(array, "0123", "4567", port=0)
        service.start()
        worker = threading.Thread(target=rebuild)
        download = None
        try:
            worker.start()
            assert rebuilding.wait(5)
            def retrieve():
                try:
                    with connect(service) as client:
                        client.retrbinary("RETR shared.bin", received.extend)
                except BaseException as exc:
                    errors.append(exc)
            download = threading.Thread(target=retrieve)
            download.start()
            assert reading.wait(5)
            assert array._readers == 1
            allow_rebuild.set()
            wait_for(lambda: array._exclusive_pending)
            assert worker.is_alive(), "Publication must wait for the FTP read lease"
            allow_read.set()
            download.join(5)
            worker.join(5)
            assert not errors
            assert not download.is_alive() and not worker.is_alive()
            assert received == payload
            assert array.status["writable"]
        finally:
            allow_read.set()
            allow_rebuild.set()
            if download:
                download.join(10)
            worker.join(10)
            service.stop()
