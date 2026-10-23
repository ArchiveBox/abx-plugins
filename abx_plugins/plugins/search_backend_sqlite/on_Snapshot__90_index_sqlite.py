#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""
SQLite FTS5 search backend - indexes snapshot content for full-text search.

This hook runs after all extractors and indexes text content in SQLite FTS5.
Skipped when SEARCH_BACKEND_SQLITE_ENABLED=False.

Usage: on_Snapshot__90_index_sqlite.py --url=<url>

Environment variables:
    SEARCH_BACKEND_SQLITE_ENABLED: Index into SQLite FTS5 (default: true)
    SQLITEFTS_DB: Database filename (default: search.sqlite3)
    FTS_TOKENIZERS: FTS5 tokenizer config (default: porter unicode61 remove_diacritics 2)
    SNAP_DIR: Snapshot directory (default: cwd)
"""

import sys
import argparse
import os
import stat
import re
import sqlite3
import hashlib
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
)
from abx_plugins.plugins.search_backend_sqlite.search import (
    delete_snapshot,
    ensure_snapshot_lookup,
    load_sqlite_config,
)


# Extractor metadata
PLUGIN_NAME = "index_sqlite"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_sqlite_config()
DATA_DIR = Path(CONFIG.DATA_DIR or ".").resolve()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
# File extensions eligible for full-text indexing
INDEXABLE_EXTENSIONS = {".txt", ".md", ".html", ".htm"}
# Directories to skip (search backends themselves, executor artifacts, non-text plugins)
SKIP_DIRS = {
    "chrome",
    "search_backend_sqlite",
    "search_backend_sonic",
    "search_backend_ripgrep",
}
# Nested runtime/build/cache directories that are never snapshot content
SKIP_NESTED_DIRS = {".cache", ".venv", "__pycache__", "site-packages", "node_modules"}
SKIP_NESTED_SUFFIXES = (".dist-info", ".egg-info")
# Filename suffixes that are executor artifacts, not content
EXECUTOR_ARTIFACT_SUFFIXES = (".stdout.log", ".stderr.log", ".pid", ".sh", ".meta.json")


def get_text_size_kb(texts: list[str]) -> int:
    total_bytes = sum(len(text.encode("utf-8")) for text in texts)
    return (total_bytes + 1023) // 1024 if total_bytes > 0 else 0


def strip_html_tags(html: str) -> str:
    """Remove HTML tags, keeping text content."""
    html = re.sub(
        r"<script[^>]*>.*?</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Quoted `>` characters do not end tags; leaking srcset data URLs here
    # can send megabytes of image bytes to the text index.
    html = re.sub(r"""<(?:[^>"']|"[^"]*"|'[^']*')+>""", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    html = html.replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"')
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def should_skip_plugin_dir(plugin_dir: Path) -> bool:
    """Skip search outputs and test/runtime environment trees inside SNAP_DIR."""
    name = plugin_dir.name
    return name in SKIP_DIRS or name.startswith(".") or name.endswith("_env")


def should_skip_nested_dir(name: str) -> bool:
    """Prune runtime/build artifacts before descending into their directories."""
    return (
        name.startswith(".")
        or name in SKIP_NESTED_DIRS
        or name.endswith(SKIP_NESTED_SUFFIXES)
    )


def find_indexable_content() -> list[tuple[str, str, Path]]:
    """Auto-discover text content to index from all plugin output directories.

    Walks every subdirectory of SNAP_DIR and collects .txt, .md, .html, .htm
    files, stripping HTML tags where needed.  This avoids hardcoding which
    plugins produce indexable output — any plugin that writes text files will
    be picked up automatically.
    """
    results = []
    snap_dir = SNAP_DIR

    if not snap_dir.is_dir():
        return results

    for plugin_dir in sorted(snap_dir.iterdir()):
        extractor = plugin_dir.name
        if should_skip_plugin_dir(plugin_dir):
            continue
        # The boundary is the snapshot, not this plugin: response-file aliases
        # are valid inputs. Private .persona state is excluded even inside it.
        # Validate root aliases before walking, or indexing could copy secrets
        # into a public search database even when replay rejects the source.
        plugin_stat = plugin_dir.lstat()
        resolved_plugin = plugin_dir
        if stat.S_ISLNK(plugin_stat.st_mode):
            resolved_plugin = plugin_dir.resolve()
            if not resolved_plugin.is_relative_to(snap_dir) or any(
                part.casefold() == ".persona"
                for part in resolved_plugin.relative_to(snap_dir).parts
            ):
                continue
            if not resolved_plugin.is_dir():
                continue
        elif not stat.S_ISDIR(plugin_stat.st_mode):
            continue

        # Match rglob's symlink behavior: follow a plugin-root alias and file
        # aliases, but never recurse through nested directory symlinks. The
        # per-file fast path below relies on keeping followlinks=False.
        for directory, dirnames, filenames in os.walk(plugin_dir, followlinks=False):
            dirnames[:] = [
                name for name in dirnames if not should_skip_nested_dir(name)
            ]
            for filename in filenames:
                match = Path(directory) / filename
                if not filename.endswith(tuple(INDEXABLE_EXTENSIONS)):
                    continue
                if any(match.name.endswith(s) for s in EXECUTOR_ARTIFACT_SUFFIXES):
                    continue
                # os.walk does not follow nested directory links. Only root
                # aliases and file links need resolution; ordinary files reuse
                # one lstat for their type and size on slow archive storage.
                # stat would hide the link itself; resolving every normal file
                # would repeat metadata reads for every ancestor directory.
                resolved = resolved_plugin / match.relative_to(plugin_dir)
                try:
                    source_stat = resolved.lstat()
                    if stat.S_ISLNK(source_stat.st_mode):
                        resolved = resolved.resolve()
                        if not resolved.is_relative_to(snap_dir) or any(
                            part.casefold() == ".persona"
                            for part in resolved.relative_to(snap_dir).parts
                        ):
                            continue
                        source_stat = resolved.stat()
                except (OSError, RuntimeError):
                    continue
                if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size == 0:
                    continue

                try:
                    content = resolved.read_text(encoding="utf-8", errors="ignore")
                    if not content.strip():
                        continue
                    if match.suffix.lower() in (".html", ".htm"):
                        content = strip_html_tags(content)
                    if not content.strip():
                        continue
                    rel_path = match.relative_to(plugin_dir)
                    results.append(
                        (f"{extractor}/{rel_path.as_posix()}", content, resolved),
                    )
                except Exception:
                    continue

    return results


def get_db_path() -> Path:
    """Get path to the search index database."""
    return DATA_DIR / CONFIG.SEARCH_BACKEND_SQLITE_DB


def get_snapshot_title(contents: list[tuple[str, str, Path]], url: str) -> str:
    """Extract title from indexed content, falling back to the URL."""
    for source_id, content, _source_path in contents:
        if source_id == "title/title.txt" and content.strip():
            return content.strip()
    return url


def sync_source_symlinks(contents: list[tuple[str, str, Path]]) -> list[Path]:
    """Mirror indexable source files into this plugin output directory via symlinks."""
    for existing in OUTPUT_DIR.iterdir():
        if existing.is_symlink():
            existing.unlink()

    links: list[Path] = []
    for source_id, _content, source_path in contents:
        link_name = source_id.replace("/", "__")
        # Flattening valid nested paths can exceed a filesystem's 255-byte
        # filename limit. Keep ordinary names stable and hash only long ones.
        if len(os.fsencode(link_name)) > 255:
            source_hash = hashlib.sha256(os.fsencode(source_id)).hexdigest()
            link_name = f"source-{source_hash}{source_path.suffix}"
        link_path = OUTPUT_DIR / link_name
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(source_path)
        links.append(link_path)
    return links


def ensure_index_schema(conn: sqlite3.Connection, tokenizers: str) -> None:
    """Ensure the FTS table exists with the expected schema."""
    expected_columns = ["snapshot_id", "url", "title", "content"]
    try:
        rows = conn.execute("PRAGMA table_info(search_index)").fetchall()
    except sqlite3.OperationalError:
        rows = []

    existing_columns = [row[1] for row in rows]
    if existing_columns and existing_columns != expected_columns:
        conn.execute("DROP TABLE IF EXISTS search_index")
        conn.execute("DROP TABLE IF EXISTS search_index_lookup")

    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index
        USING fts5(snapshot_id, url, title, content, tokenize='{tokenizers}')
    """)


def index_in_sqlite(snapshot_id: str, url: str, title: str, texts: list[str]) -> None:
    """Index texts in SQLite FTS5."""
    db_path = get_db_path()
    tokenizers = CONFIG.SEARCH_BACKEND_SQLITE_TOKENIZERS
    content = "\n\n".join(texts)
    conn = sqlite3.connect(str(db_path))

    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_index_schema(conn, tokenizers)
        ensure_snapshot_lookup(conn)

        # Remove existing entries
        delete_snapshot(conn, snapshot_id)

        # Insert new content
        inserted = conn.execute(
            "INSERT INTO search_index (snapshot_id, url, title, content) VALUES (?, ?, ?, ?)",
            (snapshot_id, url, title, content),
        )
        conn.execute(
            "INSERT INTO search_index_lookup(document_rowid,snapshot_id) VALUES (?,?)",
            (inserted.lastrowid, snapshot_id),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    """Index snapshot content in SQLite FTS5."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="URL that was archived")
    parser.add_argument(
        "--snapshot-id",
        default="",
        help="Snapshot identifier to index",
    )
    args, _unknown_args = parser.parse_known_args()
    url = args.url

    status = "failed"
    error = ""
    output_str = ""
    text_size_kb = 0

    try:
        if not CONFIG.SEARCH_BACKEND_SQLITE_ENABLED:
            print(
                "Skipping SQLite indexing (SEARCH_BACKEND_SQLITE_ENABLED=False)",
                file=sys.stderr,
            )
            status = "skipped"
            output_str = "SEARCH_BACKEND_SQLITE_ENABLED=False"
        else:
            print("SQLite indexing started", flush=True)
            snapshot_id = args.snapshot_id
            if not snapshot_id:
                raise RuntimeError("missing --snapshot-id")

            contents = find_indexable_content()

            if not contents:
                status = "noresults"
                output_str = "No indexable content"
                print("No indexable content found", file=sys.stderr)
            else:
                sync_source_symlinks(contents)
                title = get_snapshot_title(contents, url)
                texts = [
                    content
                    for source_id, content, _source_path in contents
                    if source_id != "title/title.txt"
                ]
                text_size_kb = get_text_size_kb(texts)
                index_in_sqlite(snapshot_id, url, title, texts)
                status = "succeeded"
                output_str = f"{text_size_kb}kb text indexed"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)

    if status in ("succeeded", "skipped", "noresults"):
        emit_archive_result_record(status, output_str)

    sys.exit(0 if status in ("succeeded", "skipped", "noresults") else 1)


if __name__ == "__main__":
    main()
