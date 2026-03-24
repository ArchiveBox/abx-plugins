#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-plugins",
# ]
# ///
"""
Parse plain text files and extract URLs.

This is a standalone extractor that can run without ArchiveBox.
It reads text content from a URL (file:// or https://) and extracts all URLs found.

Usage: ./on_Snapshot__71_parse_txt_urls.py --url=<url>
Output: Appends discovered URLs to SNAP_DIR/parse_txt_urls/urls.jsonl

Examples:
    ./on_Snapshot__71_parse_txt_urls.py --url=file:///path/to/urls.txt
    ./on_Snapshot__71_parse_txt_urls.py --url=https://example.com/urls.txt
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    load_config,
    write_text_atomic,
)

import rich_click as click

PLUGIN_NAME = "parse_txt_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
URLS_FILE = Path("urls.jsonl")
NORESULTS_OUTPUT = "0 URLs parsed"

# URL regex from archivebox/misc/util.py
# https://mathiasbynens.be/demo/url-regex
URL_REGEX = re.compile(
    r"(?=("
    r"http[s]?://"  # start matching from allowed schemes
    r"(?:[a-zA-Z]|[0-9]"  # followed by allowed alphanum characters
    r"|[-_$@.&+!*\(\),]"  #   or allowed symbols (keep hyphen first to match literal hyphen)
    r"|[^\u0000-\u007F])+"  #   or allowed unicode bytes
    r'[^\]\[<>"\'\s]+'  # stop parsing at these symbols
    r"))",
    re.IGNORECASE | re.UNICODE,
)


def parens_are_matched(string: str, open_char="(", close_char=")") -> bool:
    """Check that all parentheses in a string are balanced and nested properly."""
    count = 0
    for c in string:
        if c == open_char:
            count += 1
        elif c == close_char:
            count -= 1
        if count < 0:
            return False
    return count == 0


def fix_url_from_markdown(url_str: str) -> str:
    """
    Cleanup a regex-parsed URL that may contain trailing parens from markdown syntax.
    Example: https://wiki.org/article_(Disambiguation).html?q=1).text -> https://wiki.org/article_(Disambiguation).html?q=1
    """
    trimmed_url = url_str

    # Cut off trailing characters until parens are balanced
    while not parens_are_matched(trimmed_url):
        trimmed_url = trimmed_url[:-1]

    # Verify trimmed URL is still valid
    if re.findall(URL_REGEX, trimmed_url):
        return trimmed_url

    return url_str


def split_comma_separated_urls(url: str):
    """Split combined matches like https://a,https://b without breaking balanced symbol handling."""
    offset = 0
    while True:
        http_index = url.find("http://", 1)
        https_index = url.find("https://", 1)
        next_indices = [idx for idx in (http_index, https_index) if idx != -1]
        if not next_indices:
            yield offset, url
            return

        next_index = min(next_indices)
        if url[next_index - 1] != ",":
            yield offset, url
            return

        yield offset, url[: next_index - 1]
        offset += next_index
        url = url[next_index:]


def find_all_urls(text: str):
    """Find all URLs in a text string."""
    skipped_starts = set()
    for match in re.finditer(URL_REGEX, text):
        if match.start() in skipped_starts:
            continue

        for offset, url in split_comma_separated_urls(
            fix_url_from_markdown(match.group(1)),
        ):
            if offset:
                skipped_starts.add(match.start() + offset)
            yield url


def fetch_content(url: str) -> str:
    """Fetch content from a URL (supports file:// and https://)."""
    parsed = urlparse(url)

    if parsed.scheme == "file":
        # Local file
        file_path = parsed.path
        with open(file_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    else:
        # Remote URL
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
@click.option("--url", required=True, help="URL to parse (file:// or https://)")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    depth: int = 0,
):
    """Parse plain text and extract URLs."""
    if CONFIG.SNAPSHOT_DEPTH is not None:
        depth = CONFIG.SNAPSHOT_DEPTH
    try:
        content = fetch_content(url)
    except Exception as e:
        message = f"Failed to fetch {url}: {e}"
        emit_result("failed", message)
        sys.exit(1)

    urls_found = set()
    for found_url in find_all_urls(content):
        cleaned_url = sanitize_extracted_url(found_url)
        # Skip the source URL itself
        if cleaned_url and cleaned_url != url:
            urls_found.add(cleaned_url)

    # Emit Snapshot records to stdout (JSONL)
    records = []
    for found_url in sorted(urls_found):
        record = {
            "type": "Snapshot",
            "url": found_url,
            "plugin": PLUGIN_NAME,
            "depth": depth + 1,
        }
        records.append(record)
        emit_snapshot_record(record)

    # Emit ArchiveResult record to mark completion
    status, output_str = persist_records(records)
    emit_result(status, output_str)
    sys.exit(0)


if __name__ == "__main__":
    main()
