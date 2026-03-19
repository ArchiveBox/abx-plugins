from __future__ import annotations

import os
from pathlib import Path


def _resolve_path(path_value: str) -> Path:
    return Path(path_value).expanduser().resolve()


def get_lib_dir() -> Path:
    """Return library directory.

    Priority: LIB_DIR env var, otherwise ~/.config/abx/lib.
    """
    lib_dir = os.environ.get("LIB_DIR", "").strip()
    if lib_dir:
        return _resolve_path(lib_dir)
    return _resolve_path(str(Path.home() / ".config" / "abx" / "lib"))


def get_personas_dir() -> Path:
    """Return personas directory.

    Priority: PERSONAS_DIR env var, otherwise ~/.config/abx/personas.
    """
    personas_dir = os.environ.get("PERSONAS_DIR", "").strip()
    if personas_dir:
        return _resolve_path(personas_dir)
    return _resolve_path(str(Path.home() / ".config" / "abx" / "personas"))
