"""Real importer subprocesses against Readwise's published document examples.

fixtures/reader-list.json is the LIST response from https://readwise.io/reader_api
(retrieved 2026-11-14; JSON trailing commas removed, document values unchanged).
HTTP tests serve that public example; they do not establish live account access.
reader-library.csv is unchanged from duncanlew/demo-go-csv-parser at
362b95782ac6676d70118a02edff3272bcf20207/example.csv, shared for reuse at
https://dev.to/duncanlew/a-guide-to-parsing-csv-files-in-go-4lp7.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from abx_plugins.plugins.base.importers import read_records
from abx_plugins.plugins.importer_readwise.importer import document_item

EXAMPLE = json.loads((Path(__file__).parent / "fixtures/reader-list.json").read_text())


def test_public_reader_csv_batches_and_dates(httpserver):
    content = (Path(__file__).parent / "fixtures/reader-library.csv").read_bytes()
    httpserver.expect_request("/export.csv").respond_with_data(
        content, content_type="text/csv"
    )
    request = {
        "version": 1,
        "action": "import",
        "feed": "csv",
        "limit": 2,
        "settings": {"READWISE_EXPORT_URL": httpserver.url_for("/export.csv")},
        "checkpoint": {},
    }
    imported = []
    for has_more, count in ((True, 2), (False, 2), (False, 0)):
        result = subprocess.run(
            [sys.executable, "-m", "abx_plugins.plugins.importer_readwise.importer"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            check=True,
        )
        records = list(read_records(result.stdout.splitlines(), request))
        assert records[-1]["status"] == "succeeded", records
        assert records[-1]["has_more"] is has_more
        assert len(records) == count + 1
        imported.extend(records[:-1])
        request["checkpoint"] = records[-1]["checkpoint"]
    assert len({record["id"] for record in imported}) == 4
    assert imported[0]["title"] == "Getting Started with Reader"
    assert (
        imported[0]["metadata"]["bookmarked_at"] == "2022-12-21 10:42:20.043000+00:00"
    )
    assert (
        imported[1]["url"]
        == "https://www.theverge.com/23496478/google-lens-apps-smart-home"
    )


def execute(httpserver, *, action="import", checkpoint=None):
    request = {
        "version": 1,
        "action": action,
        "feed": "documents",
        "limit": 100,
        "settings": {
            "READWISE_TOKEN": "test-token",
            "READWISE_LOCATION": "all",
            "READWISE_API_URL": httpserver.url_for("/list/"),
        },
        "checkpoint": checkpoint or {},
    }
    result = subprocess.run(
        [sys.executable, "-m", "abx_plugins.plugins.importer_readwise.importer"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    records = list(read_records(result.stdout.splitlines(), request))
    assert "test-token" not in result.stdout + result.stderr
    httpserver.check_assertions()
    return records


def test_pagination_and_incremental_checkpoint(httpserver):
    documents = EXAMPLE["results"]
    httpserver.expect_ordered_request(
        "/list/",
        query_string="limit=100",
        headers={"Authorization": "Token test-token"},
    ).respond_with_json(
        {**EXAMPLE, "results": documents[:2]},
    )
    first = execute(httpserver)
    assert [item["url"] for item in first[:-1]] == [
        item["source_url"] for item in documents[:2]
    ]
    assert first[0]["title"] == documents[0]["title"]
    assert first[0]["metadata"]["bookmarked_at"] == documents[0]["saved_at"]
    assert first[-1]["has_more"] is True
    checkpoint = first[-1]["checkpoint"]
    assert checkpoint["cursor"] == EXAMPLE["nextPageCursor"]
    httpserver.expect_ordered_request(
        "/list/", query_string={"limit": "100", "pageCursor": checkpoint["cursor"]}
    ).respond_with_json(
        {**EXAMPLE, "results": documents[2:], "nextPageCursor": None},
    )
    second = execute(httpserver, checkpoint=checkpoint)
    assert [item["url"] for item in second[:-1]] == [documents[2]["source_url"]]
    assert second[-1]["has_more"] is False
    completed = second[-1]["checkpoint"]
    assert "cursor" not in completed
    assert completed["updated_after"] == checkpoint["started_at"]
    httpserver.expect_ordered_request(
        "/list/",
        query_string={"limit": "100", "updatedAfter": completed["updated_after"]},
    ).respond_with_json(
        {"count": 0, "results": [], "nextPageCursor": None},
    )
    incremental = execute(httpserver, checkpoint=completed)
    assert len(incremental) == 1
    assert (
        incremental[0]["status"] == "succeeded" and incremental[0]["has_more"] is False
    )


@pytest.mark.parametrize("action", ["check", "preview"])
def test_read_only_actions_start_at_first_page(httpserver, action):
    httpserver.expect_request("/list/", query_string="limit=100").respond_with_json(
        {**EXAMPLE, "nextPageCursor": None}
    )
    records = execute(
        httpserver,
        action=action,
        checkpoint={"cursor": "unused", "updated_after": "unused"},
    )
    assert records[-1]["status"] == "succeeded"
    assert len(records) == (1 if action == "check" else 4)


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
def test_http_failure_never_commits_progress(httpserver, status, expected):
    httpserver.expect_request("/list/").respond_with_data(
        "", status=status, headers={"Location": httpserver.url_for("/must-not-follow")}
    )
    records = execute(httpserver)
    assert len(records) == 1
    assert records[0]["status"] == expected
    assert "checkpoint" not in records[0]


def test_malformed_page_never_reports_completion(httpserver):
    httpserver.expect_request("/list/").respond_with_json({"results": []})
    result = execute(httpserver)
    assert result[0]["status"] == "failed"
    assert "checkpoint" not in result[0]


def test_document_boundaries_and_non_web_documents():
    document = EXAMPLE["results"][0]
    # Exercise field boundaries without inventing provider responses.
    wrapped = "https://archive.today/2026.09.18-104122/" + document["source_url"]
    item = document_item(
        {**document, "source_url": wrapped, "tags": {"tech": {"name": "Technology"}}}
    )
    assert item is not None
    assert item["url"] == wrapped
    assert item["metadata"]["tags"] == ["Technology"]
    for changes in (
        {"parent_id": document["id"]},
        {"category": "note"},
        {"source_url": None},
        {"source_url": "mailto:reader-forwarded-email/123"},
    ):
        assert document_item({**document, **changes}) is None
