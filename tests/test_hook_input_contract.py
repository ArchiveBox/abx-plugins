"""Hooks consume explicit inputs, never the opaque JSONL reflection context."""

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

import pytest

PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins" / "plugins"


def test_hooks_do_not_inspect_extra_context():
    forbidden = re.compile(
        r"EXTRA_CONTEXT|_?get_extra_context|getExtraContext|extraContext",
    )
    violations = [
        str(path.relative_to(PLUGINS))
        for path in PLUGINS.glob("*/on_*.*")
        if path.suffix in {".py", ".js", ".sh"} and forbidden.search(path.read_text())
    ]
    assert violations == []


@pytest.mark.parametrize(("backend", "order"), [("sqlite", "90"), ("sonic", "91")])
def test_search_hooks_require_explicit_id_even_when_context_contains_one(
    tmp_path,
    backend,
    order,
):
    hook = (
        PLUGINS
        / f"search_backend_{backend}"
        / f"on_Snapshot__{order}_index_{backend}.py"
    )
    result = subprocess.run(
        [str(hook), "--url=https://example.com"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "DATA_DIR": str(tmp_path),
            "ABXPKG_LIB_DIR": str(tmp_path / "lib"),
            f"SEARCH_BACKEND_{backend.upper()}_ENABLED": "true",
            "EXTRA_CONTEXT": json.dumps({"snapshot_id": "not-an-input"}),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "missing --snapshot-id" in result.stderr
    assert "Installing sonic" not in result.stderr
    assert not (tmp_path / "search.sqlite3").exists()


@pytest.mark.parametrize(
    ("plugin", "content"),
    [
        ("parse_txt_urls", "https://example.com/child"),
        ("parse_html_urls", '<a href="https://example.com/child">child</a>'),
        ("parse_jsonl_urls", '{"url":"https://example.com/child"}\n'),
        (
            "parse_netscape_urls",
            '<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p><DT><A HREF="https://example.com/child">Child</A></DL>',
        ),
        (
            "parse_rss_urls",
            '<rss version="2.0"><channel><title>Feed</title><item><title>Child</title><link>https://example.com/child</link></item></channel></rss>',
        ),
    ],
)
def test_parsers_use_cli_depth_and_only_reflect_context(tmp_path, plugin, content):
    source = tmp_path / "input.txt"
    source.write_text(content)
    context = {"snapshot_depth": "not-an-integer", "correlation_id": ["opaque", 17]}
    hook = next((PLUGINS / plugin).glob("on_Snapshot__*.py"))
    result = subprocess.run(
        [str(hook), f"--url={source.as_uri()}", "--depth=4"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "EXTRA_CONTEXT": json.dumps(context),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    snapshots = [record for record in records if record["type"] == "Snapshot"]
    assert len(snapshots) == 1, result.stdout
    assert snapshots[0]["url"] == "https://example.com/child"
    assert snapshots[0]["depth"] == 5
    assert all(
        record["correlation_id"] == context["correlation_id"] for record in records
    )
    assert all(record["snapshot_depth"] == "not-an-integer" for record in records)


def test_sqlite_uses_cli_id_and_preserves_large_file_title(tmp_path):
    title = "界" * 70000 + " $(touch injected) `touch injected` ; title-end"
    title_path = tmp_path / "title" / "title.txt"
    title_path.parent.mkdir()
    title_path.write_text(title)
    result = subprocess.run(
        [
            str(PLUGINS / "search_backend_sqlite" / "on_Snapshot__90_index_sqlite.py"),
            "--url=https://example.com",
            "--snapshot-id=explicit-id",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "DATA_DIR": str(tmp_path),
            "SEARCH_BACKEND_SQLITE_ENABLED": "true",
            "ABX_RUNTIME": "archivebox",
            "EXTRA_CONTEXT": json.dumps(
                {"snapshot_id": "reflection-only", "trace_id": [1, 2]},
            ),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "search.sqlite3") as conn:
        assert conn.execute(
            "SELECT snapshot_id, title FROM search_index",
        ).fetchall() == [("explicit-id", title)]
    record = json.loads(result.stdout.splitlines()[-1])
    assert record["snapshot_id"] == "reflection-only"
    assert record["trace_id"] == [1, 2]
    assert title_path.read_text() == title
    assert not list(tmp_path.rglob("injected"))
