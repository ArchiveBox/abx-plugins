#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Readwise Reader discovery through the standalone importer JSONL contract."""

import csv
import io
import json
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from abx_plugins.plugins.base.importers import (
    NeedsLogin,
    emit,
    emit_collection,
    fetch,
    item_id,
    run,
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the account token to a redirect target.
        return None


def document_item(document):
    url = document.get("source_url")
    if document.get("parent_id") or document.get("category") in {"highlight", "note"}:
        return None
    if (
        not isinstance(url, str)
        or urlsplit(url).scheme not in {"http", "https"}
        or not urlsplit(url).hostname
    ):
        return None
    tags = document.get("tags") or {}
    if isinstance(tags, dict):
        tags = [
            value.get("name", key) if isinstance(value, dict) else key
            for key, value in tags.items()
        ]
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("Readwise returned invalid document tags.")
    return {
        "type": "ImporterItem",
        "id": str(document["id"]),
        "url": url,
        "title": document.get("title") or "",
        "metadata": {
            "tags": list(dict.fromkeys(tags)),
            "bookmarked_at": document.get("saved_at") or document.get("created_at"),
            "location": document.get("location", ""),
            "category": document.get("category", ""),
        },
    }


def main(request):
    if request["feed"] == "csv":
        return import_csv(request)
    if request["feed"] != "documents":
        raise ValueError("Unknown Readwise source.")
    settings = request["settings"]
    token = settings.get("READWISE_TOKEN", "").strip()
    if not token:
        raise NeedsLogin("Enter a Readwise access token.")
    endpoint = settings.get("READWISE_API_URL") or "https://readwise.io/api/v3/list/"
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use an HTTP(S) API endpoint without credentials, query, or fragment."
        )
    if parsed.scheme != "https" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Readwise API credentials require HTTPS.")
    location = settings.get("READWISE_LOCATION", "archive")
    if location not in {"all", "new", "later", "shortlist", "archive", "feed"}:
        raise ValueError("Unknown Readwise location.")
    checkpoint = (
        dict(request.get("checkpoint", {})) if request["action"] == "import" else {}
    )
    # Freeze the watermark at scan start, not completion: edits during pagination
    # must remain eligible for the next scheduled scan.
    started_at = checkpoint.get("started_at") or datetime.now(timezone.utc).isoformat()
    cursor = checkpoint.get("cursor")
    seen_cursors = set()
    last_request = float(checkpoint.get("requested_at", 0))
    while True:
        emit({"type": "ImporterProgress", "message": "Fetching Reader documents."})
        params: dict[str, str | int] = {"limit": min(request["limit"], 100)}
        if location != "all":
            params["location"] = location
        if cursor:
            params["pageCursor"] = cursor
        if checkpoint.get("updated_after"):
            params["updatedAfter"] = checkpoint["updated_after"]
        # Reader allows 20 list requests/minute, including across batch processes.
        time.sleep(max(0, min(3.1, last_request + 3.1 - time.time())))
        last_request = time.time()
        query = Request(
            endpoint + "?" + urlencode(params),
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
        )
        try:
            with build_opener(NoRedirect()).open(query, timeout=30) as response:
                content = response.read(10 * 1024 * 1024 + 1)
        except HTTPError as error:
            if error.code in {401, 403}:
                raise NeedsLogin("Readwise rejected the access token.") from None
            raise ValueError(
                f"Readwise returned HTTP {error.code}; progress was not advanced."
            ) from None
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("Readwise response exceeds 10 MiB.")
        page = json.loads(content)
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("results"), list)
            or "nextPageCursor" not in page
        ):
            raise ValueError("Readwise returned an invalid document list.")
        next_cursor = page["nextPageCursor"]
        if next_cursor is not None and (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor == cursor
            or next_cursor in seen_cursors
        ):
            raise ValueError("Readwise returned an invalid or repeated page cursor.")
        items = [
            item
            for document in page["results"]
            if (item := document_item(document)) is not None
        ]
        if len(items) > request["limit"]:
            raise ValueError("Readwise exceeded the requested batch size.")
        # Preview should find a real sample even when a page only has highlights.
        if request["action"] == "preview" and not items and next_cursor:
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            continue
        if request["action"] != "check":
            for item in items:
                emit(item)
        if next_cursor:
            checkpoint.update(
                cursor=next_cursor, started_at=started_at, requested_at=last_request
            )
        else:
            checkpoint = {"updated_after": started_at, "requested_at": last_request}
        emit(
            {
                "type": "ImporterResult",
                "status": "succeeded",
                "checkpoint": checkpoint,
                "has_more": bool(next_cursor),
                "message": f"{len(items)} documents.",
            }
        )
        return


def import_csv(request):
    emit({"type": "ImporterProgress", "message": "Reading Reader CSV export."})
    content, _, _ = fetch(request["settings"]["READWISE_EXPORT_URL"])
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if not {"URL", "Title", "Saved date", "Document tags"} <= set(
        reader.fieldnames or []
    ):
        raise ValueError("Expected a Readwise Reader library CSV export.")
    records = []
    for row in reader:
        tags = [
            tag.strip()
            for tag in next(csv.reader([row.get("Document tags") or ""]))
            if tag.strip()
        ]
        record = document_item(
            {
                "id": row.get("ID") or item_id(row["URL"]),
                "source_url": row["URL"],
                "title": row["Title"],
                "saved_at": row["Saved date"],
                "tags": tags,
                "location": row.get("Location", ""),
            }
        )
        if record:
            records.append(record)
    emit_collection(request, records, message=f"{len(records)} Reader documents.")


if __name__ == "__main__":
    run(main)
