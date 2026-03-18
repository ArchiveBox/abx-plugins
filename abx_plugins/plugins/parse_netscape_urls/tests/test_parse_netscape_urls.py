#!/usr/bin/env python3
"""Unit tests for parse_netscape_urls extractor."""

import json
import os
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
SCRIPT_PATH = next(PLUGIN_DIR.glob("on_Snapshot__*_parse_netscape_urls.*"), None)


class TestParseNetscapeUrls:
    """Test the parse_netscape_urls extractor CLI."""

    def test_extracts_urls_from_netscape_bookmarks(self, tmp_path):
        """Test extracting URLs from Netscape bookmark HTML format."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><A HREF="https://example.com" ADD_DATE="1609459200">Example Site</A>
    <DT><A HREF="https://foo.bar/page" ADD_DATE="1609545600">Foo Bar</A>
    <DT><A HREF="https://test.org" ADD_DATE="1609632000">Test Org</A>
</DL><p>
        """)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "URLs parsed" in result.stderr or "URLs parsed" in result.stdout

        # Output goes to stdout (JSONL)
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip() and '"type": "Snapshot"' in line
        ]
        assert len(lines) == 3

        entries = [json.loads(line) for line in lines]
        urls = {e["url"] for e in entries}
        titles = {e.get("title") for e in entries}

        assert "https://example.com" in urls
        assert "https://foo.bar/page" in urls
        assert "https://test.org" in urls
        assert "Example Site" in titles
        assert "Foo Bar" in titles
        assert "Test Org" in titles

    def test_parses_add_date_timestamps(self, tmp_path):
        """Test that ADD_DATE timestamps are parsed correctly."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""
<DT><A HREF="https://example.com" ADD_DATE="1609459200">Test</A>
        """)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # Output goes to stdout (JSONL)
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        entry = json.loads(lines[0])
        # Parser converts timestamp to bookmarked_at
        assert "bookmarked_at" in entry

    def test_handles_query_params_in_urls(self, tmp_path):
        """Test that URLs with query parameters are preserved."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""
<DT><A HREF="https://example.com/search?q=test+query&page=1" ADD_DATE="1609459200">Search</A>
        """)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # Output goes to stdout (JSONL)
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        entry = json.loads(lines[0])
        assert "q=test+query" in entry["url"]
        assert "page=1" in entry["url"]

    def test_handles_html_entities(self, tmp_path):
        """Test that HTML entities in URLs and titles are decoded."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""
<DT><A HREF="https://example.com/page?a=1&amp;b=2" ADD_DATE="1609459200">Test &amp; Title</A>
        """)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # Output goes to stdout (JSONL)
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        entry = json.loads(lines[0])
        assert entry["url"] == "https://example.com/page?a=1&b=2"
        assert entry["title"] == "Test & Title"

    def test_resolves_relative_urls_against_source_page_url(self, tmp_path):
        """Test that relative bookmark HREFs are emitted as absolute URLs."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""
<DT><A HREF="/about.html" ADD_DATE="1609459200">About</A>
<DT><A HREF="docs/page.html" ADD_DATE="1609459200">Docs</A>
        """)

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args, **kwargs: QuietHandler(*args, directory=str(tmp_path), **kwargs))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/bookmarks.html"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--url",
                    base_url,
                ],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "SNAP_DIR": str(tmp_path),
                },
            )
        finally:
            server.shutdown()
            server.server_close()

        assert result.returncode == 0
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip() and '"type": "Snapshot"' in line
        ]
        assert len(lines) == 2

        entries = [json.loads(line) for line in lines]
        expected_root_relative = f"http://127.0.0.1:{server.server_address[1]}/about.html"
        expected_path_relative = (
            f"http://127.0.0.1:{server.server_address[1]}/docs/page.html"
        )
        assert entries[0]["url"] == expected_root_relative
        assert entries[1]["url"] == expected_path_relative

        urls_file = tmp_path / "parse_netscape_urls" / "urls.jsonl"
        file_entries = [json.loads(line) for line in urls_file.read_text().splitlines()]
        assert file_entries[0]["url"] == expected_root_relative
        assert file_entries[1]["url"] == expected_path_relative

    def test_skips_when_no_bookmarks_found(self, tmp_path):
        """Test that script succeeds without output when no bookmarks are found."""
        input_file = tmp_path / "empty.html"
        input_file.write_text("""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
</DL><p>
        """)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert '"status": "noresults"' in result.stdout
        assert '"output_str": "0 URLs parsed"' in result.stdout
        assert "0 URLs parsed" in result.stderr
        assert not (tmp_path / "parse_netscape_urls" / "urls.jsonl").exists()

    def test_exits_1_when_file_not_found(self, tmp_path):
        """Test that script exits with code 1 when file doesn't exist."""
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--url",
                "file:///nonexistent/bookmarks.html",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "Failed to fetch" in result.stderr
        assert '"status": "failed"' in result.stdout

    def test_handles_nested_folders(self, tmp_path):
        """Test parsing bookmarks in nested folder structure."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
    <DT><H3>Folder 1</H3>
    <DL><p>
        <DT><A HREF="https://example.com/nested1" ADD_DATE="1609459200">Nested 1</A>
        <DT><H3>Subfolder</H3>
        <DL><p>
            <DT><A HREF="https://example.com/nested2" ADD_DATE="1609459200">Nested 2</A>
        </DL><p>
    </DL><p>
    <DT><A HREF="https://example.com/top" ADD_DATE="1609459200">Top Level</A>
</DL><p>
        """)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # Output goes to stdout (JSONL)
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip() and '"type": "Snapshot"' in line
        ]
        urls = {json.loads(line)["url"] for line in lines}

        assert "https://example.com/nested1" in urls
        assert "https://example.com/nested2" in urls
        assert "https://example.com/top" in urls

    def test_case_insensitive_parsing(self, tmp_path):
        """Test that parsing is case-insensitive for HTML tags."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""
<dt><a HREF="https://example.com" ADD_DATE="1609459200">Test</a>
        """)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # Output goes to stdout (JSONL)
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        entry = json.loads(lines[0])
        assert entry["url"] == "https://example.com"

    def test_overwrites_stale_urls_file_on_rerun(self, tmp_path):
        """Test that reruns overwrite stale parser output instead of skipping."""
        input_file = tmp_path / "bookmarks.html"
        input_file.write_text("""
<DT><A HREF="https://fresh.example.com" ADD_DATE="1609459200">Fresh</A>
        """)

        urls_dir = tmp_path / "parse_netscape_urls"
        urls_dir.mkdir(parents=True, exist_ok=True)
        urls_file = urls_dir / "urls.jsonl"
        urls_file.write_text('{"type":"Snapshot","url":"https://stale.example.com"}\n')
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmp_path)

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0
        file_lines = [line for line in urls_file.read_text().splitlines() if line.strip()]
        assert len(file_lines) == 1
        entry = json.loads(file_lines[0])
        assert entry["url"] == "https://fresh.example.com"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
