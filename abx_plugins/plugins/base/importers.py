"""Standalone importer JSONL contract helpers; no host or database dependencies."""

import hashlib
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class NeedsLogin(Exception):
    pass


def read_request():
    request = json.load(sys.stdin)
    if request.get("version") != 1 or request.get("action") not in {
        "check",
        "preview",
        "import",
    }:
        raise ValueError("Unsupported importer request version or action.")
    if not isinstance(request.get("settings"), dict) or not isinstance(
        request.get("checkpoint", {}),
        dict,
    ):
        raise TypeError("Settings and checkpoint must be objects.")
    if not isinstance(request.get("limit"), int) or not 1 <= request["limit"] <= 500:
        raise ValueError("The item limit must be between 1 and 500.")
    return request


def read_records(lines, request, on_progress=None):
    """Validate the same JSONL boundary for host ingestion and learned-script replay."""
    size = count = 0
    finished = False
    for line in lines:
        size += len(line.encode())
        if size > 4 * 1024 * 1024:
            raise ValueError("Importer output exceeded 4 MiB; reduce the batch size.")
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or finished:
            raise ValueError(
                "Invalid importer record or output after the final result.",
            )
        kind = record.get("type")
        if kind == "ImporterProgress":
            if on_progress:
                on_progress(str(record.get("message", ""))[:4000])
            continue
        if kind == "ImporterItem":
            count += 1
            if count > request["limit"] or request["action"] == "check":
                raise ValueError("Importer exceeded this action's item limit.")
            url = record.get("url")
            if (
                not isinstance(url, str)
                or len(url) > 4096
                or urlsplit(url).scheme not in {"http", "https"}
                or not urlsplit(url).hostname
            ):
                raise ValueError("Importer returned an invalid HTTP(S) URL.")
            if (
                not isinstance(record.get("id"), str)
                or not record["id"]
                or len(record["id"]) > 1024
            ):
                raise ValueError("Importer items must have a stable string ID.")
            if not isinstance(record.get("title", ""), str) or not isinstance(
                record.get("metadata", {}),
                dict,
            ):
                raise ValueError("Importer returned invalid item metadata.")
        elif kind == "ImporterResult":
            if record.get("status") not in {"succeeded", "needs_login", "failed"}:
                raise ValueError("Importer returned an unknown result status.")
            if (
                record["status"] == "succeeded"
                and request["action"] == "import"
                and not isinstance(record.get("has_more"), bool)
            ):
                raise ValueError(
                    "A successful import must explicitly report whether more history remains.",
                )
            if not isinstance(record.get("checkpoint", {}), dict) or not isinstance(
                record.get("account", {}),
                dict,
            ):
                raise ValueError("Importer checkpoint and account must be objects.")
            finished = True
        else:
            raise ValueError("Importer returned an unknown record type.")
        yield record
    if not finished:
        raise ValueError(
            "Importer exited without a final result. Progress was not advanced.",
        )


def fetch(url, *, authenticated=False):
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("The source URL must use HTTP or HTTPS.")
    cookies = http.cookiejar.MozillaCookieJar()
    if (
        authenticated
        and (cookie_file := os.environ.get("COOKIES_FILE"))
        and Path(cookie_file).is_file()
    ):
        cookies.load(cookie_file, ignore_discard=True, ignore_expires=False)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": os.environ.get("USER_AGENT") or "ArchiveBox importer"},
    )
    try:
        with opener.open(
            request,
            timeout=max(1, int(os.environ.get("TIMEOUT", "60"))),
        ) as response:
            content = response.read(10 * 1024 * 1024 + 1)
            if len(content) > 10 * 1024 * 1024:
                raise ValueError(
                    "Source exceeds 10 MiB. Select a smaller feed or spreadsheet range.",
                )
            return content, response.headers.get_content_type(), response.url
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise NeedsLogin(
                "The source requires a working login. Sync your persona and check access again.",
            ) from error
        raise ValueError(
            f"The source returned HTTP {error.code}. Progress was not advanced.",
        ) from error


def item_id(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode(),
    ).hexdigest()


def emit(record):
    print(json.dumps(record, ensure_ascii=False), flush=True)


def emit_collection(request, records, *, message, account=None):
    """Track observed IDs, retaining only IDs still present in a finite source."""
    if request["action"] == "check":
        emit(
            {
                "type": "ImporterResult",
                "status": "succeeded",
                "message": message,
                "account": account or {},
            },
        )
        return
    seen = (
        set(request.get("checkpoint", {}).get("seen", []))
        if request["action"] == "import"
        else set()
    )
    unique = {record["id"]: record for record in records}
    pending = [record for key, record in unique.items() if key not in seen]
    selected = pending[: request["limit"]]
    for record in selected:
        emit({"type": "ImporterItem", **record})
    checkpoint = {
        "seen": sorted((seen & unique.keys()) | {record["id"] for record in selected}),
    }
    emit(
        {
            "type": "ImporterResult",
            "status": "succeeded",
            "account": account or {},
            "checkpoint": checkpoint,
            "has_more": len(pending) > len(selected),
            "message": f"{len(selected)} new items; {max(0, len(pending) - len(selected))} remaining in this source.",
        },
    )


def run(main):
    try:
        main(read_request())
    except NeedsLogin as error:
        emit({"type": "ImporterResult", "status": "needs_login", "message": str(error)})
    except (
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
        urllib.error.URLError,
        http.cookiejar.LoadError,
    ) as error:
        # Do not include source URLs, cookie contents, or redirect URLs in errors.
        message = (
            str(error)
            if isinstance(error, ValueError)
            else f"Cannot read source ({type(error).__name__})."
        )
        emit({"type": "ImporterResult", "status": "failed", "message": message})
