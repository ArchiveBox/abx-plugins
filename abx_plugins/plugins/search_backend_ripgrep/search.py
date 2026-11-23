#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
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
import threading
import stat
from fnmatch import fnmatchcase
import uuid
import re
from pathlib import Path
from collections.abc import Iterable, Mapping

from abx_plugins.plugins.base.utils import load_config
from abx_plugins.plugins.base.search_candidates import (
    Candidate,
    Evidence,
    SearchBudget,
    SearchTerm,
)
from abx_plugins.plugins.base.search_command import run_search_command


LEGACY_TIMESTAMP_RE = re.compile(r"^\d{10}(?:\.\d+)?$")
DEFAULT_CONTENT_EXCLUDES = ("*.json", "*.jsonl", "*.log", "*.pid", "*.css", "*.js")
DEEP_EXCLUDES = ("*.pid", "*.css", "*.js")


def _get_archive_dir(environ: Mapping[str, str] | None = None) -> Path:
    runtime_env = os.environ if environ is None else environ
    snap_dir = runtime_env["SNAP_DIR"].strip() if "SNAP_DIR" in runtime_env else ""
    if snap_dir:
        return Path(snap_dir)
    return Path.cwd()


def _get_search_roots(environ: Mapping[str, str] | None = None) -> list[Path]:
    base_dir = _get_archive_dir(environ)
    roots: list[Path] = []

    if base_dir.name == "snapshots" and base_dir.exists():
        roots.append(base_dir)

    def add_user_snapshot_roots(users_dir: Path) -> None:
        roots.extend(
            sorted(path for path in users_dir.glob("*/snapshots") if path.is_dir()),
        )

    if base_dir.is_dir():
        add_user_snapshot_roots(base_dir)

    # Current ArchiveBox layout when SNAP_DIR/DATA_DIR points at the collection
    # or archive root: archive/users/<username>/snapshots/...
    archive_users_dir = base_dir / "archive" / "users"
    if archive_users_dir.is_dir():
        add_user_snapshot_roots(archive_users_dir)

    archive_local_users_dir = base_dir / "users"
    if archive_local_users_dir.is_dir():
        add_user_snapshot_roots(archive_local_users_dir)

    if roots:
        return roots

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


def _build_cmd(
    query: str,
    search_mode: str = "contents",
    environ: Mapping[str, str] | None = None,
) -> tuple[list[str], list[Path], int]:
    config = load_config(Path(__file__).with_name("config.json"), environ=environ)

    resolved_rg = str(config.RIPGREP_BINARY or "").strip()
    if not resolved_rg:
        raise RuntimeError(
            "ripgrep binary not found. Install with: apt install ripgrep",
        )

    timeout = int(config.RIPGREP_TIMEOUT)
    ripgrep_args = [arg for arg in config.RIPGREP_ARGS if arg not in {"--follow", "-L"}]
    ripgrep_args_extra = [
        arg for arg in config.RIPGREP_ARGS_EXTRA if arg not in {"--follow", "-L"}
    ]

    search_roots = _get_search_roots(environ)
    if not search_roots:
        return [], [], timeout

    exclude_globs = DEFAULT_CONTENT_EXCLUDES if search_mode != "deep" else DEEP_EXCLUDES

    cmd = [
        resolved_rg,
        *ripgrep_args,
        *ripgrep_args_extra,
        "--files-with-matches",
        "--line-buffered",
        "--no-messages",
        *(("--glob", f"!{glob}") for glob in exclude_globs),
        "--regexp",
        query,
        *(str(root) for root in search_roots),
    ]
    return (
        [
            part
            for item in cmd
            for part in (item if isinstance(item, tuple) else (item,))
        ],
        search_roots,
        timeout,
    )


def iter_search(
    query: str,
    search_mode: str = "contents",
    *,
    environ: Mapping[str, str] | None = None,
):
    """Yield matching snapshot IDs as ripgrep prints matched files."""
    flattened_cmd, search_roots, timeout = _build_cmd(query, search_mode, environ)
    if not flattened_cmd:
        return
    proc = subprocess.Popen(
        flattened_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    timer = threading.Timer(timeout, proc.kill)
    seen = set()
    timer.start()
    try:
        for line in proc.stdout or ():
            path = Path(line.strip())
            snapshot_id = _extract_snapshot_id(path, search_roots)
            if snapshot_id and snapshot_id not in seen:
                seen.add(snapshot_id)
                yield snapshot_id
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def search(
    query: str,
    search_mode: str = "contents",
    *,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Search for snapshots using ripgrep."""
    try:
        return list(iter_search(query, search_mode=search_mode, environ=environ))
    except Exception:
        return []


def _scan_candidate(candidate, terms, budget, rg_binary):
    found = Evidence(candidate.id)
    if candidate.path is None:
        return found
    incomplete = set()
    observed = set()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def scan_file(parent, name, active):
        if not budget.files or not budget.bytes:
            incomplete.update(active)
            return
        budget.files -= 1
        # Use openat on an already opened directory and reject links/special
        # files. An archived page may have created hostile names or symlinks.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb", buffering=0) as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                incomplete.update(active)
                return
            observed.update(active)
            count = min(info.st_size, budget.bytes, budget.per_file)
            data = stream.read(count)
            budget.bytes -= len(data)
            current = os.fstat(fd)
            if len(data) != info.st_size or (current.st_size, current.st_mtime_ns) != (
                info.st_size,
                info.st_mtime_ns,
            ):
                incomplete.update(active)
        if len(active) == 1 and not any(
            c in active[0].value for c in ("\n", "\r", "\x00")
        ):
            term = active[0]
            # Never hand rg a directory: only the admitted bytes may be read.
            result = subprocess.run(
                [
                    rg_binary,
                    "--no-config",
                    "--threads=1",
                    "--quiet",
                    "--text",
                    "--encoding=none",
                    "--case-sensitive" if term.case_sensitive else "--ignore-case",
                    "--fixed-strings",
                    "-e",
                    term.value,
                    "-",
                ],
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.001, budget.remaining()),
                check=False,
            )
            if result.returncode == 0:
                found.matches[term.key] = True
            elif result.returncode != 1:
                incomplete.add(term)
        else:
            # Evaluate overlapping operands independently on the same bytes.
            # rg -e A -e B identifies a hit, but cannot prove both operands.
            text = data.decode("utf-8", "surrogateescape")
            for term in active:
                if term.pattern.search(text):
                    found.matches[term.key] = True

    def visit(parent):
        with os.scandir(parent) as entries:
            for entry in entries:
                if len(found.matches) == len(terms):
                    return
                if not budget.entries or not budget.remaining():
                    incomplete.update(terms)
                    return
                budget.entries -= 1
                if entry.name.startswith("."):
                    continue
                active = [term for term in terms if term.key not in found.matches]
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = os.open(entry.name, directory_flags, dir_fd=parent)
                        try:
                            visit(child)
                        finally:
                            os.close(child)
                    elif entry.is_file(follow_symlinks=False):
                        active = [
                            term
                            for term in active
                            if not any(
                                fnmatchcase(entry.name, glob)
                                for glob in (
                                    DEEP_EXCLUDES
                                    if term.mode == "deep"
                                    else DEFAULT_CONTENT_EXCLUDES
                                )
                            )
                        ]
                        if active:
                            scan_file(parent, entry.name, active)
                except (OSError, subprocess.TimeoutExpired):
                    incomplete.update(active)

    try:
        archive_root = candidate.archive_root
        parts = ()
        if archive_root is not None:
            if not candidate.path.is_relative_to(archive_root):
                return found
            parts = candidate.path.relative_to(archive_root).parts
            if not parts or ".." in parts:
                return found
        root = os.open(archive_root or candidate.path, directory_flags)
        try:
            # Resolving a path before opening it leaves a race if an ancestor
            # is renamed or replaced with a symlink. Walk from the trusted
            # archive directory, pinning each opened component by descriptor.
            for name in parts:
                child = os.open(name, directory_flags, dir_fd=root)
                os.close(root)
                root = child
            visit(root)
        finally:
            os.close(root)
    except OSError:
        incomplete.update(terms)
    for term in terms:
        if term in observed and term not in incomplete:
            found.matches.setdefault(term.key, False)
    return found


def search_candidates(
    candidates: list[Candidate],
    terms: list[SearchTerm],
    budget: SearchBudget,
):
    config = load_config(
        Path(__file__).with_name("config.json"),
        hydrate_binaries=False,
    )
    for candidate in candidates:
        if not budget.remaining():
            return
        yield _scan_candidate(
            candidate,
            terms,
            budget,
            str(config.RIPGREP_BINARY or "rg"),
        )


def flush(snapshot_ids: Iterable[str]) -> None:
    """No-op for ripgrep - it searches files directly."""
    pass


if __name__ == "__main__":
    raise SystemExit(
        run_search_command(iter_search, flush, search_candidates=search_candidates),
    )
