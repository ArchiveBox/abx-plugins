"""Real import hooks must agree on literal apostrophes and format delimiters."""

import csv
import io
import json
import os
import subprocess
from html import escape
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "abx_plugins" / "plugins"
URLS = [
    "https://aaib.gov.in/What's%20New%20Assets/Preliminary%20Report%20VT-EXO.pdf",
    "https://example.com/O'Reilly's",
    "https://example.com/?q='one'&other='two'",
    "https://example.com/dogs'",
    "https://example.com/two''",
    "https://example.com/?q=a,b&name=O'Reilly's",
    "https://example.com/What%27s%20new",
    "https://example.com/l’été",
    "https://example.com/What's_(new)",
]


@pytest.mark.parametrize(
    "plugin",
    ["parse_jsonl_urls", "parse_rss_urls", "parse_netscape_urls", "parse_html_urls"],
)
def test_structured_import_preserves_literal_apostrophes(tmp_path, plugin):
    if plugin == "parse_jsonl_urls":
        source = "\n".join(
            json.dumps({"url": url, "title": "A quoted URL"}) for url in URLS
        )
        suffix = ".jsonl"
    elif plugin == "parse_rss_urls":
        source = '<rss version="2.0"><channel><title>URLs</title>'
        source += "".join(
            f"<item><title>URL</title><link>{escape(url)}</link></item>" for url in URLS
        )
        source += "</channel></rss>"
        suffix = ".xml"
    else:
        source = "<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p>\n"
        source += "\n".join(
            f'<DT><A HREF="{escape(url, quote=True)}">URL</A>' for url in URLS
        )
        source += "\n</DL>"
        suffix = ".html"
    input_file = tmp_path / f"urls{suffix}"
    input_file.write_text(source)
    script = next((PLUGINS_DIR / plugin).glob("on_Snapshot__*.py"))
    result = subprocess.run(
        [str(script), "--url", input_file.as_uri()],
        cwd=tmp_path,
        env={**os.environ, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert {record["url"] for record in records if record["type"] == "Snapshot"} == set(
        URLS,
    )
    output = tmp_path / plugin / "urls.jsonl"
    assert {json.loads(line)["url"] for line in output.read_text().splitlines()} == set(
        URLS,
    )


@pytest.mark.parametrize("quotechar", ["'", '"'])
@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_csv_quoted_url_fields(tmp_path, quotechar, delimiter):
    source = io.StringIO()
    writer = csv.writer(
        source,
        quotechar=quotechar,
        delimiter=delimiter,
        quoting=csv.QUOTE_ALL,
    )
    writer.writerow(["title", "url", "notes"])
    for url in URLS:
        writer.writerow(
            ["Reader's link", url, "quoted, with punctuation; and apostrophes'"],
        )
    input_file = tmp_path / "urls.csv"
    input_file.write_text(source.getvalue())
    script = next((PLUGINS_DIR / "parse_txt_urls").glob("on_Snapshot__*.py"))
    result = subprocess.run(
        [str(script), "--url", input_file.as_uri()],
        cwd=tmp_path,
        env={**os.environ, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert {record["url"] for record in records if record["type"] == "Snapshot"} == set(
        URLS,
    )
    output = tmp_path / "parse_txt_urls" / "urls.jsonl"
    assert {json.loads(line)["url"] for line in output.read_text().splitlines()} == set(
        URLS,
    )
