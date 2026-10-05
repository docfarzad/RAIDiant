"""Host boundary checks; no large files or storage hardware are required."""

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from raidiant import host


class AllocationTests(unittest.TestCase):
    def test_native_allocation_preserves_position_and_existing_bytes(self):
        with tempfile.TemporaryFile("w+b") as file:
            file.write(b"header")
            file.seek(2)
            host.allocate_file(file, 32 * 1024)
            self.assertEqual(file.tell(), 2)
            self.assertEqual(os.fstat(file.fileno()).st_size, 32 * 1024)
            file.seek(0)
            self.assertEqual(file.read(6), b"header")
            file.seek(32 * 1024 - 8)
            self.assertEqual(file.read(), bytes(8))

    def test_fallback_has_bounded_writes_and_preserves_prefix(self):
        calls = []
        with tempfile.TemporaryFile("w+b") as file:
            file.write(b"abc")
            with mock.patch.object(host, "_native_allocate", side_effect=OSError(errno.ENOTSUP, "unsupported")), mock.patch.object(host, "ALLOCATION_CHUNK", 13):
                host.allocate_file(file, 101, lambda done, total: calls.append((done, total)))
            self.assertEqual(file.tell(), 3)
            file.seek(0)
            self.assertEqual(file.read(), b"abc" + bytes(98))
        self.assertEqual(calls[0], (0, 101))
        self.assertEqual(calls[-1], (101, 101))
        self.assertTrue(all(b[0] - a[0] <= 13 for a, b in zip(calls, calls[1:])))

    def test_no_fallback_after_disk_full_or_io_failure(self):
        for failure in (errno.ENOSPC, errno.EDQUOT, errno.EIO):
            with self.subTest(failure=failure), tempfile.TemporaryFile("w+b") as file:
                with mock.patch.object(host, "_native_allocate", side_effect=OSError(failure, "failed")), mock.patch.object(host, "_zero_fill") as fallback:
                    with self.assertRaises(OSError) as raised:
                        host.allocate_file(file, 123)
                    self.assertEqual(raised.exception.errno, failure)
                    fallback.assert_not_called()

    def test_flush_failure_propagates(self):
        with tempfile.TemporaryFile("w+b") as file, mock.patch.object(host, "durable_flush", side_effect=OSError(errno.EIO, "flush failed")):
            with self.assertRaises(OSError):
                host.allocate_file(file, 4096)

    def test_invalid_sizes_do_not_truncate(self):
        with tempfile.TemporaryFile("w+b") as file:
            file.write(b"keep")
            for size in (-1, 1, True, 4.5):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    host.allocate_file(file, size)
            file.seek(0)
            self.assertEqual(file.read(), b"keep")


class LockTests(unittest.TestCase):
    def test_exclusive_writer_blocks_other_readers_and_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "member"
            path.write_bytes(b"original")
            with path.open("r+b") as one, path.open("r+b") as two:
                one.seek(3)
                lock = host.MemberLock(one).acquire()
                try:
                    self.assertEqual(one.tell(), 3)
                    for writable in (False, True):
                        with self.assertRaises(OSError):
                            host.MemberLock(two, writable).acquire()
                finally:
                    lock.release()
                with host.MemberLock(two):
                    pass
            self.assertEqual(path.read_bytes(), b"original")

    def test_readers_can_share_but_writer_cannot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "member"
            path.touch()
            with path.open("rb") as one, path.open("rb") as two, path.open("r+b") as writer:
                with host.MemberLock(one, False), host.MemberLock(two, False):
                    with self.assertRaises(OSError):
                        host.MemberLock(writer, True).acquire()

    def test_release_after_close_and_repeated_release(self):
        file = tempfile.TemporaryFile("w+b")
        lock = host.MemberLock(file).acquire()
        file.close()
        lock.release()
        lock.release()


class CapacityTests(unittest.TestCase):
    def test_shared_volume_divides_capacity_and_reserves_once(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "one", Path(directory) / "two"]
            usage = mock.Mock(free=100 * 4096)
            with mock.patch.object(host, "get_volume_key", return_value="same"), mock.patch.object(host.shutil, "disk_usage", return_value=usage):
                report = host.capacity_report(paths, reserve_fraction=0.1, reserve_bytes=0)
            self.assertEqual(report["recommended_size"], 45 * 4096)
            self.assertEqual(report["volumes"][0]["count"], 2)

    def test_distinct_volumes_use_minimum_and_align_down(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "one", Path(directory) / "two"]
            with mock.patch.object(host, "get_volume_key", side_effect=["a", "b"]), mock.patch.object(host.shutil, "disk_usage", side_effect=[mock.Mock(free=10000), mock.Mock(free=22000)]):
                report = host.capacity_report(paths, reserve_fraction=0, reserve_bytes=1000)
            self.assertEqual(report["recommended_size"], 8192)

    def test_duplicate_and_hardlink_aliases_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            one = Path(directory) / "one"
            with self.assertRaises(ValueError):
                host.capacity_report([one, one])
            one.write_bytes(b"x")
            two = Path(directory) / "two"
            try:
                os.link(one, two)
            except OSError:
                self.skipTest("Host does not support hard links")
            with self.assertRaises(ValueError):
                host.capacity_report([one, two])

    def test_invalid_locations_and_reserves(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                host.capacity_report([Path(directory)])
            with self.assertRaises(FileNotFoundError):
                host.capacity_report([Path(directory) / "missing" / "file"])
            for reserve in (-0.1, 1, float("nan")):
                with self.assertRaises(ValueError):
                    host.capacity_report([Path(directory) / "file"], reserve_fraction=reserve)


class FilenameTests(unittest.TestCase):
    def test_portable_components_cannot_escape_parent(self):
        names = ["", ".", "..", "../parent", "C:\\bad\\file", "NUL.txt", "con", "COM\u00b9", "lpt9.log", "end. ", "bad\x00name", "a\u202eb", ".hidden", "\ud800"]
        for name in names:
            with self.subTest(name=repr(name)):
                result = host.sanitize_component(name)
                self.assertTrue(result)
                self.assertNotIn(result, (".", ".."))
                self.assertFalse(result.startswith("."))
                self.assertFalse(result.endswith((".", " ")))
                self.assertFalse(any(char in result for char in '<>:"/\\|?*\x00'))
                self.assertFalse(host._reserved(result))

    def test_byte_limit_and_truncation_cannot_create_reserved_names(self):
        self.assertEqual(host.sanitize_component("CONNECTION", 3), "_CO")
        for length in (1, 2, 3, 7, 180):
            value = host.sanitize_component("\U0001f642" * 100, length)
            self.assertLessEqual(len(value.encode("utf-8")), length)
            self.assertTrue(value)

    def test_collisions_include_case_unicode_and_sanitization(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / "A_B.txt").touch()
            (parent / "a_b (2).txt").touch()
            self.assertEqual(host.unique_export_path(parent, "a:b.txt").name, "a_b (3).txt")
            (parent / "\u00e9.txt").touch()
            self.assertEqual(host.unique_export_path(parent, "e\u0301.txt").name, "\u00e9 (2).txt")
            self.assertEqual(len(list(parent.iterdir())), 3)

    def test_hidden_flags_and_dotfiles(self):
        self.assertTrue(host.is_hidden(Path(".not-present")))
        with mock.patch.object(Path, "lstat", return_value=mock.Mock(st_file_attributes=2, st_flags=0)):
            self.assertTrue(host.is_hidden(Path("hidden")))
        with mock.patch.object(Path, "lstat", return_value=mock.Mock(st_file_attributes=0, st_flags=0x8000)):
            self.assertTrue(host.is_hidden(Path("hidden")))
        with tempfile.TemporaryDirectory() as directory:
            visible = Path(directory) / "visible"
            visible.touch()
            self.assertFalse(host.is_hidden(visible))


if __name__ == "__main__":
    unittest.main()
