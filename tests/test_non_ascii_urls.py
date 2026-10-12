"""Hooks that fetch the snapshot URL with urllib must accept non-ASCII URLs.

http.client encodes the request line as ASCII, so passing an IRI such as
http://host/über straight to urlopen raises UnicodeEncodeError before any
request is sent. These run the real hooks against a local server and assert
both the hook result and the percent-encoded request the server received.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.utils import iri_to_uri

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "abx_plugins" / "plugins"
LINKED_URL = "https://example.com/linked"
SOURCES = {
    "parse_html_urls": f'<html><body><a href="{LINKED_URL}">Linked</a></body></html>',
    "parse_txt_urls": f"{LINKED_URL}\n",
    "parse_jsonl_urls": json.dumps({"url": LINKED_URL, "title": "Linked"}),
    "parse_rss_urls": (
        '<rss version="2.0"><channel><title>URLs</title>'
        f"<item><title>Linked</title><link>{LINKED_URL}</link></item>"
        "</channel></rss>"
    ),
    "parse_netscape_urls": (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p>\n"
        f'<DT><A HREF="{LINKED_URL}">Linked</A>\n</DL>'
    ),
}
TEST_ICON_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0000004010100f7ff650000000049454e44ae426082",
)


@pytest.mark.parametrize(
    ("iri", "uri"),
    [
        # Already-valid URIs, including existing escapes, must not change.
        ("https://example.com/a%20b?x=%C3%BC&y=O'Reilly#f", None),
        ("https://user:pw@[::1]:8080/p;q?r=s", None),
        (
            "https://example.com/l’été?q=ü#ß",
            "https://example.com/l%E2%80%99%C3%A9t%C3%A9?q=%C3%BC#%C3%9F",
        ),
        (
            "https://usér@Bücher.example:8443/",
            "https://us%C3%A9r@xn--bcher-kva.example:8443/",
        ),
        ("http://例え.テスト/パス", "http://xn--r8jz45g.xn--zckzah/%E3%83%91%E3%82%B9"),
    ],
)
def test_iri_to_uri(iri, uri):
    assert iri_to_uri(iri) == (uri or iri)


def run_hook(plugin: str, url: str, snap_dir: Path) -> subprocess.CompletedProcess:
    script = next((PLUGINS_DIR / plugin).glob("on_Snapshot__*.py"))
    return subprocess.run(
        [str(script), "--url", url],
        cwd=snap_dir,
        env={**os.environ, "SNAP_DIR": str(snap_dir)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("plugin", sorted(SOURCES))
def test_url_parser_fetches_non_ascii_source_url(tmp_path, httpserver, plugin):
    httpserver.expect_request("/über/quelle ü").respond_with_data(
        SOURCES[plugin],
        content_type="text/plain; charset=utf-8",
    )
    result = run_hook(plugin, httpserver.url_for("/über/quelle ü"), tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert [r["url"] for r in records if r["type"] == "Snapshot"] == [LINKED_URL], (
        result.stdout + result.stderr
    )
    assert [r["status"] for r in records if r["type"] == "ArchiveResult"] == [
        "succeeded",
    ], result.stdout + result.stderr
    saved = tmp_path / plugin / "urls.jsonl"
    assert [json.loads(line)["url"] for line in saved.read_text().splitlines()] == [
        LINKED_URL,
    ]
    assert [request.environ["REQUEST_URI"] for request, _ in httpserver.log] == [
        "/%C3%BCber/quelle%20%C3%BC",
    ]


def test_favicon_fetches_non_ascii_page_and_icon_urls(tmp_path, httpserver):
    httpserver.expect_request("/über/").respond_with_data(
        '<html><head><link rel="icon" href="/ícono.png"></head></html>',
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/ícono.png").respond_with_data(
        TEST_ICON_BYTES,
        content_type="image/png",
    )
    result = run_hook("favicon", httpserver.url_for("/über/"), tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "favicon/favicon.ico",
    }, result.stdout + result.stderr
    assert (tmp_path / "favicon" / "favicon.ico").read_bytes() == TEST_ICON_BYTES
    assert [request.environ["REQUEST_URI"] for request, _ in httpserver.log] == [
        "/%C3%BCber/",
        "/%C3%ADcono.png",
    ]


def test_rss_importer_fetches_non_ascii_feed_url(tmp_path, httpserver):
    httpserver.expect_request("/flux/über.xml").respond_with_data(
        SOURCES["parse_rss_urls"],
        content_type="application/rss+xml; charset=utf-8",
    )
    request = {
        "version": 1,
        "action": "preview",
        "limit": 10,
        "settings": {"IMPORTER_RSS_URL": httpserver.url_for("/flux/über.xml")},
    }
    result = subprocess.run(
        [str(PLUGINS_DIR / "importer_rss" / "importer.py")],
        input=json.dumps(request),
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["url"] for r in records if r["type"] == "ImporterItem"] == [LINKED_URL]
    assert records[-1]["type"] == "ImporterResult", records
    assert records[-1]["status"] == "succeeded", records
    assert [request.environ["REQUEST_URI"] for request, _ in httpserver.log] == [
        "/flux/%C3%BCber.xml",
    ]
