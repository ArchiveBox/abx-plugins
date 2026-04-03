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
#
# Parse HTML files and extract href URLs.
#
# This is a standalone extractor that can run without ArchiveBox.
# It reads HTML content and extracts all <a href="..."> URLs.
#
# Usage: ./on_Snapshot__70_parse_html_urls.py --url=<url>
# Output: Appends discovered URLs to SNAP_DIR/parse_html_urls/urls.jsonl
#
# Examples:
#     ./on_Snapshot__70_parse_html_urls.py --url=file:///path/to/page.html
#     ./on_Snapshot__70_parse_html_urls.py --url=https://example.com/page.html

import json
import os
import re
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    get_extra_context,
    load_config,
    write_text_atomic,
)

import rich_click as click

PLUGIN_NAME = "parse_html_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)

URLS_FILE = Path("urls.jsonl")
NORESULTS_OUTPUT = "0 URLs parsed"


# URL regex from archivebox/misc/util.py
URL_REGEX = re.compile(
    r"(?=("
    r"http[s]?://"
    r"(?:[a-zA-Z]|[0-9]"
    r"|[-_$@.&+!*\(\),]"
    r"|[^\u0000-\u007F])+"
    r'[^\]\[<>"\'\s]+'
    r"))",
    re.IGNORECASE | re.UNICODE,
)
READ_CHUNK_SIZE = 262144
URL_SCAN_OVERLAP = 8192
HTTP_PREFIXES = ("http://", "https://")
URL_EDGE_STRIP_CHARS = " \t\r\n\"''<>[]()"
URL_TRAILING_ARTIFACTS = ".,;:!?)\\'\""


class HrefParser(HTMLParser):
    """Extract href URLs and explicit absolute URLs while streaming one HTML source."""

    def __init__(self, *, root_url: str, urls_found: set[str]):
        super().__init__()
        self.root_url = root_url
        self.urls_found = urls_found
        self.raw_tail = ""

    def _add_url(self, url: str) -> None:
        normalized = normalize_url(url, root_url=self.root_url)
        lowered = normalized.lower()
        if lowered.startswith(HTTP_PREFIXES):
            if normalized != self.root_url:
                self.urls_found.add(
                    unescape(normalized) if "&" in normalized else normalized,
                )

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for attr, value in attrs:
                if attr == "href" and value:
                    self._add_url(value)

    def scan_raw_chunk(self, chunk: str, *, final: bool = False) -> None:
        text = self.raw_tail + chunk
        scan_limit = len(text) if final else max(0, len(text) - URL_SCAN_OVERLAP)
        if "://" not in text:
            self.raw_tail = "" if final else text[scan_limit:]
            return
        for match in URL_REGEX.finditer(text):
            start = match.start(1)
            end = match.end(1)
            if not final and start >= scan_limit:
                break
            if not final and end > scan_limit:
                continue
            self._add_url(match.group(1))
        self.raw_tail = "" if final else text[scan_limit:]


def did_urljoin_misbehave(root_url: str, relative_path: str, final_url: str) -> bool:
    """Check if urljoin incorrectly stripped // from sub-URLs."""
    relative_path = relative_path.lower()
    if relative_path.startswith("http://") or relative_path.startswith("https://"):
        relative_path = relative_path.split("://", 1)[-1]

    original_path_had_suburl = "://" in relative_path
    original_root_had_suburl = "://" in root_url[8:]
    final_joined_has_suburl = "://" in final_url[8:]

    return (
        original_root_had_suburl or original_path_had_suburl
    ) and not final_joined_has_suburl


def fix_urljoin_bug(url: str, nesting_limit=5) -> str:
    """Fix broken sub-URLs where :// was changed to :/."""
    input_url = url
    for _ in range(nesting_limit):
        url = re.sub(
            r"(?P<root>.+?)"
            r"(?P<separator>[-=/_&+%$#@!*\(\\])"
            r"(?P<subscheme>[a-zA-Z0-9+_-]{1,32}?):/"
            r"(?P<suburl>[^/\\]+)",
            r"\1\2\3://\4",
            input_url,
            re.IGNORECASE | re.UNICODE,
        )
        if url == input_url:
            break
        input_url = url
    return url


def normalize_url(url: str, root_url: str | None = None) -> str:
    """Normalize a URL, resolving relative paths if root_url provided."""
    url = clean_url_candidate(url)
    if not root_url:
        return _normalize_trailing_slash(url)

    if url.lower().startswith(HTTP_PREFIXES):
        return url

    # Resolve relative URL
    resolved = urljoin(root_url, url)

    # Fix urljoin bug with sub-URLs
    if did_urljoin_misbehave(root_url, url, resolved):
        resolved = fix_urljoin_bug(resolved)

    return _normalize_trailing_slash(resolved)


def _normalize_trailing_slash(url: str) -> str:
    """Drop trailing slash for non-root paths when no query/fragment."""
    if not url.endswith("/") or "?" in url or "#" in url:
        return url
    try:
        parsed = urlparse(url)
        path = parsed.path or ""
        if (
            path != "/"
            and path.endswith("/")
            and not parsed.query
            and not parsed.fragment
        ):
            path = path.rstrip("/")
            return urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                ),
            )
    except Exception:
        pass
    return url


def clean_url_candidate(url: str) -> str:
    """Strip obvious surrounding/trailing punctuation from extracted URLs."""
    if _is_obviously_clean_url(url):
        return url

    cleaned = sanitize_extracted_url(url)
    if not cleaned:
        return cleaned

    # Strip common wrappers
    cleaned = cleaned.strip(" \t\r\n")
    cleaned = cleaned.strip("\"''<>[]()")

    # Strip trailing punctuation and escape artifacts
    cleaned = cleaned.rstrip(".,;:!?)\\'\"")
    cleaned = cleaned.rstrip('"')

    # Strip leading punctuation artifacts
    cleaned = cleaned.lstrip("(\"'<")

    return cleaned


def _is_obviously_clean_url(url: str) -> bool:
    """Fast-path URLs that the cleanup logic would return unchanged."""
    return bool(
        url
        and url[0] not in URL_EDGE_STRIP_CHARS
        and url[-1] not in (URL_EDGE_STRIP_CHARS + URL_TRAILING_ARTIFACTS)
        and '"' not in url
        and "'" not in url
        and "`" not in url
        and "&" not in url
        and "“" not in url
        and "”" not in url
        and "‘" not in url
        and "’" not in url,
    )


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


def iter_html_source_paths():
    """Yield HTML source files from other extractors in the snapshot directory."""
    search_patterns = [
        "readability/content.html",
        "*_readability/content.html",
        "mercury/content.html",
        "*_mercury/content.html",
        "singlefile/singlefile.html",
        "*_singlefile/singlefile.html",
        "singlefile/*.html",
        "*_singlefile/*.html",
        "dom/output.html",
        "*_dom/output.html",
        "dom/*.html",
        "*_dom/*.html",
        "wget/**/*.html",
        "*_wget/**/*.html",
        "wget/**/*.htm",
        "*_wget/**/*.htm",
        "wget/**/*.htm*",
        "*_wget/**/*.htm*",
    ]

    seen_paths: set[Path] = set()
    for base in (Path.cwd(), Path.cwd().parent):
        for pattern in search_patterns:
            for match in base.glob(pattern):
                if not match.is_file() or match.stat().st_size == 0:
                    continue
                resolved = match.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                yield resolved


def extract_urls_from_reader(reader, *, root_url: str, urls_found: set[str]) -> None:
    parser = HrefParser(root_url=root_url, urls_found=urls_found)
    while True:
        chunk = reader.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        parser.feed(chunk)
        parser.scan_raw_chunk(chunk)
    parser.close()
    parser.scan_raw_chunk("", final=True)


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="HTML URL to parse")
@click.option("--depth", type=int, default=0, help="Current depth level")
def main(
    url: str,
    depth: int = 0,
):
    """Parse HTML and extract href URLs."""
    extra_context = get_extra_context()
    if "snapshot_depth" in extra_context:
        depth = int(extra_context["snapshot_depth"])
    urls_found = set()
    source_paths = tuple(iter_html_source_paths())
    print(f"parsing {len(source_paths) if source_paths else 1} files for urls...")
    try:
        if source_paths:
            for source_path in source_paths:
                with source_path.open(encoding="utf-8", errors="replace") as reader:
                    extract_urls_from_reader(
                        reader,
                        root_url=url,
                        urls_found=urls_found,
                    )
        else:
            parsed = urlparse(url)
            if parsed.scheme == "file":
                reader_cm = open(parsed.path, encoding="utf-8", errors="replace")
            else:
                timeout = CONFIG.TIMEOUT
                user_agent = CONFIG.USER_AGENT

                import io
                import urllib.request

                req = urllib.request.Request(url, headers={"User-Agent": user_agent})
                response = urllib.request.urlopen(req, timeout=timeout)
                reader_cm = io.TextIOWrapper(
                    response,
                    encoding="utf-8",
                    errors="replace",
                )
            with reader_cm as reader:
                extract_urls_from_reader(reader, root_url=url, urls_found=urls_found)
    except Exception as e:
        emit_result("failed", f"Failed to fetch {url}: {e}")
        sys.exit(1)

    # Emit Snapshot records to stdout (JSONL) and urls.jsonl for crawl system
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
    print(output_str)
    emit_result(status, output_str)
    sys.exit(0)


if __name__ == "__main__":
    main()
