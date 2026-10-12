"""Hooks that fetch the snapshot URL with urllib must accept non-ASCII URLs.

http.client encodes the request line as ASCII, so passing an IRI such as
http://host/über straight to urlopen raises UnicodeEncodeError before any
request is sent. These run the real hooks against a local server and assert
both the hook result and the percent-encoded request the server received.
"""

import base64
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
        # Userinfo is encoded per component; the port is never touched.
        (
            "https://user:p%40ss@example.com:8443/über?q=1",
            "https://user:p%40ss@example.com:8443/%C3%BCber?q=1",
        ),
        (
            "https://usér:pä ss@münchen.de:8080/",
            "https://us%C3%A9r:p%C3%A4%20ss@xn--mnchen-3ya.de:8080/",
        ),
        # Browsers split userinfo at the last "@" and the first ":"; the "@"
        # and ":" left inside the password must be escaped to keep that split.
        ("https://user:p@ss:w@example.com/", "https://user:p%40ss%3Aw@example.com/"),
        # UTS #46 nontransitional mapping, as Chrome and Node use: ß and ς
        # stay distinct (IDNA 2003 would map straße to the different strasse).
        ("https://Straße.example/", "https://xn--strae-oqa.example/"),
        # Fullwidth letters and dots fold to the ASCII host they display as.
        ("http://ＥＸＡＭＰＬＥ．com/", "http://example.com/"),
    ],
)
def test_iri_to_uri(iri, uri):
    assert iri_to_uri(iri) == (uri or iri)


# (username, password) as typed in the URL, and the credentials they decode to.
CREDENTIALS = {
    "ascii-escaped": (("user", "p%40ss"), "user:p@ss"),
    "non-ascii": (("usér", "pä ss"), "usér:pä ss"),
}


def credentialed_url(httpserver, path: str, userinfo: tuple[str, str]) -> str:
    """URL with basic-auth userinfo on the server's random (non-default) port."""
    user, password = userinfo
    return f"http://{user}:{password}@{httpserver.host}:{httpserver.port}{path}"


def basic_auth(credentials: str) -> str:
    return "Basic " + base64.b64encode(credentials.encode()).decode()


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


@pytest.mark.parametrize("credentials", sorted(CREDENTIALS))
@pytest.mark.parametrize("plugin", sorted(SOURCES))
def test_url_parser_sends_url_credentials_on_non_default_port(
    tmp_path,
    httpserver,
    plugin,
    credentials,
):
    # urllib never turns URL userinfo into an Authorization header; it passed
    # "user:pw@host" to http.client as the hostname and failed to connect.
    userinfo, decoded = CREDENTIALS[credentials]
    httpserver.expect_request(
        "/privé/liste",
        headers={"Authorization": basic_auth(decoded)},
    ).respond_with_data(SOURCES[plugin], content_type="text/plain; charset=utf-8")
    result = run_hook(
        plugin,
        credentialed_url(httpserver, "/privé/liste", userinfo),
        tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert [r["url"] for r in records if r["type"] == "Snapshot"] == [LINKED_URL], (
        result.stdout + result.stderr
    )
    assert [
        (request.environ["REQUEST_URI"], request.headers.get("Authorization"))
        for request, _ in httpserver.log
    ] == [("/priv%C3%A9/liste", basic_auth(decoded))]


@pytest.mark.parametrize("credentials", sorted(CREDENTIALS))
def test_favicon_sends_url_credentials_to_page_and_icon(
    tmp_path,
    httpserver,
    credentials,
):
    userinfo, decoded = CREDENTIALS[credentials]
    auth = {"Authorization": basic_auth(decoded)}
    httpserver.expect_request("/über/", headers=auth).respond_with_data(
        '<html><head><link rel="icon" href="/ícono.png"></head></html>',
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/ícono.png", headers=auth).respond_with_data(
        TEST_ICON_BYTES,
        content_type="image/png",
    )
    result = run_hook(
        "favicon",
        credentialed_url(httpserver, "/über/", userinfo),
        tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["status"] == "succeeded", (
        result.stdout + result.stderr
    )
    assert (tmp_path / "favicon" / "favicon.ico").read_bytes() == TEST_ICON_BYTES
    assert [
        (request.environ["REQUEST_URI"], request.headers.get("Authorization"))
        for request, _ in httpserver.log
    ] == [
        ("/%C3%BCber/", basic_auth(decoded)),
        ("/%C3%ADcono.png", basic_auth(decoded)),
    ]


def test_url_credentials_are_not_forwarded_across_redirects(tmp_path, httpserver):
    # Basic auth belongs to the URL's own origin. A redirect elsewhere must not
    # carry it along (urllib copies ordinary headers onto redirected requests).
    from pytest_httpserver import HTTPServer

    with HTTPServer() as other:
        other.expect_request("/elsewhere").respond_with_data(
            SOURCES["parse_txt_urls"],
            content_type="text/plain; charset=utf-8",
        )
        httpserver.expect_request("/über").respond_with_data(
            "",
            status=302,
            headers={"Location": other.url_for("/elsewhere")},
        )
        result = run_hook(
            "parse_txt_urls",
            credentialed_url(httpserver, "/über", ("user", "secret")),
            tmp_path,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert [
            request.headers.get("Authorization") for request, _ in httpserver.log
        ] == [basic_auth("user:secret")]
        assert [request.headers.get("Authorization") for request, _ in other.log] == [
            None,
        ]


@pytest.mark.parametrize("credentials", sorted(CREDENTIALS))
def test_rss_importer_sends_url_credentials(tmp_path, httpserver, credentials):
    userinfo, decoded = CREDENTIALS[credentials]
    httpserver.expect_request(
        "/flux/über.xml",
        headers={"Authorization": basic_auth(decoded)},
    ).respond_with_data(
        SOURCES["parse_rss_urls"],
        content_type="application/rss+xml; charset=utf-8",
    )
    request = {
        "version": 1,
        "action": "preview",
        "limit": 10,
        "settings": {
            "IMPORTER_RSS_URL": credentialed_url(
                httpserver,
                "/flux/über.xml",
                userinfo,
            ),
        },
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
    assert records[-1]["status"] == "succeeded", records
    assert [
        (request.environ["REQUEST_URI"], request.headers.get("Authorization"))
        for request, _ in httpserver.log
    ] == [("/flux/%C3%BCber.xml", basic_auth(decoded))]


def run_wget(url: str, snap_dir: Path, **env: str) -> subprocess.CompletedProcess:
    from abx_plugins.plugins.base.testing import install_required_binary_from_config

    wget = install_required_binary_from_config(PLUGINS_DIR / "wget", "wget")
    hook = next((PLUGINS_DIR / "wget").glob("on_Snapshot__*_wget.*"))
    return subprocess.run(
        [str(hook), "--url", url],
        cwd=snap_dir,
        env={
            **os.environ,
            "SNAP_DIR": str(snap_dir),
            "WGET_BINARY": str(wget.loaded_abspath),
            "WGET_WARC_ENABLED": "False",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("credentials", sorted(CREDENTIALS))
def test_wget_reports_non_ascii_output_and_sends_credentials(
    tmp_path,
    httpserver,
    credentials,
):
    # wget runs under LC_ALL=C, where it prints saved filenames with octal
    # escapes ('\303\274ber.html'). The hook matched that text as a path, found
    # no such file, and reported noresults although the page was saved.
    userinfo, decoded = CREDENTIALS[credentials]
    httpserver.expect_request(
        "/über",
        headers={"Authorization": basic_auth(decoded)},
    ).respond_with_data(
        "<html><head><title>Über</title></head><body>Grüße</body></html>",
        content_type="text/html; charset=utf-8",
    )
    # wget only sends credentials after a 401 challenge, like a browser.
    httpserver.expect_request("/über").respond_with_data(
        "",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="test"'},
    )
    result = run_wget(credentialed_url(httpserver, "/über", userinfo), tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout.splitlines()[-1])
    saved = f"wget/{httpserver.host}+{httpserver.port}/über.html"
    assert record == {
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": saved,
    }, result.stdout + result.stderr
    assert "Grüße" in (tmp_path / saved).read_text(encoding="utf-8")
    assert {request.environ["REQUEST_URI"] for request, _ in httpserver.log} == {
        "/%C3%BCber",
    }


def test_wget_resolves_idn_hostname_as_idna(tmp_path, httpserver):
    # Under LC_ALL=C wget cannot tell the URL's charset, so it sent the Unicode
    # hostname percent-encoded (m%C3%BCnchen.example) instead of its IDNA name.
    # The local server acts as an HTTP proxy to observe the absolute request.
    httpserver.expect_request("/straße").respond_with_data(
        "<html><body>ok</body></html>",
        content_type="text/html",
    )
    result = run_wget(
        "http://münchen.example/straße",
        tmp_path,
        http_proxy=httpserver.url_for("/"),
        no_proxy="",
        NO_PROXY="",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [request.environ["REQUEST_URI"] for request, _ in httpserver.log] == [
        "http://xn--mnchen-3ya.example/stra%C3%9Fe",
    ]
    assert json.loads(result.stdout.splitlines()[-1])["status"] == "succeeded", (
        result.stdout + result.stderr
    )
