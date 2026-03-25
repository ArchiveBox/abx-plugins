#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "abx-plugins",
# ]
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

import os
import subprocess
import uuid
import re
from pathlib import Path
from collections.abc import Iterable

from abx_plugins.plugins.base.utils import load_config


LEGACY_TIMESTAMP_RE = re.compile(r"^\d{10}(?:\.\d+)?$")
DEFAULT_CONTENT_EXCLUDES = ("*.json", "*.jsonl", "*.log", "*.pid", "*.css", "*.js")
DEEP_EXCLUDES = ("*.pid", "*.css", "*.js")


def _get_archive_dir() -> Path:
    snap_dir = os.environ["SNAP_DIR"].strip() if "SNAP_DIR" in os.environ else ""
    if snap_dir:
        return Path(snap_dir)
    return Path.cwd()


def _get_search_roots() -> list[Path]:
    base_dir = _get_archive_dir()
    roots: list[Path] = []

    if base_dir.name in {"archive", "snapshots"} and base_dir.exists():
        roots.append(base_dir)

    users_dir = base_dir / "users"
    if users_dir.is_dir():
        roots.extend(
            sorted(path for path in users_dir.glob("*/snapshots") if path.is_dir()),
        )

    archive_dir = base_dir / "archive"
    if archive_dir.is_dir():
        roots.append(archive_dir)

    if roots:
        return roots

    if base_dir.exists():
        return [base_dir]

    return []


def _is_snapshot_id(segment: str) -> bool:
    try:
        uuid.UUID(segment)
        return True
    except (ValueError, AttributeError, TypeError):
        return bool(LEGACY_TIMESTAMP_RE.fullmatch(str(segment or "").strip()))


def _extract_snapshot_id(match_path: Path, search_roots: list[Path]) -> str | None:
    for root in search_roots:
        try:
            relative = match_path.relative_to(root)
        except ValueError:
            continue

        for segment in relative.parts:
            if _is_snapshot_id(segment):
                return segment

        if root.name == "archive" and relative.parts:
            return relative.parts[0]

    return None


def search(query: str, search_mode: str = "contents") -> list[str]:
    """Search for snapshots using ripgrep."""
    config = load_config(Path(__file__).with_name("config.json"))

    rg_binary = str(config.RIPGREP_BINARY or "")
    if not rg_binary or not Path(rg_binary).expanduser().is_file():
        raise RuntimeError(
            "ripgrep binary not found. Install with: apt install ripgrep",
        )

    timeout = int(config.RIPGREP_TIMEOUT)
    ripgrep_args = [arg for arg in config.RIPGREP_ARGS if arg not in {"--follow", "-L"}]
    ripgrep_args_extra = [
        arg for arg in config.RIPGREP_ARGS_EXTRA if arg not in {"--follow", "-L"}
    ]

    search_roots = _get_search_roots()
    if not search_roots:
        return []

    exclude_globs = DEFAULT_CONTENT_EXCLUDES if search_mode != "deep" else DEEP_EXCLUDES

    cmd = [
        rg_binary,
        *ripgrep_args,
        *ripgrep_args_extra,
        "--files-with-matches",
        "--no-messages",
        *(("--glob", f"!{glob}") for glob in exclude_globs),
        "--regexp",
        query,
        *(str(root) for root in search_roots),
    ]
    flattened_cmd = [
        part for item in cmd for part in (item if isinstance(item, tuple) else (item,))
    ]

    try:
        result = subprocess.run(
            flattened_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        snapshot_ids = set()
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            path = Path(line.strip())
            snapshot_id = _extract_snapshot_id(path, search_roots)
            if snapshot_id:
                snapshot_ids.add(snapshot_id)

        return list(snapshot_ids)

    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []


def flush(snapshot_ids: Iterable[str]) -> None:
    """No-op for ripgrep - it searches files directly."""
    pass
