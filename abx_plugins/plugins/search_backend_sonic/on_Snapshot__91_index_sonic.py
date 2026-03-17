#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "sonic-client",
# ]
# ///
"""
Sonic search backend - indexes snapshot content in Sonic server.

This hook runs after all extractors and indexes text content in Sonic.
Only runs if SEARCH_BACKEND_ENGINE=sonic.

Usage: on_Snapshot__91_index_sonic.py --url=<url> --snapshot-id=<uuid>

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
import json
import os
import re
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import get_env, get_env_bool, get_env_int


# Extractor metadata
PLUGIN_NAME = "index_sonic"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
# Text file patterns to index
INDEXABLE_FILES = [
    ("readability", "content.txt"),
    ("readability", "content.html"),
    ("mercury", "content.txt"),
    ("mercury", "content.html"),
    ("opendataloader", "content.txt"),
    ("opendataloader", "content.md"),
    ("htmltotext", "output.txt"),
    ("singlefile", "singlefile.html"),
    ("dom", "output.html"),
    ("wget", "**/*.html"),
    ("wget", "**/*.htm"),
    ("title", "title.txt"),
]


def get_text_size_kb(texts: list[str]) -> int:
    total_bytes = sum(len(text.encode("utf-8")) for text in texts)
    return (total_bytes + 1023) // 1024 if total_bytes > 0 else 0


def strip_html_tags(html: str) -> str:
    """Remove HTML tags, keeping text content."""
    html = re.sub(
        r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
    )
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    html = html.replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"')
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def find_indexable_content() -> list[tuple[str, str]]:
    """Find text content to index from extractor outputs."""
    results = []
    snap_dir = SNAP_DIR

    for extractor, file_pattern in INDEXABLE_FILES:
        plugin_dir = snap_dir / extractor
        if not plugin_dir.exists():
            continue

        if "*" in file_pattern:
            matches = list(plugin_dir.glob(file_pattern))
        else:
            match = plugin_dir / file_pattern
            matches = [match] if match.exists() else []

        for match in matches:
            if match.is_file() and match.stat().st_size > 0:
                try:
                    content = match.read_text(encoding="utf-8", errors="ignore")
                    if content.strip():
                        if match.suffix in (".html", ".htm"):
                            content = strip_html_tags(content)
                        rel_path = match.relative_to(plugin_dir)
                        results.append((f"{extractor}/{rel_path.as_posix()}", content))
                except Exception:
                    continue

    return results


def get_sonic_config() -> dict:
    """Get Sonic connection configuration."""
    return {
        "host": get_env("SEARCH_BACKEND_HOST_NAME", "127.0.0.1"),
        "port": get_env_int("SEARCH_BACKEND_PORT", 1491),
        "password": get_env("SEARCH_BACKEND_PASSWORD", "SecretPassword"),
        "collection": get_env("SONIC_COLLECTION", "archivebox"),
        "bucket": get_env("SONIC_BUCKET", "snapshots"),
    }


def index_in_sonic(snapshot_id: str, texts: list[str]) -> None:
    """Index texts in Sonic."""
    try:
        sonic = import_module("sonic")
    except ModuleNotFoundError:
        raise RuntimeError("sonic-client not installed. Run: pip install sonic-client")
    ingest_client: Any = sonic.IngestClient

    config = get_sonic_config()

    with ingest_client(config["host"], config["port"], config["password"]) as ingest:
        # Flush existing content
        try:
            ingest.flush_object(config["collection"], config["bucket"], snapshot_id)
        except Exception:
            pass

        # Index new content in chunks (Sonic has size limits)
        content = " ".join(texts)
        chunk_size = 10000
        for i in range(0, len(content), chunk_size):
            chunk = content[i : i + chunk_size]
            ingest.push(config["collection"], config["bucket"], snapshot_id, chunk)


def main() -> None:
    """Index snapshot content in Sonic."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="URL that was archived")
    parser.add_argument("--snapshot-id", required=True, help="Snapshot UUID")
    args = parser.parse_args()
    url = args.url
    snapshot_id = args.snapshot_id

    status = "failed"
    error = ""
    output_str = ""
    text_size_kb = 0

    try:
        # Check if this backend is enabled (permanent skips - don't retry)
        backend = get_env("SEARCH_BACKEND_ENGINE", "sqlite")
        if backend != "sonic":
            print(
                f"Skipping Sonic indexing (SEARCH_BACKEND_ENGINE={backend})",
                file=sys.stderr,
            )
            status = "skipped"
            output_str = f"SEARCH_BACKEND_ENGINE={backend}"
        elif not get_env_bool("USE_INDEXING_BACKEND", True):
            print("Skipping indexing (USE_INDEXING_BACKEND=False)", file=sys.stderr)
            status = "skipped"
            output_str = "USE_INDEXING_BACKEND=False"
        else:
            contents = find_indexable_content()

            if not contents:
                status = "noresults"
                output_str = "No indexable content"
                print("No indexable content found", file=sys.stderr)
            else:
                texts = [content for _, content in contents]
                text_size_kb = get_text_size_kb(texts)
                index_in_sonic(snapshot_id, texts)
                status = "succeeded"
                output_str = f"{text_size_kb}kb text indexed"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)

    if status in ("succeeded", "skipped", "noresults"):
        print(
            json.dumps(
                {
                    "type": "ArchiveResult",
                    "status": status,
                    "output_str": output_str,
                }
            )
        )

    sys.exit(0 if status in ("succeeded", "skipped", "noresults") else 1)


if __name__ == "__main__":
    main()
