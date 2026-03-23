#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
#
# Parse Netscape bookmark HTML files and extract URLs.
#
# This is a standalone extractor that can run without ArchiveBox.
# It reads Netscape-format bookmark exports (produced by all major browsers).
#
# Usage:
#     ./on_Snapshot__73_parse_netscape_urls.py --url=<url>
# Output: Appends discovered URLs to SNAP_DIR/parse_netscape_urls/urls.jsonl
#
# Examples:
#     ./on_Snapshot__73_parse_netscape_urls.py --url=file:///path/to/bookmarks.html

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    load_config,
    write_text_atomic,
)

import rich_click as click

PLUGIN_NAME = "parse_netscape_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
URLS_FILE = Path("urls.jsonl")
NORESULTS_OUTPUT = "0 URLs parsed"

# Constants for timestamp epoch detection
UNIX_EPOCH = 0  # 1970-01-01 00:00:00 UTC
MAC_COCOA_EPOCH = 978307200  # 2001-01-01 00:00:00 UTC (Mac/Cocoa/NSDate epoch)

# Reasonable date range for bookmarks (to detect correct epoch/unit)
MIN_REASONABLE_YEAR = 1995  # Netscape Navigator era
MAX_REASONABLE_YEAR = 2035  # Far enough in future

# Regex pattern for Netscape bookmark format
# Example: <DT><A HREF="https://example.com/?q=1+2" ADD_DATE="1497562974" TAGS="tag1,tag2">example title</A>
# Make ADD_DATE optional and allow negative numbers
NETSCAPE_PATTERN = re.compile(
    r'<a\s+href="([^"]+)"(?:\s+add_date="([^"]*)")?(?:\s+[^>]*?tags="([^"]*)")?[^>]*>([^<]+)</a>',
    re.UNICODE | re.IGNORECASE,
)


def parse_timestamp(timestamp_str: str) -> datetime | None:
    """
    Intelligently parse bookmark timestamp with auto-detection of format and epoch.

    Browsers use different timestamp formats:
    - Firefox: Unix epoch (1970) in seconds (10 digits): 1609459200
    - Safari: Mac/Cocoa epoch (2001) in seconds (9-10 digits): 631152000
    - Chrome: Unix epoch in microseconds (16 digits): 1609459200000000
    - Others: Unix epoch in milliseconds (13 digits): 1609459200000

    Strategy:
    1. Try parsing with different epoch + unit combinations
    2. Pick the one that yields a reasonable date (1995-2035)
    3. Prioritize more common formats (Unix seconds, then Mac seconds, etc.)
    """
    if not timestamp_str or timestamp_str == "":
        return None

    try:
        timestamp_num = float(timestamp_str)
    except (ValueError, TypeError):
        return None

    # Detect sign and work with absolute value
    abs_timestamp = abs(timestamp_num)

    # Determine number of digits to guess the unit
    if abs_timestamp == 0:
        num_digits = 1
    else:
        num_digits = len(str(int(abs_timestamp)))

    # Try different interpretations in order of likelihood
    candidates = []

    # Unix epoch seconds (10-11 digits) - Most common: Firefox, Chrome HTML export
    if 9 <= num_digits <= 11:
        try:
            dt = datetime.fromtimestamp(timestamp_num, tz=timezone.utc)
            if MIN_REASONABLE_YEAR <= dt.year <= MAX_REASONABLE_YEAR:
                candidates.append((dt, "unix_seconds", 100))  # Highest priority
        except (ValueError, OSError, OverflowError):
            pass

    # Mac/Cocoa epoch seconds (9-10 digits) - Safari
    # Only consider if Unix seconds didn't work or gave unreasonable date
    if 8 <= num_digits <= 11:
        try:
            dt = datetime.fromtimestamp(
                timestamp_num + MAC_COCOA_EPOCH,
                tz=timezone.utc,
            )
            if MIN_REASONABLE_YEAR <= dt.year <= MAX_REASONABLE_YEAR:
                candidates.append((dt, "mac_seconds", 90))
        except (ValueError, OSError, OverflowError):
            pass

    # Unix epoch milliseconds (13 digits) - JavaScript exports
    if 12 <= num_digits <= 14:
        try:
            dt = datetime.fromtimestamp(timestamp_num / 1000, tz=timezone.utc)
            if MIN_REASONABLE_YEAR <= dt.year <= MAX_REASONABLE_YEAR:
                candidates.append((dt, "unix_milliseconds", 95))
        except (ValueError, OSError, OverflowError):
            pass

    # Mac/Cocoa epoch milliseconds (12-13 digits) - Rare
    if 11 <= num_digits <= 14:
        try:
            dt = datetime.fromtimestamp(
                (timestamp_num / 1000) + MAC_COCOA_EPOCH,
                tz=timezone.utc,
            )
            if MIN_REASONABLE_YEAR <= dt.year <= MAX_REASONABLE_YEAR:
                candidates.append((dt, "mac_milliseconds", 85))
        except (ValueError, OSError, OverflowError):
            pass

    # Unix epoch microseconds (16-17 digits) - Chrome WebKit timestamps
    if 15 <= num_digits <= 18:
        try:
            dt = datetime.fromtimestamp(timestamp_num / 1_000_000, tz=timezone.utc)
            if MIN_REASONABLE_YEAR <= dt.year <= MAX_REASONABLE_YEAR:
                candidates.append((dt, "unix_microseconds", 98))
        except (ValueError, OSError, OverflowError):
            pass

    # Mac/Cocoa epoch microseconds (15-16 digits) - Very rare
    if 14 <= num_digits <= 18:
        try:
            dt = datetime.fromtimestamp(
                (timestamp_num / 1_000_000) + MAC_COCOA_EPOCH,
                tz=timezone.utc,
            )
            if MIN_REASONABLE_YEAR <= dt.year <= MAX_REASONABLE_YEAR:
                candidates.append((dt, "mac_microseconds", 80))
        except (ValueError, OSError, OverflowError):
            pass

    # If no candidates found, return None
    if not candidates:
        return None

    # Sort by priority (highest first) and return best match
    candidates.sort(key=lambda x: x[2], reverse=True)
    best_dt, best_format, _ = candidates[0]

    return best_dt


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


def normalize_bookmark_url(bookmark_url: str, root_url: str) -> str:
    """Resolve relative bookmark URLs against the source page URL."""
    cleaned = sanitize_extracted_url(bookmark_url)
    if not cleaned:
        return cleaned
    return urljoin(root_url, cleaned)


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
@click.option("--url", required=True, help="Netscape bookmark file URL to parse")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    depth: int = 0,
):
    """Parse Netscape bookmark HTML and extract URLs."""
    if CONFIG.SNAPSHOT_DEPTH is not None:
        depth = CONFIG.SNAPSHOT_DEPTH
    try:
        content = fetch_content(url)
    except Exception as e:
        emit_result("failed", f"Failed to fetch {url}: {e}")
        sys.exit(1)

    urls_found = []
    all_tags = set()

    for line in content.splitlines():
        match = NETSCAPE_PATTERN.search(line)
        if match:
            bookmark_url = match.group(1)
            timestamp_str = match.group(2)
            tags_str = match.group(3) or ""
            title = match.group(4).strip()
            resolved_url = normalize_bookmark_url(bookmark_url, url)

            entry = {
                "type": "Snapshot",
                "url": resolved_url,
                "plugin": PLUGIN_NAME,
                "depth": depth + 1,
            }
            if title:
                entry["title"] = unescape(title)
            if tags_str:
                entry["tags"] = tags_str
                # Collect unique tags
                for tag in tags_str.split(","):
                    tag = tag.strip()
                    if tag:
                        all_tags.add(tag)

            # Parse timestamp with intelligent format detection
            if timestamp_str:
                dt = parse_timestamp(timestamp_str)
                if dt:
                    entry["bookmarked_at"] = dt.isoformat()

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
