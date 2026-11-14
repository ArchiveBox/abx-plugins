"""Real RSS/Atom CLI parsing against static XML served over HTTP."""

import json
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from abx_plugins.plugins.base.importers import read_records


@pytest.mark.parametrize("kind", ["rss", "atom"])
@pytest.mark.parametrize("enabled", [None, False, True])
def test_categories_are_always_imported_as_tags(tmp_path, kind, enabled):
    if kind == "rss":
        xml = '<rss version="2.0"><channel><title>Reading list</title><item><guid>one</guid><title>First</title><link>https://example.com/first</link><category> Technology </category><category>Science &amp; Society</category><category>Technology</category><category> </category></item><item><guid>two</guid><link>https://example.com/second</link></item></channel></rss>'
    else:
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Reading list</title><entry><id>one</id><title>First</title><link href="https://example.com/first"/><category term=" Technology "/><category term="Science &amp; Society"/><category term="Technology"/><category term=" "/></entry><entry><id>two</id><link href="https://example.com/second"/></entry></feed>'
    (tmp_path / "feed.xml").write_text(xml)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=str(tmp_path)),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = {
        "version": 1,
        "action": "import",
        "feed": "feed",
        "limit": 10,
        "checkpoint": {},
        "settings": {
            "IMPORTER_RSS_URL": f"http://127.0.0.1:{server.server_port}/feed.xml",
        },
    }
    if enabled is not None:
        request["settings"]["IMPORTER_RSS_IMPORT_TAGS"] = enabled
    try:
        result = subprocess.run(
            [sys.executable, "-m", "abx_plugins.plugins.importer_rss.importer"],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            check=True,
        )
        records = list(read_records(result.stdout.splitlines(), request))
        assert records[-1]["status"] == "succeeded"
        assert len(records) == 3
        assert records[0]["url"] == "https://example.com/first"
        assert records[0]["metadata"].get("tags", []) == (
            ["Technology", "Science & Society"]
        )
        assert records[1]["metadata"].get("tags", []) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
