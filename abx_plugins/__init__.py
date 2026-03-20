"""Plugin suite package for ArchiveBox-compatible tools."""

from __future__ import annotations

from pathlib import Path


def get_plugins_dir() -> Path:
    """Return the filesystem path to the bundled plugins directory."""
    return Path(__file__).resolve().parent / "plugins"


__all__ = ["get_plugins_dir"]
