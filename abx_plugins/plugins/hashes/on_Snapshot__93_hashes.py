#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
#
# Generate a cryptographic Merkle hash tree of all files in a snapshot directory.
# Outputs hashes.json containing root_hash, tree structure, file list, and metadata.
#
# Usage:
#     ./on_Snapshot__93_hashes.py [...] > events.jsonl

import hashlib
import json
import os
import re
import stat
import struct
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    load_config,
    write_text_atomic,
)

PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def sha256_file(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def sha256_data(data: bytes) -> str:
    """Compute SHA256 hash of raw data."""
    return hashlib.sha256(data).hexdigest()


def collect_files(
    snapshot_dir: Path,
    exclude_dirs: list[str] | None = None,
) -> list[tuple[Path, str, int]]:
    """Recursively collect all files in snapshot directory."""
    exclude_dirs = exclude_dirs or [
        "hashes",
        "opentimestamps",
        ".git",
        "__pycache__",
        "chrome_profile",
        "archivebox-extensions",
    ]
    files = []

    for root, dirs, filenames in os.walk(snapshot_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        if Path(root) == snapshot_dir:
            # Session state belongs to the runner and can change after this
            # snapshot finishes (or be shared with the next snapshot).
            dirs[:] = [d for d in dirs if d not in {".persona", ".abx-dl"}]

        for filename in filenames:
            filepath = Path(root) / filename
            rel_path = filepath.relative_to(snapshot_dir)

            # Crawl cleanup can run after the snapshot's final-file hooks when
            # both share one output root. Exclude only disposable Chrome session
            # markers, not retained evidence such as random_seed.json or cmd.sh.
            if rel_path.parent == Path("chrome") and filename in {
                "cdp_url.txt",
                "target_id.txt",
                "chrome.pid",
                "browser.json",
                "url.txt",
                "navigation.json",
            }:
                continue

            # The runner keeps appending lifecycle records after hashing. These
            # are execution bookkeeping, not archived content or evidence.
            if rel_path == Path("index.jsonl"):
                continue

            if len(rel_path.parts) == 2 and re.fullmatch(
                r"on_[A-Za-z]+__.+\.[0-9a-f]{32}\.(?:sh|pid|stdout\.log|stderr\.log)(?:\.\d+)?",
                filename,
            ):
                continue

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
            "timestamp": datetime.now(UTC).isoformat(),
            "file_count": len(files),
            "total_size": total_size,
            "tree_depth": len(tree_levels),
        },
    }


def deduplicate_files(snapshot_dir: Path, files: list[dict[str, Any]]) -> None:
    """Share identical extents while keeping every output independently writable."""
    if sys.platform != "linux":
        return
    import fcntl

    originals: dict[tuple[str, int], Path] = {}
    for item in files:
        key = (str(item["hash"]), int(item["size"]))
        # Empty files reclaim nothing; a zero digest means hashing failed, not equality.
        if key[1] <= 0 or key[0] == "0" * 64:
            continue
        path = snapshot_dir / item["path"]
        source = originals.setdefault(key, path)
        if source == path:
            continue
        try:
            source_stat, path_stat = source.lstat(), path.lstat()
            if (
                os.path.samestat(source_stat, path_stat)
                or source_stat.st_dev != path_stat.st_dev
                or not stat.S_ISREG(source_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or source_stat.st_size != key[1]
                or path_stat.st_size != key[1]
            ):
                continue
            # Hardlinks let a later extractor overwrite another retained output
            # and merge their timestamps. FIDEDUPERANGE keeps both inodes and
            # metadata, verifies equality in the kernel, and makes future writes
            # private. Its range limit is filesystem-dependent (commonly 16 MiB).
            with source.open("rb") as src, path.open("r+b") as dst:
                for offset in range(0, key[1], 16 * 1024 * 1024):
                    length = min(16 * 1024 * 1024, key[1] - offset)
                    # linux/fs.h: file_dedupe_range followed by one destination.
                    request = bytearray(
                        struct.pack(
                            "=QQHHIqQQiI",
                            offset,
                            length,
                            1,
                            0,
                            0,
                            dst.fileno(),
                            offset,
                            0,
                            0,
                            0,
                        ),
                    )
                    fcntl.ioctl(src.fileno(), 0xC0189436, request, True)
                    _, _, _, _, _, _, _, deduped, status, _ = struct.unpack(
                        "=QQHHIqQQiI",
                        request,
                    )
                    if status != 0 or deduped != length:
                        break
        except OSError:
            # Unsupported filesystems (including remote mounts) retain separate
            # files. A failed space optimization must never change saved bytes.
            pass


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
        # Invalidate completion before any work, including disabled/failed reruns.
        completion_path = OUTPUT_DIR / "hashes.sha256"
        completion_path.unlink(missing_ok=True)
        # Check if enabled
        save_hashes = CONFIG.HASHES_ENABLED

        if not save_hashes:
            status = "skipped"
            emit_archive_result_record(status, "HASHES_ENABLED=False")
            sys.exit(0)

        print("Hash generation started", file=sys.stderr, flush=True)
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
        phase_started = time.monotonic()
        merkle_data = create_hashes(snapshot_dir)
        counts = (
            f"files={merkle_data['metadata']['file_count']} "
            f"bytes={merkle_data['metadata']['total_size']}"
        )
        print(
            f"phase=hash complete elapsed_ms={(time.monotonic() - phase_started) * 1000:.3f} {counts}",
            file=sys.stderr,
            flush=True,
        )
        phase_started = time.monotonic()
        deduplicate_files(snapshot_dir, merkle_data["files"])
        print(
            f"phase=dedupe complete elapsed_ms={(time.monotonic() - phase_started) * 1000:.3f} {counts}",
            file=sys.stderr,
            flush=True,
        )

        # Write output
        manifest = json.dumps(merkle_data, indent=2)
        write_text_atomic(output_path, manifest)
        # Consumers require this last, atomic publication and check its digest.
        write_text_atomic(completion_path, sha256_data(manifest.encode("utf-8")) + "\n")

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
