#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
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
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from abx_plugins.plugins.base.utils import emit_archive_result_record, emit_snapshot_record, write_text_atomic

import rich_click as click

PLUGIN_NAME = "parse_txt_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
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


def find_all_urls(text: str):
    """Find all URLs in a text string."""
    for url in re.findall(URL_REGEX, text):
        yield fix_url_from_markdown(url)


def fetch_content(url: str) -> str:
    """Fetch content from a URL (supports file:// and https://)."""
    parsed = urlparse(url)

    if parsed.scheme == "file":
        # Local file
        file_path = parsed.path
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    else:
        # Remote URL
        timeout = int(os.environ.get("TIMEOUT", "60"))
        user_agent = os.environ.get(
            "USER_AGENT", "Mozilla/5.0 (compatible; ArchiveBox/1.0)"
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
            URLS_FILE, "\n".join(json.dumps(record) for record in records) + "\n"
        )
        return "succeeded", f"{len(records)} URLs parsed"

    URLS_FILE.unlink(missing_ok=True)
    return "noresults", NORESULTS_OUTPUT


@click.command()
@click.option("--url", required=True, help="URL to parse (file:// or https://)")
@click.option("--snapshot-id", required=False, help="Parent Snapshot UUID")
@click.option("--crawl-id", required=False, help="Crawl UUID")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    snapshot_id: str | None = None,
    crawl_id: str | None = None,
    depth: int = 0,
):
    """Parse plain text and extract URLs."""
    env_depth = os.environ.get("SNAPSHOT_DEPTH")
    if env_depth is not None:
        try:
            depth = int(env_depth)
        except Exception:
            pass
    crawl_id = crawl_id or os.environ.get("CRAWL_ID")

    try:
        content = fetch_content(url)
    except Exception as e:
        message = f"Failed to fetch {url}: {e}"
        emit_result("failed", message)
        sys.exit(1)

    urls_found = set()
    for found_url in find_all_urls(content):
        cleaned_url = unescape(found_url)
        # Skip the source URL itself
        if cleaned_url != url:
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
        if snapshot_id:
            record["parent_snapshot_id"] = snapshot_id
        if crawl_id:
            record["crawl_id"] = crawl_id
        records.append(record)
        emit_snapshot_record(record)

    # Emit ArchiveResult record to mark completion
    status, output_str = persist_records(records)
    emit_result(status, output_str)
    sys.exit(0)


if __name__ == "__main__":
    main()
