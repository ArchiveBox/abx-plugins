#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Ripgrep search backend - searches files directly without indexing.
#
# This backend doesn't maintain an index - it searches archived files directly
# using ripgrep (rg). This is simpler but slower for large archives.
#
# Environment variables:
#     RIPGREP_BINARY: Path to ripgrep binary (default: rg)
#     RIPGREP_ARGS: Default ripgrep arguments (JSON array)
#     RIPGREP_ARGS_EXTRA: Extra arguments to append (JSON array)
#     RIPGREP_TIMEOUT: Search timeout in seconds (default: 90)

import json
import os
import subprocess
import shutil
import sys
from pathlib import Path
from typing import Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from base.utils import get_env, get_env_int, get_env_array


def _get_archive_dir() -> Path:
    snap_dir = os.environ.get("SNAP_DIR", "").strip()
    if snap_dir:
        return Path(snap_dir)
    return Path.cwd()


def search(query: str) -> List[str]:
    """Search for snapshots using ripgrep."""
    rg_binary = get_env("RIPGREP_BINARY", "rg")
    rg_binary = shutil.which(rg_binary) or rg_binary
    if not rg_binary or not Path(rg_binary).exists():
        raise RuntimeError(
            "ripgrep binary not found. Install with: apt install ripgrep"
        )

    timeout = get_env_int("RIPGREP_TIMEOUT", 90)
    ripgrep_args = get_env_array("RIPGREP_ARGS", [])
    ripgrep_args_extra = get_env_array("RIPGREP_ARGS_EXTRA", [])

    archive_dir = _get_archive_dir()
    if not archive_dir.exists():
        return []

    cmd = [
        rg_binary,
        *ripgrep_args,
        *ripgrep_args_extra,
        "--regexp",
        query,
        str(archive_dir),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        # Extract snapshot IDs from file paths
        # Paths look like: archive/<snapshot_id>/<extractor>/file.txt
        snapshot_ids = set()
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            path = Path(line)
            try:
                relative = path.relative_to(archive_dir)
                snapshot_id = relative.parts[0]
                snapshot_ids.add(snapshot_id)
            except (ValueError, IndexError):
                continue

        return list(snapshot_ids)

    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []


def flush(snapshot_ids: Iterable[str]) -> None:
    """No-op for ripgrep - it searches files directly."""
    pass
