"""File hashing and native copy-on-write deduplication shared by the hashes hook."""

import hashlib
import os
import stat
import struct
import sys
from pathlib import Path
from typing import Any


def sha256_file(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def deduplicate_files(
    snapshot_dir: Path,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Share identical extents while keeping every output independently writable."""
    result = {
        "supported_platform": sys.platform == "linux",
        "attempted_files": 0,
        "extent_bytes_verified": 0,
        "failed_files": 0,
    }
    if sys.platform != "linux":
        return result
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
                result["attempted_files"] += 1
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
                        result["failed_files"] += 1
                        break
                    result["extent_bytes_verified"] += deduped
        except OSError:
            # Unsupported filesystems (including remote mounts) retain separate
            # files. A failed space optimization must never change saved bytes.
            result["failed_files"] += 1
    return result
