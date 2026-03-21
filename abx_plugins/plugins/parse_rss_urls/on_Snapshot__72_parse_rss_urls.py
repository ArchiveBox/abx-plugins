#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "feedparser",
#   "rich-click",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
"""
Parse RSS/Atom feeds and extract URLs.

This is a standalone extractor that can run without ArchiveBox.
It reads feed content from a URL and extracts article URLs.

Usage: ./on_Snapshot__72_parse_rss_urls.py --url=<url>
Output: Appends discovered URLs to SNAP_DIR/parse_rss_urls/urls.jsonl

Examples:
    ./on_Snapshot__72_parse_rss_urls.py --url=https://example.com/feed.rss
    ./on_Snapshot__72_parse_rss_urls.py --url=file:///path/to/feed.xml
"""

import json
import os
import sys
from importlib import import_module
from pathlib import Path
from datetime import datetime, timezone
from html import unescape
from time import mktime
from typing import Any
from urllib.parse import urlparse

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    write_text_atomic,
)

import rich_click as click

PLUGIN_NAME = "parse_rss_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
URLS_FILE = Path("urls.jsonl")
NORESULTS_OUTPUT = "0 URLs parsed"

feedparser: Any | None
try:
    feedparser = import_module("feedparser")
except ModuleNotFoundError:
    feedparser = None


def fetch_content(url: str) -> str:
    """Fetch content from a URL (supports file:// and https://)."""
    parsed = urlparse(url)

    if parsed.scheme == "file":
        file_path = parsed.path
        with open(file_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    else:
        timeout = int(os.environ.get("TIMEOUT", "60"))
        user_agent = os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (compatible; ArchiveBox/1.0)",
        )

        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")


def emit_result(status: str, output_str: str) -> None:
    """Emit final ArchiveResult JSONL plus a short stderr summary."""
    emit_archive_result_record(status, output_str)
    if output_str:
        click.echo(output_str, err=True)


def persist_records(records: list[dict]) -> tuple[str, str]:
    """Write extracted URLs when present, otherwise clear stale output after success."""
    if records:
        write_text_atomic(
            URLS_FILE,
            "\n".join(json.dumps(record) for record in records) + "\n",
        )
        return "succeeded", f"{len(records)} URLs parsed"

    URLS_FILE.unlink(missing_ok=True)
    return "noresults", NORESULTS_OUTPUT


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="RSS/Atom feed URL to parse")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    depth: int = 0,
):
    """Parse RSS/Atom feed and extract article URLs."""
    env_depth = os.environ.get("SNAPSHOT_DEPTH")
    if env_depth is not None:
        try:
            depth = int(env_depth)
        except Exception:
            pass
    if feedparser is None:
        emit_result("failed", "feedparser library not installed")
        sys.exit(1)

    try:
        content = fetch_content(url)
    except Exception as e:
        emit_result("failed", f"Failed to fetch {url}: {e}")
        sys.exit(1)

    # Parse the feed
    feed = feedparser.parse(content)

    urls_found = []
    all_tags = set()

    if not feed.entries:
        # No entries - will emit skipped status at end
        pass
    else:
        for item in feed.entries:
            item_url = getattr(item, "link", None)
            if not item_url:
                continue

            title = getattr(item, "title", None)

            # Get bookmarked_at (published/updated date as ISO 8601)
            bookmarked_at = None
            if hasattr(item, "published_parsed") and item.published_parsed:
                bookmarked_at = datetime.fromtimestamp(
                    mktime(item.published_parsed),
                    tz=timezone.utc,
                ).isoformat()
            elif hasattr(item, "updated_parsed") and item.updated_parsed:
                bookmarked_at = datetime.fromtimestamp(
                    mktime(item.updated_parsed),
                    tz=timezone.utc,
                ).isoformat()

            # Get tags
            tags = ""
            if hasattr(item, "tags") and item.tags:
                try:
                    tags = ",".join(
                        tag.term for tag in item.tags if hasattr(tag, "term")
                    )
                    # Collect unique tags
                    for tag in tags.split(","):
                        tag = tag.strip()
                        if tag:
                            all_tags.add(tag)
                except (AttributeError, TypeError):
                    pass

            entry = {
                "type": "Snapshot",
                "url": unescape(item_url),
                "plugin": PLUGIN_NAME,
                "depth": depth + 1,
            }
            if title:
                entry["title"] = unescape(title)
            if bookmarked_at:
                entry["bookmarked_at"] = bookmarked_at
            if tags:
                entry["tags"] = tags
            urls_found.append(entry)

    # Emit Tag records first (to stdout as JSONL)
    for tag_name in sorted(all_tags):
        print(
            json.dumps(
                {
                    "type": "Tag",
                    "name": tag_name,
                },
            ),
        )

    # Emit Snapshot records (to stdout as JSONL)
    for entry in urls_found:
        emit_snapshot_record(entry)

    # Emit ArchiveResult record to mark completion
    status, output_str = persist_records(urls_found)
    emit_result(status, output_str)
    sys.exit(0)


if __name__ == "__main__":
    main()
