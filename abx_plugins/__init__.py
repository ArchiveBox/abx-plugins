"""Plugin suite package for ArchiveBox-compatible tools."""

from __future__ import annotations

from pathlib import Path
from importlib import resources


def get_plugins_dir() -> Path:
    """Return the filesystem path to the bundled plugins directory."""
    return Path(resources.files(__name__) / "plugins")


__all__ = ["get_plugins_dir"]
