#!/usr/bin/env -S uv run --active --script
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
import re
import sys
import io
from pathlib import Path
from urllib.parse import urlparse

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    get_extra_context,
    load_config,
    write_text_atomic,
)

import rich_click as click

PLUGIN_NAME = "parse_txt_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
URLS_FILENAME = "urls.jsonl"
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
SANITIZE_TRIGGER_CHARS = ('"', "'", "`", "&", "“", "”", "‘", "’")
READ_CHUNK_SIZE = 262144
URL_SCAN_OVERLAP = 8192


def fix_url_from_markdown(url_str: str) -> str:
    """
    Cleanup a regex-parsed URL that may contain trailing parens from markdown syntax.
    Example: https://wiki.org/article_(Disambiguation).html?q=1).text -> https://wiki.org/article_(Disambiguation).html?q=1
    """
    if "(" not in url_str and ")" not in url_str:
        return url_str

    balance = 0
    last_valid_end = 0
    for index, char in enumerate(url_str, start=1):
        if char == "(":
            balance += 1
        elif char == ")":
            balance -= 1
            if balance < 0:
                break
        if balance == 0:
            last_valid_end = index

    trimmed_url = url_str[:last_valid_end]
    if not trimmed_url or trimmed_url == url_str:
        return url_str

    # Verify trimmed URL is still valid
    if URL_REGEX.match(trimmed_url):
        return trimmed_url

    return url_str


def split_comma_separated_urls(url: str):
    """Split combined matches like https://a,https://b without breaking balanced symbol handling."""
    if "," not in url:
        yield 0, url
        return

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

        matched_url = match.group(1)
        if "(" in matched_url or ")" in matched_url:
            matched_url = fix_url_from_markdown(matched_url)

        for offset, url in split_comma_separated_urls(matched_url):
            if offset:
                skipped_starts.add(match.start() + offset)
            yield url


def get_output_file() -> Path:
    config = load_config()
    snap_dir = Path(config.SNAP_DIR or ".").resolve()
    output_dir = snap_dir / PLUGIN_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / URLS_FILENAME


def emit_result(status: str, output_str: str) -> None:
    """Emit final ArchiveResult JSONL plus a short stderr summary."""
    emit_archive_result_record(status, output_str)
    if output_str:
        click.echo(output_str, err=True)


def persist_records(records: list[dict], urls_file: Path) -> tuple[str, str]:
    """Write extracted URLs when present, otherwise clear stale output after success."""
    if records:
        write_text_atomic(
            urls_file,
            "\n".join(json.dumps(record) for record in records) + "\n",
        )
        return "succeeded", f"{len(records)} URLs parsed"

    urls_file.unlink(missing_ok=True)
    return "noresults", NORESULTS_OUTPUT


def add_urls_from_text_chunk(
    chunk: str,
    *,
    carry: str,
    final: bool,
    source_url: str,
    urls_found: set[str],
) -> str:
    text = carry + chunk
    scan_limit = len(text) if final else max(0, len(text) - URL_SCAN_OVERLAP)
    for match in re.finditer(URL_REGEX, text):
        start = match.start(1)
        end = match.end(1)
        if not final and start >= scan_limit:
            break
        if not final and end > scan_limit:
            continue

        matched_url = match.group(1)
        if "(" in matched_url or ")" in matched_url:
            matched_url = fix_url_from_markdown(matched_url)

        for _, found_url in split_comma_separated_urls(matched_url):
            if any(char in found_url for char in SANITIZE_TRIGGER_CHARS):
                cleaned_url = sanitize_extracted_url(found_url)
            else:
                cleaned_url = found_url.strip()
            if cleaned_url and cleaned_url != source_url:
                urls_found.add(cleaned_url)
    return "" if final else text[scan_limit:]


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
    urls_file = get_output_file()
    extra_context = get_extra_context()
    if "snapshot_depth" in extra_context:
        depth = int(extra_context["snapshot_depth"])
    urls_found = set()
    print("parsing 1 files for urls...")
    try:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            reader_cm = open(parsed.path, encoding="utf-8", errors="replace")
        else:
            config = load_config()
            timeout = config.TIMEOUT
            user_agent = config.USER_AGENT

            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            response = urllib.request.urlopen(req, timeout=timeout)
            reader_cm = io.TextIOWrapper(response, encoding="utf-8", errors="replace")

        carry = ""
        with reader_cm as reader:
            while True:
                chunk = reader.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                carry = add_urls_from_text_chunk(
                    chunk,
                    carry=carry,
                    final=False,
                    source_url=url,
                    urls_found=urls_found,
                )
        add_urls_from_text_chunk(
            "",
            carry=carry,
            final=True,
            source_url=url,
            urls_found=urls_found,
        )
    except Exception as e:
        message = f"Failed to fetch {url}: {e}"
        emit_result("failed", message)
        sys.exit(1)

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
    status, output_str = persist_records(records, urls_file)
    print(output_str)
    emit_result(status, output_str)
    sys.exit(0)


if __name__ == "__main__":
    main()
