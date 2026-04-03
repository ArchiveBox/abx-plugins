#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "jambo",
#     "rich-click",
#     "abx-plugins",
# ]
# ///
"""
Parse JSONL bookmark files and extract URLs.

This is a standalone extractor that can run without ArchiveBox.
It reads JSONL-format bookmark exports (one JSON object per line).

Usage: ./on_Snapshot__74_parse_jsonl_urls.py --url=<url>
Output: Appends discovered URLs to SNAP_DIR/parse_jsonl_urls/urls.jsonl

Expected JSONL format (one object per line):
    {"url": "https://example.com", "title": "Example", "tags": "tag1,tag2"}
    {"href": "https://other.com", "description": "Other Site"}

Supports various field names for URL, title, timestamp, and tags.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime
from html import unescape
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

PLUGIN_NAME = "parse_jsonl_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
URLS_FILE = Path("urls.jsonl")
NORESULTS_OUTPUT = "0 URLs parsed"


def parse_bookmarked_at(link: dict) -> str | None:
    """Parse timestamp from various JSON formats, return ISO 8601."""
    from datetime import timezone

    def json_date(s: str) -> datetime:
        # Try ISO 8601 format
        return datetime.strptime(s.split(",", 1)[0], "%Y-%m-%dT%H:%M:%S%z")

    def to_iso(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    try:
        if link.get("bookmarked_at"):
            # Already in our format, pass through
            return link["bookmarked_at"]
        elif link.get("timestamp"):
            # Chrome/Firefox histories use microseconds
            return to_iso(
                datetime.fromtimestamp(link["timestamp"] / 1000000, tz=timezone.utc),
            )
        elif link.get("time"):
            return to_iso(json_date(link["time"]))
        elif link.get("created_at"):
            return to_iso(json_date(link["created_at"]))
        elif link.get("created"):
            return to_iso(json_date(link["created"]))
        elif link.get("date"):
            return to_iso(json_date(link["date"]))
        elif link.get("bookmarked"):
            return to_iso(json_date(link["bookmarked"]))
        elif link.get("saved"):
            return to_iso(json_date(link["saved"]))
    except (ValueError, TypeError, KeyError):
        pass

    return None


def json_object_to_entry(link: dict) -> dict | None:
    """Convert a JSON bookmark object to a URL entry."""
    # Parse URL (try various field names)
    url = link.get("href") or link.get("url") or link.get("URL")
    if not url:
        return None
    cleaned_url = sanitize_extracted_url(url)
    if not cleaned_url:
        return None

    entry = {
        "type": "Snapshot",
        "url": cleaned_url,
        "plugin": PLUGIN_NAME,
    }

    # Parse title
    title = None
    if link.get("title"):
        title = link["title"].strip()
    elif link.get("description"):
        title = link["description"].replace(" — Readability", "").strip()
    elif link.get("name"):
        title = link["name"].strip()
    if title:
        entry["title"] = unescape(title)

    # Parse bookmarked_at (ISO 8601)
    bookmarked_at = parse_bookmarked_at(link)
    if bookmarked_at:
        entry["bookmarked_at"] = bookmarked_at

    # Parse tags
    tags = link["tags"] if "tags" in link else ""
    if isinstance(tags, list):
        tags = ",".join(tags)
    elif isinstance(tags, str) and "," not in tags and tags:
        # If no comma, assume space-separated
        tags = tags.replace(" ", ",")
    if tags:
        entry["tags"] = unescape(tags)

    return entry


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
@click.option("--url", required=True, help="JSONL file URL to parse")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    depth: int = 0,
):
    """Parse JSONL bookmark file and extract URLs."""
    extra_context = get_extra_context()
    if "snapshot_depth" in extra_context:
        depth = int(extra_context["snapshot_depth"])
    print("parsing 1 files for urls...")
    try:
        content = fetch_content(url)
    except Exception as e:
        emit_result("failed", f"Failed to fetch {url}: {e}")
        sys.exit(1)

    urls_found = []
    all_tags = set()

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            link = json.loads(line)
            entry = json_object_to_entry(link)
            if entry:
                # Add crawl tracking metadata
                entry["depth"] = depth + 1

                # Collect tags
                if entry.get("tags"):
                    for tag in entry["tags"].split(","):
                        tag = tag.strip()
                        if tag:
                            all_tags.add(tag)

                urls_found.append(entry)
        except json.JSONDecodeError:
            # Skip malformed lines
            continue

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
