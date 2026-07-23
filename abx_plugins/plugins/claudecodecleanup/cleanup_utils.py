"""Deterministic filesystem inventory for Claude Code cleanup decisions."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
from collections import defaultdict
from pathlib import Path
from typing import BinaryIO, TypedDict
from collections.abc import Sequence


PROCESS_CONTROL_SUFFIXES = (".stdout.log", ".stderr.log", ".pid", ".sh")
TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".mhtml",
    ".rss",
    ".svg",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MAX_FILES = 10_000
MAX_DIRECTORIES = 2_048
MAX_FILESYSTEM_ENTRIES = MAX_FILES + MAX_DIRECTORIES
MAX_HASH_BYTES = 64 * 1024 * 1024
MAX_SAMPLE_BYTES = 64 * 1024


class InventoryEntry(TypedDict):
    path: str
    abspath: Path
    size: int
    mimetype: str
    content_kind: str
    sample: str
    regular: bool


def ensure_owned_output_dir(snap_dir: Path, output_dir: Path) -> Path:
    """Create the hook-owned output directory without following a symlink."""
    snap_dir = snap_dir.resolve(strict=True)
    requested_output_dir = output_dir.absolute()
    if requested_output_dir.parent.resolve(strict=True) != snap_dir:
        raise ValueError(
            f"Cleanup output directory escapes snapshot: {requested_output_dir}",
        )
    output_dir = snap_dir / requested_output_dir.name
    if output_dir.is_symlink():
        raise ValueError(
            f"Cleanup output directory must not be a symlink: {output_dir}",
        )
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"Cleanup output path must be a directory: {output_dir}")
    output_dir.mkdir(mode=0o700, exist_ok=True)
    return output_dir


def write_owned_output_file(
    snap_dir: Path,
    output_dir: Path,
    filename: str,
    content: str,
) -> Path:
    """Write one hook-owned file without following a replacement symlink."""
    if Path(filename).name != filename:
        raise ValueError(f"Cleanup output filename must be a basename: {filename}")
    output_dir = ensure_owned_output_dir(snap_dir, output_dir)
    directory_fd = os.open(
        output_dir,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        file_fd = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(file_fd, "w", encoding="utf-8") as file:
            file.write(content)
    finally:
        os.close(directory_fd)
    return output_dir / filename


def _open_regular_file(path: Path) -> BinaryIO:
    file_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        raise ValueError(f"Cleanup inventory path is not a regular file: {path}")
    return os.fdopen(file_fd, "rb")


def _sha256(path: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    with _open_regular_file(path) as file:
        remaining = expected_size
        while remaining:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining:
            raise ValueError(f"Cleanup inventory file shrank during hashing: {path}")
        if file.read(1):
            raise ValueError(f"Cleanup inventory file grew during hashing: {path}")
    return digest.hexdigest()


def _detect_content_kind(data: bytes) -> str:
    """Identify common output content from a bounded file prefix."""
    if not data:
        return "empty"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"\xff\xd8\xff",)):
        return "image/jpeg"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if data.startswith(b"SQLite format 3\x00"):
        return "application/x-sqlite3"

    stripped = data.lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<?xml")):
        return "text/html"
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    printable = sum(
        character.isprintable() or character.isspace() for character in decoded
    )
    if decoded and printable / len(decoded) >= 0.9:
        return "text/plain"
    return "application/octet-stream"


def _is_text_file(path: Path, mimetype: str, content_kind: str) -> bool:
    return (
        content_kind.startswith("text/")
        or mimetype.startswith("text/")
        or path.suffix.lower() in TEXT_SUFFIXES
    )


def build_cleanup_inventory(
    snap_dir: Path,
    output_dir: Path,
    *,
    max_bytes: int = 64 * 1024,
    sample_bytes: int = 200,
    max_files: int = MAX_FILES,
    max_directories: int = MAX_DIRECTORIES,
    max_filesystem_entries: int = MAX_FILESYSTEM_ENTRIES,
) -> str:
    """Inspect the snapshot once and return a hard-bounded cleanup inventory."""
    if max_bytes < 1024:
        raise ValueError("Cleanup inventory max_bytes must be at least 1024")
    if sample_bytes < 1:
        raise ValueError("Cleanup inventory sample_bytes must be positive")
    if min(max_files, max_directories, max_filesystem_entries) < 1:
        raise ValueError("Cleanup inventory traversal limits must be positive")
    snap_dir = snap_dir.resolve(strict=True)
    output_dir = ensure_owned_output_dir(snap_dir, output_dir)
    entries: list[InventoryEntry] = []
    directory_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    empty_directories: list[str] = []
    excluded_control_files = 0
    files_inspected = 0
    directories_inspected = 0
    omitted_file_entries = 0
    omitted_directory_entries = 0
    filesystem_entries_inspected = 0
    traversal_limit_reached = False
    sample_bytes_read = 0
    omitted_text_samples = 0
    pending_directories = [snap_dir]

    while pending_directories:
        current_dir = pending_directories.pop()
        children: list[Path] = []
        with os.scandir(current_dir) as directory:
            for child in directory:
                path = Path(child.path)
                if path.absolute() == output_dir:
                    continue
                if filesystem_entries_inspected >= max_filesystem_entries:
                    traversal_limit_reached = True
                    break
                filesystem_entries_inspected += 1
                children.append(path)
        children.sort(key=lambda path: path.name)

        if current_dir != snap_dir and not children:
            relative_dir = current_dir.relative_to(snap_dir)
            if len(empty_directories) < max_directories:
                empty_directories.append(str(relative_dir))
            else:
                omitted_directory_entries += 1

        child_directories: list[Path] = []
        for path in children:
            file_stat = path.lstat()
            if stat.S_ISDIR(file_stat.st_mode):
                if directories_inspected >= max_directories:
                    traversal_limit_reached = True
                    omitted_directory_entries += 1
                    break
                directories_inspected += 1
                relative_dir = path.relative_to(snap_dir)
                directory_totals[relative_dir.parts[0]]
                child_directories.append(path)
                continue
            if path.name.endswith(PROCESS_CONTROL_SUFFIXES):
                excluded_control_files += 1
                continue
            if files_inspected >= max_files:
                traversal_limit_reached = True
                omitted_file_entries += 1
                break
            relative_path = path.relative_to(snap_dir)
            regular_file = stat.S_ISREG(file_stat.st_mode)
            size = file_stat.st_size
            mimetype = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if regular_file
                else "inode/symlink"
            )
            content_kind = "inode/symlink"
            sample = ""
            if regular_file:
                remaining_sample_bytes = MAX_SAMPLE_BYTES - sample_bytes_read
                if remaining_sample_bytes > 0:
                    with _open_regular_file(path) as file:
                        sample_data = file.read(
                            min(sample_bytes, remaining_sample_bytes),
                        )
                    sample_bytes_read += len(sample_data)
                    content_kind = _detect_content_kind(sample_data)
                    if content_kind.startswith("text/"):
                        sample = sample_data.decode("utf-8", errors="replace")
                else:
                    content_kind = "not-sampled"
                    omitted_text_samples += 1
            top_level = relative_path.parts[0] if len(relative_path.parts) > 1 else "."
            directory_totals[top_level][0] += 1
            directory_totals[top_level][1] += size
            files_inspected += 1
            entries.append(
                {
                    "path": str(relative_path),
                    "abspath": path,
                    "size": size,
                    "mimetype": mimetype,
                    "content_kind": content_kind,
                    "sample": sample,
                    "regular": regular_file,
                },
            )
        if traversal_limit_reached:
            break
        pending_directories.extend(reversed(child_directories))

    same_size: dict[int, list[InventoryEntry]] = defaultdict(list)
    for entry in entries:
        if entry["regular"]:
            same_size[entry["size"]].append(entry)

    same_hash: dict[tuple[int, str], list[str]] = defaultdict(list)
    unverified_same_size_groups: list[dict[str, object]] = []
    hash_bytes_read = 0
    for size, candidates in sorted(same_size.items()):
        if len(candidates) < 2:
            continue
        group_bytes = size * len(candidates)
        if hash_bytes_read + group_bytes > MAX_HASH_BYTES:
            unverified_same_size_groups.append(
                {
                    "size": size,
                    "paths": sorted(str(entry["path"]) for entry in candidates),
                    "reason": "deterministic hash byte budget exhausted",
                },
            )
            continue
        for entry in candidates:
            same_hash[(size, _sha256(entry["abspath"], size))].append(entry["path"])
        hash_bytes_read += group_bytes

    duplicate_groups = [
        {"size": size, "sha256": digest, "paths": sorted(paths)}
        for (size, digest), paths in sorted(same_hash.items())
        if len(paths) > 1
    ]

    text_samples: list[dict[str, object]] = []
    for entry in entries:
        path = entry["abspath"]
        mimetype = entry["mimetype"]
        content_kind = entry["content_kind"]
        if (
            not entry["regular"]
            or not entry["sample"]
            or not _is_text_file(path, mimetype, content_kind)
        ):
            continue
        text_samples.append(
            {
                "path": entry["path"],
                "size": entry["size"],
                "mimetype": mimetype,
                "content_kind": content_kind,
                "sample": entry["sample"],
            },
        )

    file_metadata = [
        {
            "path": entry["path"],
            "size": entry["size"],
            "mimetype": entry["mimetype"],
            "content_kind": entry["content_kind"],
        }
        for entry in entries
    ]
    lines = [
        "ARCHIVEBOX CLEANUP INVENTORY v1",
        json.dumps(
            {
                "snapshot": str(snap_dir),
                "files_inspected": files_inspected,
                "directories_inspected": directories_inspected,
                "filesystem_entries_inspected": filesystem_entries_inspected,
                "process_control_files_excluded": excluded_control_files,
                "file_entry_limit": max_files,
                "directory_entry_limit": max_directories,
                "filesystem_entry_limit": max_filesystem_entries,
                "hash_byte_limit": MAX_HASH_BYTES,
                "hash_bytes_read": hash_bytes_read,
                "sample_byte_limit": MAX_SAMPLE_BYTES,
                "sample_bytes_read": sample_bytes_read,
            },
            sort_keys=True,
        ),
    ]
    used_bytes = sum(len(line.encode("utf-8")) + 1 for line in lines)
    body_limit = max_bytes - 512

    def append_bounded(line: str) -> bool:
        nonlocal used_bytes
        line_bytes = len(line.encode("utf-8")) + 1
        if used_bytes + line_bytes > body_limit:
            return False
        lines.append(line)
        used_bytes += line_bytes
        return True

    omitted_serialized_entries: dict[str, int] = defaultdict(int)

    def append_section(name: str, values: Sequence[object]) -> None:
        if not append_bounded(name):
            omitted_serialized_entries[name] += len(values)
            return
        for value in values:
            serialized = json.dumps(value, sort_keys=True)
            if not append_bounded(serialized):
                omitted_serialized_entries[name] += 1

    append_section(
        "DIRECTORY_SUMMARY",
        [
            {"directory": name, "files": values[0], "total_size": values[1]}
            for name, values in sorted(directory_totals.items())
        ],
    )
    append_section("EMPTY_DIRECTORIES", sorted(empty_directories))
    append_section("DUPLICATE_GROUPS", duplicate_groups)
    append_section("UNVERIFIED_SAME_SIZE_GROUPS", unverified_same_size_groups)
    append_section("FILE_METADATA", file_metadata)
    append_section("TEXT_SAMPLES", text_samples)

    truncated = bool(
        omitted_file_entries
        or omitted_directory_entries
        or omitted_text_samples
        or traversal_limit_reached
        or unverified_same_size_groups
        or omitted_serialized_entries,
    )
    lines.append(
        json.dumps(
            {
                "inventory_truncated": truncated,
                "traversal_limit_reached": traversal_limit_reached,
                "omitted_file_entries": omitted_file_entries,
                "omitted_directory_entries": omitted_directory_entries,
                "omitted_text_samples": omitted_text_samples,
                "omitted_serialized_entries": dict(omitted_serialized_entries),
            },
            sort_keys=True,
        ),
    )
    inventory = "\n".join(lines)
    if len(inventory.encode("utf-8")) > max_bytes:
        raise ValueError("Cleanup inventory exceeded its hard byte limit")
    return inventory
