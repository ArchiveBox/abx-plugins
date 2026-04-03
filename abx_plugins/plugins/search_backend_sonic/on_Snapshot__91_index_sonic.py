#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "jambo",
#     "sonic-client",
#     "abx-plugins",
# ]
# ///
"""
Sonic search backend - indexes snapshot content in Sonic server.

This hook runs after all extractors and indexes text content in Sonic.
Only runs if SEARCH_BACKEND_ENGINE=sonic.

Usage: on_Snapshot__91_index_sonic.py --url=<url>

Environment variables:
    SEARCH_BACKEND_ENGINE: Must be 'sonic' for this hook to run
    USE_INDEXING_BACKEND: Enable search indexing (default: true)
    SEARCH_BACKEND_HOST_NAME: Sonic server host (default: 127.0.0.1)
    SEARCH_BACKEND_PORT: Sonic server port (default: 1491)
    SEARCH_BACKEND_PASSWORD: Sonic server password (default: SecretPassword)
    SONIC_COLLECTION: Collection name (default: archivebox)
    SONIC_BUCKET: Bucket name (default: snapshots)
"""

import argparse
import os
import re
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    get_extra_context,
    load_config,
)


# Extractor metadata
PLUGIN_NAME = "index_sonic"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
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


def find_indexable_content() -> list[tuple[str, str]]:
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
                    results.append((f"{extractor}/{rel_path.as_posix()}", content))
                except Exception:
                    continue

    return results


def index_in_sonic(snapshot_id: str, texts: list[str], config: Any) -> None:
    """Index texts in Sonic."""
    try:
        sonic = import_module("sonic")
    except ModuleNotFoundError:
        raise RuntimeError("sonic-client not installed. Run: pip install sonic-client")
    ingest_client: Any = sonic.IngestClient

    with ingest_client(
        config.SEARCH_BACKEND_SONIC_HOST_NAME,
        config.SEARCH_BACKEND_SONIC_PORT,
        config.SEARCH_BACKEND_SONIC_PASSWORD,
    ) as ingest:
        # Flush existing content
        try:
            ingest.flush_object(
                config.SEARCH_BACKEND_SONIC_COLLECTION,
                config.SEARCH_BACKEND_SONIC_BUCKET,
                snapshot_id,
            )
        except Exception:
            pass

        # Index new content in chunks (Sonic has size limits)
        content = " ".join(texts)
        chunk_size = 10000
        for i in range(0, len(content), chunk_size):
            chunk = content[i : i + chunk_size]
            ingest.push(
                config.SEARCH_BACKEND_SONIC_COLLECTION,
                config.SEARCH_BACKEND_SONIC_BUCKET,
                snapshot_id,
                chunk,
            )


def get_snapshot_id_from_context() -> str:
    extra_context = get_extra_context()
    return str(
        extra_context.get("snapshot_id") or extra_context.get("id") or "",
    ).strip()


def main() -> None:
    """Index snapshot content in Sonic."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="URL that was archived")
    args, _unknown_args = parser.parse_known_args()

    status = "failed"
    error = ""
    output_str = ""
    text_size_kb = 0

    try:
        config = load_config()

        if config.ABX_RUNTIME != "archivebox":
            print("Skipping Sonic indexing (ABX_RUNTIME!=archivebox)", file=sys.stderr)
            status = "skipped"
            output_str = f"ABX_RUNTIME={config.ABX_RUNTIME}"
        # Check if this backend is enabled (permanent skips - don't retry)
        elif config.SEARCH_BACKEND_ENGINE != "sonic":
            print(
                f"Skipping Sonic indexing (SEARCH_BACKEND_ENGINE={config.SEARCH_BACKEND_ENGINE})",
                file=sys.stderr,
            )
            status = "skipped"
            output_str = f"SEARCH_BACKEND_ENGINE={config.SEARCH_BACKEND_ENGINE}"
        elif not bool(config.USE_INDEXING_BACKEND):
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
                texts = [content for _, content in contents]
                text_size_kb = get_text_size_kb(texts)
                index_in_sonic(snapshot_id, texts, config)
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
