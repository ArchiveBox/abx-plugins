#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click",
#   "abx-plugins",
# ]
# ///
#
# Generate a cryptographic Merkle hash tree of all files in a snapshot directory.
# Outputs hashes.json containing root_hash, tree structure, file list, and metadata.
#
# Usage:
#     ./on_Snapshot__93_hashes.py [...] > events.jsonl

import os
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import click

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config


PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def sha256_file(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return "0" * 64


def sha256_data(data: bytes) -> str:
    """Compute SHA256 hash of raw data."""
    return hashlib.sha256(data).hexdigest()


def collect_files(
    snapshot_dir: Path,
    exclude_dirs: list[str] | None = None,
) -> list[tuple[Path, str, int]]:
    """Recursively collect all files in snapshot directory."""
    exclude_dirs = exclude_dirs or ["hashes", ".git", "__pycache__"]
    files = []

    for root, dirs, filenames in os.walk(snapshot_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for filename in filenames:
            filepath = Path(root) / filename
            rel_path = filepath.relative_to(snapshot_dir)

            if filepath.is_symlink():
                continue

            file_hash = sha256_file(filepath)
            file_size = filepath.stat().st_size if filepath.exists() else 0
            files.append((rel_path, file_hash, file_size))

    files.sort(key=lambda x: str(x[0]))
    return files


def build_merkle_tree(file_hashes: list[str]) -> tuple[str, list[list[str]]]:
    """Build a Merkle tree from a list of leaf hashes."""
    if not file_hashes:
        return sha256_data(b""), [[]]

    tree_levels = [file_hashes.copy()]

    while len(tree_levels[-1]) > 1:
        current_level = tree_levels[-1]
        next_level = []

        for i in range(0, len(current_level), 2):
            left = current_level[i]
            if i + 1 < len(current_level):
                right = current_level[i + 1]
                combined = left + right
            else:
                combined = left + left

            parent_hash = sha256_data(combined.encode("utf-8"))
            next_level.append(parent_hash)

        tree_levels.append(next_level)

    root_hash = tree_levels[-1][0]
    return root_hash, tree_levels


def create_hashes(snapshot_dir: Path) -> dict[str, Any]:
    """Create a complete Merkle hash tree of all files in snapshot directory."""
    files = collect_files(snapshot_dir)
    file_hashes = [file_hash for _, file_hash, _ in files]
    root_hash, tree_levels = build_merkle_tree(file_hashes)
    total_size = sum(size for _, _, size in files)

    file_list = [
        {"path": str(path), "hash": file_hash, "size": size}
        for path, file_hash, size in files
    ]

    return {
        "root_hash": root_hash,
        "tree_levels": tree_levels,
        "files": file_list,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_count": len(files),
            "total_size": total_size,
            "tree_depth": len(tree_levels),
        },
    }


def format_output_str(total_size: int, root_hash: str | None) -> str:
    total_size_mb = total_size / 1_000_000
    short_hash = (root_hash or "")[:12]
    return f"{total_size_mb:.1f}MB {short_hash}".strip()


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL being archived")
def main(url: str):
    """Generate Merkle tree of all archived outputs."""
    status = "failed"
    error = ""
    root_hash = None
    total_size = 0

    try:
        # Check if enabled
        save_hashes = os.getenv("HASHES_ENABLED", "true").lower() in (
            "true",
            "1",
            "yes",
            "on",
        )

        if not save_hashes:
            status = "skipped"
            emit_archive_result_record(status, "HASHES_ENABLED=False")
            sys.exit(0)

        # Working directory is the extractor output dir (e.g., <snapshot>/hashes/)
        # Parent is the snapshot directory
        output_dir = Path.cwd()
        snapshot_dir = output_dir.parent

        if not snapshot_dir.exists():
            raise FileNotFoundError(f"Snapshot directory not found: {snapshot_dir}")

        # Ensure output directory exists
        output_dir.mkdir(exist_ok=True)
        output_path = output_dir / "hashes.json"

        # Generate Merkle tree
        merkle_data = create_hashes(snapshot_dir)

        # Write output
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(merkle_data, f, indent=2)

        status = "succeeded"
        root_hash = merkle_data["root_hash"]
        total_size = merkle_data["metadata"]["total_size"]

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"
        click.echo(f"Error: {error}", err=True)

    output_str = (
        format_output_str(total_size, root_hash)
        if status == "succeeded"
        else (error or "")
    )
    emit_archive_result_record(status, output_str)

    sys.exit(0 if status in ("succeeded", "skipped") else 1)


if __name__ == "__main__":
    main()
