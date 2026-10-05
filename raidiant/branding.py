"""Application identity shared by the source launcher and frozen desktop build."""

from pathlib import Path
import sys


ASSETS = Path(__file__).resolve().parent / "assets"


def prepare_desktop() -> None:
    """Give Windows a stable taskbar identity before the first window exists."""
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "RAIDiant.Desktop.1"
            )
        except (AttributeError, OSError):
            pass


def load_image(window, filename):
    """Decode each asset once per Tcl interpreter and retain its image handle."""
    import tkinter as tk
    owner = window._root()
    cache = getattr(owner, "_raidiant_image_cache", None)
    if cache is None:
        cache = owner._raidiant_image_cache = {}
    path = str(ASSETS / filename)
    if path not in cache:
        cache[path] = tk.PhotoImage(master=owner, file=path)
    return cache[path]


def apply_window_icon(window) -> None:
    """Keep Tk image references alive, including when running from PyInstaller."""
    images = [load_image(window, f"app-icon-{size}.png")
              for size in (32, 64, 128, 256)]
    window._raidiant_icons = images
    window.iconphoto(True, *images)
    if sys.platform == "win32":
        window.iconbitmap(str(ASSETS / "app-icon.ico"))
