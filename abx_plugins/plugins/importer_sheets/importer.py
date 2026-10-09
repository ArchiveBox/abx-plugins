#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""CSV discovery with content identities, independent of mutable row positions."""

import csv
import io
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from abx_plugins.plugins.base.importers import (
    NeedsLogin,
    emit_collection,
    fetch,
    item_id,
    run,
)


def csv_url(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?", parsed.path)
    if parsed.hostname == "docs.google.com" and match:
        query = {**parse_qs(parsed.query), **parse_qs(parsed.fragment)}
        return f"https://docs.google.com/spreadsheets/d/{match[1]}/export?" + urlencode(
            {"format": "csv", "gid": query.get("gid", ["0"])[0]},
        )
    return url


def main(request):
    content, mime, final_url = fetch(
        csv_url(request["settings"]["IMPORTER_SHEETS_URL"]),
        authenticated=True,
    )
    if urlsplit(final_url).hostname == "accounts.google.com":
        raise NeedsLogin(
            "Google requires a login. Sync your persona, or use a sheet with a readable CSV export.",
        )
    if mime == "text/html" or content.lstrip().lower().startswith(
        (b"<!doctype html", b"<html"),
    ):
        raise ValueError(
            "The source returned an HTML page instead of CSV. Check sharing and export access.",
        )
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    column = request["settings"]["IMPORTER_SHEETS_COLUMN"]
    if column not in (reader.fieldnames or []):
        raise ValueError(
            f"CSV has no column named {column!r}. Check the URL column setting.",
        )
    records = []
    for row_number, row in enumerate(reader, start=2):
        for url in re.findall(r'https?://[^\s<>"\)]+', row.get(column) or ""):
            records.append(
                {
                    "id": item_id([row, url]),
                    "url": url,
                    "title": row.get("Title") or row.get("title") or "",
                    "metadata": {"row": row_number, "relationship": "spreadsheet row"},
                },
            )
    emit_collection(
        request,
        records,
        message=f"Readable CSV; {len(records)} URL entries.",
    )


if __name__ == "__main__":
    run(main)
