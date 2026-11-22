"""Synthetic Google Alerts Atom input through the real feed importer pipeline."""

from html import escape
from urllib.parse import urlencode

import pytest

from abx_plugins.plugins.base.importers import item_id, read_records
from abx_plugins.plugins.importer_rss.importer import import_feed

ALERTS_URL = "https://www.google.com/alerts/feeds/synthetic-account/synthetic-alert"
PUBLISHER_URL = "https://example.org/story?part=1&lang=en#section"


def wrapper(*params):
    return f"https://www.google.com/url?{urlencode(params)}"


def poll(capsys, entries, *, configured=ALERTS_URL, source=ALERTS_URL, checkpoint=None):
    request = {
        "version": 1,
        "action": "import",
        "feed": "feed",
        "checkpoint": checkpoint or {},
        "limit": 50,
        "settings": {"IMPORTER_RSS_URL": configured},
    }
    xml = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Alerts</title>'
    for entry_id, url in entries:
        identity = f"<id>{entry_id}</id>" if entry_id else ""
        xml += f'<entry>{identity}<title>Publisher story</title><link href="{escape(url, quote=True)}" /></entry>'
    import_feed(request, (xml + "</feed>").encode(), source)
    records = list(read_records(capsys.readouterr().out.splitlines(), request))
    assert records[-1]["status"] == "succeeded"
    return records[:-1], records[-1]["checkpoint"]


@pytest.mark.parametrize("entry_id", ["tag:google.com,2005:reader/item/synthetic", ""])
def test_google_alerts_uses_publisher_url_with_stable_checkpoint(capsys, entry_id):
    first = wrapper(("url", PUBLISHER_URL), ("ct", "first"))
    items, checkpoint = poll(capsys, [(entry_id, first)])
    assert [(item["url"], item["id"]) for item in items] == [
        (PUBLISHER_URL, item_id(entry_id or PUBLISHER_URL)),
    ]

    updated_tracking = wrapper(("ct", "second"), ("url", PUBLISHER_URL))
    entries = [(entry_id, updated_tracking)]
    items, next_checkpoint = poll(capsys, entries, checkpoint=checkpoint)
    assert items == []
    assert next_checkpoint == checkpoint


def test_google_alerts_skips_invalid_wrappers_and_preserves_direct_urls(capsys):
    bad = [
        wrapper(("ct", "missing")),
        wrapper(("q", PUBLISHER_URL)),
        wrapper(("url", "")),
        wrapper(("url", PUBLISHER_URL), ("url", "")),
        wrapper(("url", PUBLISHER_URL), ("url", PUBLISHER_URL)),
        wrapper(("url", "javascript:alert(1)")),
        wrapper(("url", "//example.org/story")),
        wrapper(("url", "https:///missing-host")),
        wrapper(("url", "https://[invalid")),
        wrapper(("url", "https://example.org:invalid/story")),
        wrapper(("url", wrapper(("url", PUBLISHER_URL)))),
    ]
    items, _ = poll(capsys, [*(("", url) for url in bad), ("direct", PUBLISHER_URL)])
    assert [item["url"] for item in items] == [PUBLISHER_URL]


@pytest.mark.parametrize(
    ("configured", "source"),
    [
        ("https://example.org/feed", "https://example.org/feed"),
        ("https://www.google.com.example.org/alerts/feeds/account/alert",) * 2,
        ("https://www.google.com/feed/",) * 2,
        ("https://example.org/feed", ALERTS_URL),
        (ALERTS_URL, "https://example.org/feed"),
    ],
)
def test_other_feeds_leave_google_links_unchanged(capsys, configured, source):
    link = wrapper(("url", PUBLISHER_URL))
    items, _ = poll(capsys, [("one", link)], configured=configured, source=source)
    assert [item["url"] for item in items] == [link]


def test_rss_at_google_alerts_path_is_unchanged(capsys):
    link = wrapper(("url", PUBLISHER_URL))
    request = {
        "version": 1,
        "action": "import",
        "feed": "feed",
        "checkpoint": {},
        "limit": 1,
        "settings": {"IMPORTER_RSS_URL": ALERTS_URL},
    }
    xml = (
        '<rss version="2.0"><channel><title>Feed</title>'
        f"<item><guid>story</guid><link>{escape(link)}</link></item></channel></rss>"
    )
    import_feed(request, xml.encode(), ALERTS_URL)
    records = list(read_records(capsys.readouterr().out.splitlines(), request))
    assert records[0]["url"] == link
    assert records[-1]["status"] == "succeeded"
