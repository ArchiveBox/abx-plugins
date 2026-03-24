#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "abx-plugins",
# ]
# ///
"""
SQLite FTS5 search backend - indexes snapshot content for full-text search.

This hook runs after all extractors and indexes text content in SQLite FTS5.
Only runs if SEARCH_BACKEND_ENGINE=sqlite.

Usage: on_Snapshot__90_index_sqlite.py --url=<url>

Environment variables:
    SEARCH_BACKEND_ENGINE: Must be 'sqlite' for this hook to run
    USE_INDEXING_BACKEND: Enable search indexing (default: true)
    SQLITEFTS_DB: Database filename (default: search.sqlite3)
    FTS_TOKENIZERS: FTS5 tokenizer config (default: porter unicode61 remove_diacritics 2)
    SNAP_DIR: Snapshot directory (default: cwd)
"""

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    get_extra_context,
    load_config,
)


# Extractor metadata
PLUGIN_NAME = "index_sqlite"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
DATA_DIR = Path(CONFIG.DATA_DIR or ".").resolve()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
# File extensions eligible for full-text indexing
INDEXABLE_EXTENSIONS = {".txt", ".md", ".html", ".htm"}
# Directories to skip (search backends themselves, executor artifacts, non-text plugins)
SKIP_DIRS = {"search_backend_sqlite", "search_backend_sonic", "search_backend_ripgrep"}
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
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    html = html.replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"')
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def should_skip_plugin_dir(plugin_dir: Path) -> bool:
    """Skip search outputs and test/runtime environment trees inside SNAP_DIR."""
    name = plugin_dir.name
    return name in SKIP_DIRS or name.startswith(".") or name.endswith("_env")


def should_skip_source_path(source_path: Path, plugin_dir: Path) -> bool:
    """Ignore cache/build artifacts nested under otherwise indexable directories."""
    for part in source_path.relative_to(plugin_dir).parts[:-1]:
        if (
            part.startswith(".")
            or part in SKIP_NESTED_DIRS
            or part.endswith(SKIP_NESTED_SUFFIXES)
        ):
            return True
    return False


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
        if not plugin_dir.is_dir():
            continue

        extractor = plugin_dir.name
        if should_skip_plugin_dir(plugin_dir):
            continue

        for ext in INDEXABLE_EXTENSIONS:
            for match in plugin_dir.rglob(f"*{ext}"):
                if not match.is_file():
                    continue
                if should_skip_source_path(match, plugin_dir):
                    continue
                if any(match.name.endswith(s) for s in EXECUTOR_ARTIFACT_SUFFIXES):
                    continue
                if match.stat().st_size == 0:
                    continue

                try:
                    content = match.read_text(encoding="utf-8", errors="ignore")
                    if not content.strip():
                        continue
                    if match.suffix.lower() in (".html", ".htm"):
                        content = strip_html_tags(content)
                    if not content.strip():
                        continue
                    rel_path = match.relative_to(plugin_dir)
                    results.append(
                        (f"{extractor}/{rel_path.as_posix()}", content, match),
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

    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index
        USING fts5(snapshot_id, url, title, content, tokenize='{tokenizers}')
    """)


def index_in_sqlite(snapshot_id: str, url: str, title: str, texts: list[str]) -> None:
    """Index texts in SQLite FTS5."""
    db_path = get_db_path()
    tokenizers = CONFIG.SEARCH_BACKEND_SQLITE_TOKENIZERS
    conn = sqlite3.connect(str(db_path))

    try:
        ensure_index_schema(conn, tokenizers)

        # Remove existing entries
        conn.execute("DELETE FROM search_index WHERE snapshot_id = ?", (snapshot_id,))

        # Insert new content
        content = "\n\n".join(texts)
        conn.execute(
            "INSERT INTO search_index (snapshot_id, url, title, content) VALUES (?, ?, ?, ?)",
            (snapshot_id, url, title, content),
        )
        conn.commit()
    finally:
        conn.close()


def get_snapshot_id_from_context() -> str:
    extra_context = get_extra_context()
    return str(
        extra_context.get("snapshot_id") or extra_context.get("id") or "",
    ).strip()


def main() -> None:
    """Index snapshot content in SQLite FTS5."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="URL that was archived")
    args, _unknown_args = parser.parse_known_args()
    url = args.url

    status = "failed"
    error = ""
    output_str = ""
    text_size_kb = 0

    try:
        if CONFIG.ABX_RUNTIME != "archivebox":
            print("Skipping SQLite indexing (ABX_RUNTIME!=archivebox)", file=sys.stderr)
            status = "skipped"
            output_str = f"ABX_RUNTIME={CONFIG.ABX_RUNTIME}"
        # Check if this backend is enabled (permanent skips - don't retry)
        elif CONFIG.SEARCH_BACKEND_ENGINE != "sqlite":
            print(
                f"Skipping SQLite indexing (SEARCH_BACKEND_ENGINE={CONFIG.SEARCH_BACKEND_ENGINE})",
                file=sys.stderr,
            )
            status = "skipped"
            output_str = f"SEARCH_BACKEND_ENGINE={CONFIG.SEARCH_BACKEND_ENGINE}"
        elif not bool(CONFIG.USE_INDEXING_BACKEND):
            print("Skipping indexing (USE_INDEXING_BACKEND=False)", file=sys.stderr)
            status = "skipped"
            output_str = "USE_INDEXING_BACKEND=False"
        else:
            snapshot_id = get_snapshot_id_from_context()
            if not snapshot_id:
                raise RuntimeError("missing snapshot_id in extra context")

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
