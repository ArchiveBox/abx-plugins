#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""RSS/Atom discovery command; JSON request on stdin, importer JSONL on stdout."""

import re
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

import feedparser
from abx_plugins.plugins.base.importers import emit_collection, fetch, item_id, run


def _is_google_alerts_url(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "www.google.com"
        and re.fullmatch(r"/alerts/feeds/[^/]+/[^/]+/?", parsed.path) is not None
    )


def _google_alerts_publisher(link):
    try:
        query = parse_qs(
            link.query,
            keep_blank_values=True,
            errors="strict",
            max_num_fields=64,
        )
        values = query.get("url", [])
        if len(values) != 1:
            return None
        url = values[0]
        target = urlsplit(url)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.port == 0
            or any(char.isspace() or ord(char) < 32 or char in "\\\x7f" for char in url)
            or (
                target.hostname.rstrip(".") in {"google.com", "www.google.com"}
                and target.path == "/url"
            )
        ):
            return None
        return url
    except ValueError:
        return None


def main(request):
    content, _, source_url = fetch(
        request["settings"]["IMPORTER_RSS_URL"],
        authenticated=True,
    )
    import_feed(request, content, source_url)


def import_feed(request, content, source_url):
    feed = feedparser.parse(content)
    if not feed.version:
        raise ValueError(
            "The response is not an RSS or Atom feed. Check the feed URL and access.",
        )
    # Do not reinterpret arbitrary links in ordinary feeds (including URLs
    # which merely redirect to or from an Alerts feed).
    google_alerts = (
        feed.version.startswith("atom")
        and _is_google_alerts_url(request["settings"]["IMPORTER_RSS_URL"])
        and _is_google_alerts_url(source_url)
    )
    records = []
    for entry in feed.entries:
        url = urljoin(source_url, entry.get("link", ""))
        parsed = urlsplit(url)
        if not entry.get("link") or parsed.scheme not in {"http", "https"}:
            continue
        if (
            google_alerts
            and parsed.scheme == "https"
            and parsed.netloc.lower() == "www.google.com"
            and parsed.path == "/url"
        ):
            # Google can return an HTML redirect page instead of an HTTP 3xx.
            # Extract the publisher locally without fetching the wrapper.
            url = _google_alerts_publisher(parsed)
            if url is None:
                continue
        tags = list(
            dict.fromkeys(
                unescape(category.get("term", "")).strip()
                for category in entry.get("tags", [])
                if isinstance(category.get("term"), str) and category["term"].strip()
            ),
        )
        records.append(
            {
                "id": item_id(entry.get("id") or url),
                "url": url,
                "title": unescape(entry.get("title", "")),
                "metadata": {
                    "feed_title": feed.feed.get("title", ""),
                    "published": entry.get("published", ""),
                    "relationship": "feed entry",
                    **({"tags": tags} if tags else {}),
                },
            },
        )
    emit_collection(
        request,
        records,
        message=f"Readable feed: {feed.feed.get('title', 'Untitled')}; {len(records)} article links.",
    )


if __name__ == "__main__":
    run(main)
