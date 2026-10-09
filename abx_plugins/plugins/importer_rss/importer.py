#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""RSS/Atom discovery command; JSON request on stdin, importer JSONL on stdout."""

from html import unescape
from urllib.parse import urljoin, urlsplit

import feedparser
from abx_plugins.plugins.base.importers import emit_collection, fetch, item_id, run


def main(request):
    content, _, source_url = fetch(
        request["settings"]["IMPORTER_RSS_URL"],
        authenticated=True,
    )
    feed = feedparser.parse(content)
    if not feed.version:
        raise ValueError(
            "The response is not an RSS or Atom feed. Check the feed URL and access.",
        )
    records = []
    for entry in feed.entries:
        url = urljoin(source_url, entry.get("link", ""))
        if not entry.get("link") or urlsplit(url).scheme not in {"http", "https"}:
            continue
        records.append(
            {
                "id": item_id(entry.get("id") or url),
                "url": url,
                "title": unescape(entry.get("title", "")),
                "metadata": {
                    "feed_title": feed.feed.get("title", ""),
                    "published": entry.get("published", ""),
                    "relationship": "feed entry",
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
