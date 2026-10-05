"""Desktop launcher plus a bundled end-to-end diagnostic."""
import argparse
import ftplib
import io
import json
from pathlib import Path
import sys
import tempfile
import traceback


def self_test(gui=False):
    from .diagnostics import check_multiprocessing, isolated_state
    from .engine import Array
    helper_tests = check_multiprocessing()
    with tempfile.TemporaryDirectory(prefix="raidiant-self-test-") as directory, isolated_state(Path(directory) / "state"):
        base = Path(directory)
        paths = [base / f"member-{index}.r5m" for index in range(5)]
        source = base / "source.bin"
        content = bytes(range(256)) * 1024 + b"end"
        source.write_bytes(content)
        with Array.create(paths, parity=2, member_size=2 * 1024**2, chunk_size=4096) as array:
            array.upload_files([source])
            entry = array.list_dir()[0]
            # Exercise imports collected by PyInstaller and real loopback FTP.
            from .ftp_service import FTPService
            server = FTPService(array, "0123", "4567", port=0)
            try:
                server.start()
                with ftplib.FTP() as client:
                    client.connect("127.0.0.1", server.status["port"], timeout=10)
                    client.login("0123", "4567")
                    client.storbinary("STOR ftp-diagnostic.bin", io.BytesIO(content))
                    downloaded = bytearray()
                    client.retrbinary("RETR ftp-diagnostic.bin", downloaded.extend)
                    if bytes(downloaded) != content:
                        raise AssertionError("FTP roundtrip failed")
                    client.delete("ftp-diagnostic.bin")
            finally:
                server.stop()
            if gui:
                import tkinter as tk
                from . import gui as desktop
                settings_path = desktop._settings_path
                desktop._settings_path = lambda: base / "desktop.json"
                root = None
                app = None
                try:
                    root = tk.Tk()
                    root.withdraw()
                    app = desktop.RaidApp(root)
                    app._opened(array)
                    root.update_idletasks()
                    if len(app.tree.get_children()) != 1:
                        raise AssertionError("GUI file listing failed")
                finally:
                    try:
                        if app is not None:
                            app.array = None
                            app._closing = True
                        if root is not None:
                            root.destroy()
                    finally:
                        desktop._settings_path = settings_path
        with Array.open(paths[1:4]) as array:
            if array.status["state"] != "degraded":
                raise AssertionError("Degraded state failed")
            output = base / "out"
            output.mkdir()
            if array.export(entry["id"], output).read_bytes() != content:
                raise AssertionError("Two-member reconstruction failed")
            array.rebuild({0: base / "new0.r5m", 4: base / "new4.r5m"})
            if not array.status["writable"] or array.scrub()["damaged_shards"]:
                raise AssertionError("Rebuild verification failed")
        return {"ok": True, "tests": helper_tests + ["grow-on-demand creation", "import", "FTP upload/download/delete", "two simultaneous failures", "verified export", "rebuild", "scrub"] + (["GUI file listing"] if gui else [])}


def main(argv=None):
    # PyInstaller helpers re-enter this executable with worker/tracker arguments.
    # Divert them before argparse or any storage, FTP, or GUI initialization.
    # PyInstaller supplies the required cross-platform override of this API.
    import multiprocessing
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(description="RAIDiant file-backed redundant storage")
    parser.add_argument("--self-test", action="store_true", help="Run a temporary five-member array diagnostic and exit")
    parser.add_argument("--gui-smoke-test", action="store_true", help="Include a hidden Tk window in the diagnostic")
    parser.add_argument("--report", type=Path, help="Write diagnostic JSON to this file")
    args = parser.parse_args(argv)
    if args.self_test or args.gui_smoke_test:
        try:
            result = self_test(gui=args.gui_smoke_test)
        except Exception:
            result = {"ok": False, "error": traceback.format_exc()}
        text = json.dumps(result, indent=2)
        if args.report:
            args.report.write_text(text, encoding="utf-8")
        if sys.stdout:
            print(text)
        return 0 if result["ok"] else 1
    from .gui import main as desktop_main
    desktop_main()
    return 0
