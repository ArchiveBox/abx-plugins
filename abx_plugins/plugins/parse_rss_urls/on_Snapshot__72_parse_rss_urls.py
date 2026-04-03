#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "feedparser",
#   "rich-click",
#   "abx-plugins",
# ]
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

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    emit_tag_record,
    get_extra_context,
    load_config,
    write_text_atomic,
)

import rich_click as click

PLUGIN_NAME = "parse_rss_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
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
        timeout = CONFIG.TIMEOUT
        user_agent = CONFIG.USER_AGENT

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
    extra_context = get_extra_context()
    if "snapshot_depth" in extra_context:
        depth = int(extra_context["snapshot_depth"])
    if feedparser is None:
        emit_result("failed", "feedparser library not installed")
        sys.exit(1)

    print("parsing 1 files for urls...")
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
            item_url = item["link"] if "link" in item else None
            if not item_url:
                continue
            item_url = sanitize_extracted_url(item_url)
            if not item_url:
                continue

            title = item["title"] if "title" in item else None

            # Get bookmarked_at (published/updated date as ISO 8601)
            bookmarked_at = None
            if "published_parsed" in item and item.published_parsed:
                bookmarked_at = datetime.fromtimestamp(
                    mktime(item.published_parsed),
                    tz=timezone.utc,
                ).isoformat()
            elif "updated_parsed" in item and item.updated_parsed:
                bookmarked_at = datetime.fromtimestamp(
                    mktime(item.updated_parsed),
                    tz=timezone.utc,
                ).isoformat()

            # Get tags
            tags = ""
            if "tags" in item and item.tags:
                try:
                    tags = ",".join(tag["term"] for tag in item.tags if "term" in tag)
                    # Collect unique tags
                    for tag in tags.split(","):
                        tag = tag.strip()
                        if tag:
                            all_tags.add(tag)
                except (AttributeError, TypeError):
                    pass

            entry = {
                "type": "Snapshot",
                "url": item_url,
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
        emit_tag_record(tag_name)

    # Emit Snapshot records (to stdout as JSONL)
    for entry in urls_found:
        emit_snapshot_record(entry)

    # Emit ArchiveResult record to mark completion
    status, output_str = persist_records(urls_found)
    print(output_str)
    emit_result(status, output_str)
    sys.exit(0)


if __name__ == "__main__":
    main()
