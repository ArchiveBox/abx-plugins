#!/usr/bin/env python3
"""Unit tests for parse_jsonl_urls extractor."""

import json
import os
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
SCRIPT_PATH = next(PLUGIN_DIR.glob("on_Snapshot__*_parse_jsonl_urls.*"), None)


class TestParseJsonlUrls:
    """Test the parse_jsonl_urls extractor CLI."""

    def test_extracts_urls_from_jsonl(self, tmp_path):
        """Test extracting URLs from JSONL bookmark file."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com", "title": "Example"}\n'
            '{"url": "https://foo.bar/page", "title": "Foo Bar"}\n'
            '{"url": "https://test.org", "title": "Test Org"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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
        assert "Example" in titles
        assert "Foo Bar" in titles
        assert "Test Org" in titles

    def test_supports_href_field(self, tmp_path):
        """Test that 'href' field is recognized as URL."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text('{"href": "https://example.com", "title": "Test"}\n')

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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

    def test_trims_trailing_quote_garbage_in_url_fields(self, tmp_path):
        """Malformed exported URL fields should stop at quote garbage."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com\\"><img", "title": "Broken"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        entry = json.loads(lines[0])
        assert entry["url"] == "https://example.com"

    def test_supports_description_as_title(self, tmp_path):
        """Test that 'description' field is used as title fallback."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com", "description": "A description"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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
        assert entry["title"] == "A description"

    def test_parses_various_timestamp_formats(self, tmp_path):
        """Test parsing of different timestamp field names."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com", "timestamp": 1609459200000000}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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

    def test_parses_tags_as_string(self, tmp_path):
        """Test parsing tags as comma-separated string."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com", "tags": "tech,news,reading"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # Output goes to stdout (JSONL)
        # Parser converts tags to separate Tag objects in the output
        content = result.stdout
        assert "tech" in content or "news" in content or "Tag" in content

    def test_parses_tags_as_list(self, tmp_path):
        """Test parsing tags as JSON array."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com", "tags": ["tech", "news"]}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        # Output goes to stdout (JSONL)
        # Parser converts tags to separate Tag objects in the output
        content = result.stdout
        assert "tech" in content or "news" in content or "Tag" in content

    def test_skips_malformed_lines(self, tmp_path):
        """Test that malformed JSON lines are skipped."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://valid.com"}\n'
            "not valid json\n"
            '{"url": "https://also-valid.com"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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
        assert len(lines) == 2

    def test_skips_entries_without_url(self, tmp_path):
        """Test that entries without URL field are skipped."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://valid.com"}\n'
            '{"title": "No URL here"}\n'
            '{"url": "https://also-valid.com"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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
        assert len(lines) == 2

    def test_skips_when_no_urls_found(self, tmp_path):
        """Test that script succeeds without output when no URLs are found."""
        input_file = tmp_path / "empty.jsonl"
        input_file.write_text('{"title": "No URL"}\n')

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert '"status": "noresults"' in result.stdout
        assert '"output_str": "0 URLs parsed"' in result.stdout
        assert "0 URLs parsed" in result.stderr
        assert not (tmp_path / "parse_jsonl_urls" / "urls.jsonl").exists()

    def test_exits_1_when_file_not_found(self, tmp_path):
        """Test that script exits with code 1 when file doesn't exist."""
        result = subprocess.run(
            [
                str(SCRIPT_PATH),
                "--url",
                "file:///nonexistent/bookmarks.jsonl",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "Failed to fetch" in result.stderr
        assert '"status": "failed"' in result.stdout

    def test_handles_html_entities(self, tmp_path):
        """Test that HTML entities in URLs and titles are decoded."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com/page?a=1&amp;b=2", "title": "Test &amp; Title"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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

    def test_skips_empty_lines(self, tmp_path):
        """Test that empty lines are skipped."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://example.com"}\n\n   \n{"url": "https://other.com"}\n',
        )

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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
        assert len(lines) == 2

    def test_output_includes_required_fields(self, tmp_path):
        """Test that output includes required fields."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text('{"url": "https://example.com"}\n')

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
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
        assert "type" in entry
        assert "plugin" in entry

    def test_overwrites_stale_urls_file_on_rerun(self, tmp_path):
        """Test that reruns overwrite stale parser output instead of skipping."""
        input_file = tmp_path / "bookmarks.jsonl"
        input_file.write_text(
            '{"url": "https://fresh.example.com", "title": "Fresh"}\n',
        )

        urls_dir = tmp_path / "parse_jsonl_urls"
        urls_dir.mkdir(parents=True, exist_ok=True)
        urls_file = urls_dir / "urls.jsonl"
        urls_file.write_text('{"type":"Snapshot","url":"https://stale.example.com"}\n')
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmp_path)

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", f"file://{input_file}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0
        file_lines = [
            line for line in urls_file.read_text().splitlines() if line.strip()
        ]
        assert len(file_lines) == 1
        entry = json.loads(file_lines[0])
        assert entry["url"] == "https://fresh.example.com"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
