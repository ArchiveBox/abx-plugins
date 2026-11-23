"""Bookmark records shared by import hooks and upload previews."""

import csv
import io
import json
from datetime import datetime
from html import unescape

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url

PLUGIN_NAME = "parse_jsonl_urls"


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
    if not isinstance(url, str) or not url:
        return None
    archivebox_record = link.get("type") == "Snapshot" and bool(
        link.get("schema_version"),
    )
    cleaned_url = url if archivebox_record else sanitize_extracted_url(url)
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
        entry["title"] = title if archivebox_record else unescape(title)

    # Parse bookmarked_at (ISO 8601)
    bookmarked_at = parse_bookmarked_at(link)
    if bookmarked_at:
        entry["bookmarked_at"] = bookmarked_at

    # Parse tags
    tags = link["tags"] if "tags" in link else ""
    if isinstance(tags, list):
        tags = ",".join(tags)
    elif not archivebox_record and isinstance(tags, str) and "," not in tags and tags:
        # If no comma, assume space-separated
        tags = tags.replace(" ", ",")
    if tags:
        entry["tags"] = tags if archivebox_record else unescape(tags)

    return entry


def iter_json_records(content: str):
    """Accept a JSON document or one bookmark object per JSONL line."""
    if bookmark_input_format(content) == "csv":
        text = content.lstrip("\ufeff")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        yield from csv.DictReader(io.StringIO(text), dialect=dialect)
        return
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        for line in content.split("\n"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    else:
        yield from document if isinstance(document, list) else [document]


def bookmark_input_format(content: str) -> str | None:
    """Recognize structured bookmark inputs without scanning URL text as CSV."""
    text = content.lstrip("\ufeff \t\r\n")
    if text.startswith(("{", "[")):
        return "json"
    header = next(csv.reader(io.StringIO(text[:8192])), [])
    if "url" in header and any(
        field in header for field in ("title", "bookmarked_at", "tags")
    ):
        return "csv"
    return None
