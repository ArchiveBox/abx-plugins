#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Finite search result discovery through the native importer JSONL contract."""

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from abx_plugins.plugins.base.importers import NeedsLogin, emit_collection, item_id, run
from abx_plugins.plugins.base.utils import load_config

ENDPOINTS = {
    "kagi": "https://kagi.com/api/v1/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "exa": "https://api.exa.ai/search",
    "duckduckgo": "https://html.duckduckgo.com/html/",
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API credentials must never follow a server-controlled redirect.
        return None


def result_item(url, title):
    if not isinstance(url, str) or not isinstance(title, str):
        raise ValueError("Search returned invalid result fields.")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            return None
        # Fragments do not identify a distinct fetched document. Preserve queries.
        url = urlunsplit(parts._replace(fragment=""))
    except ValueError:
        return None
    if len(url) > 4096:
        return None
    return {"id": item_id(url), "url": url, "title": title, "metadata": {}}


class DuckDuckGoResults(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.link = None
        self.title = []
        self.empty = False
        self.blocked = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if "no-results" in classes:
            self.empty = True
        if (
            attrs.get("id") in {"challenge-form", "img-form"}
            or "anomaly-modal" in classes
        ):
            self.blocked = True
        if tag == "a" and "result__a" in classes:
            self.link, self.title = attrs.get("href"), []

    def handle_data(self, data):
        if self.link is not None:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self.link is None:
            return
        url = urljoin(ENDPOINTS["duckduckgo"], self.link)
        parsed = urlsplit(url)
        if parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"}:
            url = parse_qs(parsed.query).get("uddg", [""])[0]
        record = result_item(url, "".join(self.title).strip())
        if record:
            self.results.append(record)
        self.link = None


def parse_results(feed, content):
    if feed == "duckduckgo":
        parser = DuckDuckGoResults()
        parser.feed(content.decode("utf-8"))
        if parser.blocked or (not parser.results and not parser.empty):
            raise ValueError(
                "DuckDuckGo did not return a recognized results page; it may require interactive access. No progress was advanced.",
            )
        return parser.results
    try:
        page = json.loads(content)
        if feed == "kagi":
            rows = page["data"]["search"]
        elif feed == "brave":
            rows = page["web"]["results"]
        else:
            rows = page["results"]
        if not isinstance(rows, list):
            raise TypeError
        records = []
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError
            record = result_item(row["url"], row.get("title") or "")
            if record:
                records.append(record)
        return records
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError(
            "Search API returned an invalid results response; no progress was advanced.",
        ) from None


def fetch_results(feed, query, key, endpoint):
    """Fetch one result window. Production supplies only a fixed official URL."""
    parts = urlsplit(endpoint)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Use an HTTP(S) endpoint without credentials, query, or fragment.",
        )
    if parts.scheme != "https" and parts.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Search endpoints require HTTPS except on loopback.")
    headers = {
        "User-Agent": "ArchiveBox (+https://archivebox.io)",
        "Accept": "application/json",
    }
    body = None
    if feed == "duckduckgo":
        endpoint += "?" + urlencode({"q": query})
        headers["Accept"] = "text/html"
    else:
        if not key:
            raise NeedsLogin(
                f"Configure {feed.upper()}_SEARCH_API_KEY in Search plugin settings.",
            )
        if feed == "brave":
            headers["X-Subscription-Token"] = key
            endpoint += "?" + urlencode(
                {"q": query, "count": 20, "result_filter": "web"},
            )
        else:
            headers["Authorization" if feed == "kagi" else "x-api-key"] = (
                f"Bearer {key}" if feed == "kagi" else key
            )
            headers["Content-Type"] = "application/json"
            payload = (
                {"query": query, "workflow": "search"}
                if feed == "kagi"
                else {"query": query, "type": "auto", "numResults": 20}
            )
            body = json.dumps(payload).encode()
    try:
        with build_opener(NoRedirect()).open(
            Request(endpoint, data=body, headers=headers),
            timeout=30,
        ) as response:
            content = response.read(4 * 1024 * 1024 + 1)
    except HTTPError as error:
        if error.code in {401, 403} and feed != "duckduckgo":
            raise NeedsLogin(
                "Search provider denied access; check API key and plan permissions.",
            ) from None
        raise ValueError(
            f"Search provider returned HTTP {error.code}; no progress was advanced.",
        ) from None
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("Search response exceeds 4 MiB.")
    return parse_results(feed, content)


def main(request):
    feed = request["feed"]
    if feed not in ENDPOINTS:
        raise ValueError("Unknown search provider.")
    query = request["settings"].get("SEARCH_QUERY", "").strip()
    if not query or len(query) > 600 or len(query.split()) > 75:
        raise ValueError("Enter a search query of at most 600 characters and 75 words.")
    key = ""
    if feed != "duckduckgo":
        config = load_config(
            Path(__file__).with_name("config.json"),
            hydrate_binaries=False,
        )
        key = getattr(config, feed.upper() + "_SEARCH_API_KEY", "").strip()
    records = fetch_results(feed, query, key, ENDPOINTS[feed])
    # Search rankings are not chronological cursors: re-query the finite result
    # window, and let native item IDs/checkpoints deduplicate scheduled runs.
    emit_collection(request, records, message=f"{len(records)} search results.")


if __name__ == "__main__":
    run(main)
