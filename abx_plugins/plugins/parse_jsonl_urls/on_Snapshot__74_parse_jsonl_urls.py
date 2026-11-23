#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""
Parse JSON/JSONL bookmark files and extract URLs.

This is a standalone extractor that can run without ArchiveBox.
It reads captured staticfile text, imported .txt files, or an HTTP URL.

Usage: ./on_Snapshot__74_parse_jsonl_urls.py --url=<url>
Output: Appends discovered URLs to SNAP_DIR/parse_jsonl_urls/urls.jsonl

Expected JSONL format (one object per line):
    {"url": "https://example.com", "title": "Example", "tags": "tag1,tag2"}
    {"href": "https://other.com", "description": "Other Site"}

Supports various field names for URL, title, timestamp, and tags.
Also accepts a JSON object or array of bookmark objects, including pretty-printed JSON.
ArchiveBox CSV exports use the same bookmark fields, read with csv.DictReader.
"""

import sys
import json
import os
from pathlib import Path

from abx_plugins.plugins.parse_jsonl_urls.bookmarks import (
    iter_json_records,
    json_object_to_entry,
)
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    emit_tag_record,
    load_config,
    read_snapshot_source_text,
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
@click.option("--url", required=True, help="JSON or JSONL file URL to parse")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    depth: int = 0,
):
    """Parse JSON/JSONL bookmarks and extract explicit URL fields."""
    print("parsing 1 files for urls...")
    try:
        content = read_snapshot_source_text(
            url,
            snap_dir=SNAP_DIR,
            timeout=CONFIG.TIMEOUT,
            user_agent=CONFIG.USER_AGENT,
        )
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

    urls_found = []
    all_tags = set()

    for link in iter_json_records(content):
        if not isinstance(link, dict):
            continue
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
