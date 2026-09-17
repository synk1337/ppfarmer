"""Where the application reads its resources and writes its data.

A frozen build breaks both assumptions of a source checkout: `__file__` no
longer points at a real directory, and the executable may sit somewhere the
user cannot write to. So resources come from the bundle and data goes to the
user's own profile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ppfarmer"


def frozen() -> bool:
    """Is this running from a PyInstaller build rather than from source?"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> Path:
    """Directory holding bundled read-only files, such as the page."""
    if frozen():
        return Path(sys._MEIPASS)                      # noqa: SLF001
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Directory for everything the app writes.

    From source it is the working directory, which keeps a checkout
    self-contained. Frozen, it is the user's local app data: an executable can
    live in Downloads or Program Files, where writing is unreliable or simply
    not allowed.
    """
    if not frozen():
        return Path.cwd()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db() -> str:
    return str(data_dir() / "ppfarmer.db")


def beatmap_cache() -> Path:
    return data_dir() / ".osu_cache"
