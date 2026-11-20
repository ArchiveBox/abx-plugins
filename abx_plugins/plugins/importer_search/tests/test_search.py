"""Real network calls, native JSONL emission, and CLI validation; no paid live calls.

The DDG fixture is two unchanged result anchors from a normal public GET for
'ArchiveBox web archiving', 2026-11-19. API envelopes match primary published
Kagi v1, Brave web search, and Exa search schemas; they are not live API proof.
"""

import json
import io
from contextlib import redirect_stdout
import os
import subprocess
import sys
from pathlib import Path

import pytest

from abx_plugins.plugins.base.importers import NeedsLogin, emit_collection, read_records
from abx_plugins.plugins.importer_search.importer import (
    fetch_results,
    parse_results,
    result_item,
)


def execute(feed, endpoint, *, action="import", checkpoint=None, limit=1):
    request = {
        "version": 1,
        "action": action,
        "feed": feed,
        "limit": limit,
        "settings": {"SEARCH_QUERY": "ArchiveBox"},
        "checkpoint": checkpoint or {},
    }
    records = fetch_results(feed, "ArchiveBox", "local-protocol-token", endpoint)
    output = io.StringIO()
    with redirect_stdout(output):
        emit_collection(request, records, message="Search results.")
    assert "local-protocol-token" not in output.getvalue()
    return list(read_records(output.getvalue().splitlines(), request))


@pytest.mark.parametrize("feed", ["kagi", "brave", "exa"])
def test_real_api_batches_checkpoint_and_dry_run(httpserver, feed):
    rows = [
        {"url": "https://archivebox.io/#about", "title": "ArchiveBox"},
        {"url": "https://docs.archivebox.io/", "title": "Documentation"},
    ]
    payload = {
        "kagi": {"data": {"search": rows}},
        "brave": {"web": {"results": rows}},
        "exa": {"results": rows},
    }[feed]
    if feed == "brave":
        expectation = httpserver.expect_request(
            "/search",
            method="GET",
            query_string={"q": "ArchiveBox", "count": "20", "result_filter": "web"},
            headers={"X-Subscription-Token": "local-protocol-token"},
        )
    else:
        body = (
            {"query": "ArchiveBox", "workflow": "search"}
            if feed == "kagi"
            else {"query": "ArchiveBox", "type": "auto", "numResults": 20}
        )
        headers = (
            {"Authorization": "Bearer local-protocol-token"}
            if feed == "kagi"
            else {"x-api-key": "local-protocol-token"}
        )
        expectation = httpserver.expect_request(
            "/search",
            method="POST",
            json=body,
            headers=headers,
        )
    expectation.respond_with_json(payload)
    endpoint = httpserver.url_for("/search")
    first = execute(feed, endpoint)
    assert first[0]["url"] == "https://archivebox.io/"
    assert first[-1]["has_more"] is True
    second = execute(feed, endpoint, checkpoint=first[-1]["checkpoint"])
    assert second[0]["url"] == "https://docs.archivebox.io/"
    assert second[-1]["has_more"] is False
    repeated = execute(feed, endpoint, checkpoint=second[-1]["checkpoint"])
    assert len(repeated) == 1 and repeated[-1]["status"] == "succeeded"
    dry = execute(feed, endpoint, action="preview", checkpoint=second[-1]["checkpoint"])
    assert dry[0]["url"] == first[0]["url"]
    check = execute(feed, endpoint, action="check")
    assert len(check) == 1 and check[-1]["status"] == "succeeded"
    httpserver.check_assertions()


def test_real_public_duckduckgo_markup_through_http(httpserver):
    content = (Path(__file__).parent / "fixtures/duckduckgo.html").read_bytes()
    httpserver.expect_request(
        "/html/",
        query_string={"q": "ArchiveBox"},
    ).respond_with_data(content, content_type="text/html")
    records = execute("duckduckgo", httpserver.url_for("/html/"), limit=20)
    assert records[-1]["status"] == "succeeded"
    assert len(records) == 3
    assert any(row.get("url") == "https://archivebox.io/" for row in records)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "needs_login"),
        (403, "needs_login"),
        (429, "failed"),
        (500, "failed"),
        (302, "failed"),
    ],
)
def test_http_errors_do_not_advance_or_leak(httpserver, status, expected):
    httpserver.expect_request("/search").respond_with_data(
        "private-provider-error",
        status=status,
        headers={"Location": httpserver.url_for("/forbidden")},
    )
    error_type = NeedsLogin if expected == "needs_login" else ValueError
    with pytest.raises(error_type) as error:
        fetch_results(
            "brave",
            "ArchiveBox",
            "local-protocol-token",
            httpserver.url_for("/search"),
        )
    assert "private-provider-error" not in str(error.value)
    assert "local-protocol-token" not in str(error.value)
    httpserver.check_assertions()


@pytest.mark.parametrize(
    "page",
    [
        b"<html>Consent required</html>",
        b'<form id="challenge-form"></form>',
        b"<html>Enable JavaScript</html>",
    ],
)
def test_interstitial_is_not_empty_success(page):
    with pytest.raises(ValueError, match="recognized"):
        parse_results("duckduckgo", page)


def test_url_identity_and_invalid_api_shape():
    one = result_item("https://example.org/a?q=1#one", "A")
    two = result_item("https://example.org/a?q=1#two", "A")
    other = result_item("https://example.org/a?q=2", "A")
    assert one is not None and two is not None and other is not None
    assert one["id"] == two["id"]
    assert one["id"] != other["id"]
    for url in [
        "javascript:alert(1)",
        "https://user:pass@example.org/",
        "file:///tmp/a",
    ]:
        assert result_item(url, "A") is None
    with pytest.raises(ValueError, match="invalid results"):
        parse_results("kagi", b'{"data": []}')


def test_catalog_secret_fields_are_not_schedule_requirements():
    config = json.loads((Path(__file__).parents[1] / "config.json").read_text())
    assert list(config["importers"]) == ["kagi", "brave", "duckduckgo", "exa"]
    for feed in config["importers"].values():
        assert feed["provider"] == "Search"
        assert feed["config_fields"] == ["SEARCH_QUERY"]
    for provider in ["KAGI", "BRAVE", "EXA"]:
        field = config["properties"][provider + "_SEARCH_API_KEY"]
        assert field["x-sensitive"] is True
        assert field["format"] == "password" and field["default"] == ""
        assert not field.get("x-required")


@pytest.mark.parametrize(
    "feed,payload",
    [
        ("kagi", {"data": {"search": []}}),
        ("brave", {"web": {"results": []}}),
        ("exa", {"results": []}),
    ],
)
def test_explicit_empty_api_results_are_success(httpserver, feed, payload):
    httpserver.expect_request("/search").respond_with_json(payload)
    records = execute(feed, httpserver.url_for("/search"))
    assert len(records) == 1
    assert records[0]["status"] == "succeeded"
    assert records[0]["has_more"] is False


def test_cli_missing_key_cannot_use_source_endpoint(httpserver):
    request = {
        "version": 1,
        "action": "import",
        "feed": "brave",
        "limit": 20,
        "settings": {
            "SEARCH_QUERY": "ArchiveBox",
            "SEARCH_API_URL": httpserver.url_for("/must-not-call"),
        },
        "checkpoint": {},
    }
    env = {**os.environ, "BRAVE_SEARCH_API_KEY": ""}
    result = subprocess.run(
        [sys.executable, "-m", "abx_plugins.plugins.importer_search.importer"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    records = list(read_records(result.stdout.splitlines(), request))
    assert len(records) == 1 and records[0]["status"] == "needs_login"
    assert "checkpoint" not in records[0]
    assert httpserver.log == []
    config = json.loads((Path(__file__).parents[1] / "config.json").read_text())
    assert "SEARCH_API_URL" not in config["properties"]


@pytest.mark.parametrize(
    "status,expected",
    [
        (
            401,
            "Kagi rejected the Search API key (HTTP 401). Check the key in Search plugin settings.",
        ),
        (
            403,
            "Kagi denied access (HTTP 403). Check whether the key allows this server's IP address.",
        ),
    ],
)
def test_kagi_auth_error_distinguishes_credentials_and_allowed_ip(
    httpserver,
    status,
    expected,
):
    httpserver.expect_request(
        "/search",
        method="POST",
        headers={"Authorization": "Bearer local-protocol-token"},
    ).respond_with_data(
        "private-provider-error",
        status=status,
        headers={"X-Request-ID": "private-provider-header"},
    )
    with pytest.raises(NeedsLogin) as error:
        fetch_results(
            "kagi",
            "ArchiveBox",
            "local-protocol-token",
            httpserver.url_for("/search"),
        )
    assert str(error.value) == expected
    httpserver.check_assertions()
