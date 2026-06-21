#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12,<3.14"
# ///
#
# Parse Netscape bookmark HTML files and extract URLs.
#
# This is a standalone extractor that can run without ArchiveBox.
# It reads Netscape-format bookmark exports from SNAP_DIR/staticfile/*.txt or an HTTP URL.
#
# Usage:
#     ./on_Snapshot__73_parse_netscape_urls.py --url=<url>
# Output: Appends discovered URLs to SNAP_DIR/parse_netscape_urls/urls.jsonl
#
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    emit_tag_record,
    get_extra_context,
    iter_staticfile_text_inputs,
    load_config,
    read_file_url_text,
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

# Browser exports are often malformed HTML, so anchors are scanned manually
# instead of requiring a valid DOM parse.
ATTR_PATTERN = re.compile(
    r"""([^\s=<>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""",
    re.UNICODE | re.IGNORECASE | re.DOTALL,
)


def looks_like_netscape_bookmarks(content: str) -> bool:
    """Avoid treating arbitrary HTML pages as Netscape bookmark exports."""
    lowered = content[:200000].lower()
    if "netscape-bookmark-file-1" in lowered:
        return True
    if "<dt" in lowered and "<a" in lowered and "href" in lowered:
        return True
    return "<dl" in lowered and "add_date" in lowered and "href" in lowered


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
    """Fetch content from snapshot source artifacts or an HTTP URL."""
    source_paths = iter_staticfile_text_inputs(SNAP_DIR)
    if source_paths:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace") for path in source_paths
        )
    file_content = read_file_url_text(url)
    if file_content is not None:
        return file_content
    if not url.startswith(("http://", "https://")):
        return ""
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


def parse_bookmark_attrs(attrs: str) -> dict[str, str]:
    """Parse loose HTML attributes from a Netscape bookmark anchor."""
    parsed = {}
    for match in ATTR_PATTERN.finditer(attrs):
        name = match.group(1).lower()
        value = next(
            (
                group
                for group in (match.group(2), match.group(3), match.group(4))
                if group is not None
            ),
            "",
        )
        parsed[name] = unescape(value)
    return parsed


def clean_bookmark_title(raw_title: str) -> str:
    """Collapse bookmark title text while tolerating nested/broken markup."""
    without_tags = re.sub(r"<[^>]*>", " ", raw_title or "")
    return " ".join(unescape(without_tags).split())


def find_tag_end(content: str, start: int) -> int:
    """Return the end of an opening tag, ignoring > inside quoted attrs."""
    quote = ""
    i = start
    while i < len(content):
        char = content[i]
        if quote:
            if char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == ">":
            return i
        i += 1
    return -1


def find_next_anchor_start(content: str, start: int) -> int:
    """Find the next <a tag start without treating words like <article as anchors."""
    lowered = content.lower()
    pos = start
    while True:
        pos = lowered.find("<a", pos)
        if pos == -1:
            return -1
        next_char_index = pos + 2
        if next_char_index >= len(content):
            return -1
        if content[next_char_index].isspace() or content[next_char_index] in {">", "/"}:
            return pos
        pos += 2


def iter_bookmarks(content: str):
    """Yield loose Netscape bookmark anchors from the full file content."""
    lowered = content.lower()
    pos = 0
    while True:
        anchor_start = find_next_anchor_start(content, pos)
        if anchor_start == -1:
            return

        open_end = find_tag_end(content, anchor_start)
        if open_end == -1:
            return

        attrs_text = content[anchor_start + 2 : open_end]
        close_start = lowered.find("</a", open_end + 1)
        if close_start == -1:
            title_text = ""
            pos = open_end + 1
        else:
            title_text = content[open_end + 1 : close_start]
            close_end = find_tag_end(content, close_start)
            pos = close_end + 1 if close_end != -1 else close_start + 3

        attrs = parse_bookmark_attrs(attrs_text)
        bookmark_url = attrs.get("href")
        if not bookmark_url:
            continue
        yield {
            "url": bookmark_url,
            "add_date": attrs.get("add_date", ""),
            "tags": attrs.get("tags", ""),
            "title": clean_bookmark_title(title_text),
        }


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
    extra_context = get_extra_context()
    if "snapshot_depth" in extra_context:
        depth = int(extra_context["snapshot_depth"])
    print("parsing 1 files for urls...")
    try:
        content = fetch_content(url)
    except Exception as e:
        if url.startswith(("http://", "https://")):
            # Snapshot URL fetching is only a fallback when no staticfile import
            # artifact exists. Normal webpages, blocked requests, or transient
            # network errors should not make this parser hook look broken.
            status, output_str = persist_records([])
            print(output_str)
            emit_result(status, output_str)
            sys.exit(0)
        emit_result("failed", f"Failed to fetch {url}: {e}")
        sys.exit(1)

    if not looks_like_netscape_bookmarks(content):
        status, output_str = persist_records([])
        print(output_str)
        emit_result(status, output_str)
        sys.exit(0)

    urls_found = []
    all_tags = set()

    for bookmark in iter_bookmarks(content):
        bookmark_url = bookmark["url"]
        timestamp_str = bookmark["add_date"]
        tags_str = bookmark["tags"]
        title = bookmark["title"]
        resolved_url = normalize_bookmark_url(bookmark_url, url)

        entry = {
            "type": "Snapshot",
            "url": resolved_url,
            "plugin": PLUGIN_NAME,
            "depth": depth + 1,
        }
        if title:
            entry["title"] = title
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
