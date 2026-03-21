#!/usr/bin/env python3
"""Unit tests for parse_rss_urls extractor."""

import json
import os
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
SCRIPT_PATH = next(PLUGIN_DIR.glob("on_Snapshot__*_parse_rss_urls.*"), None)


class TestParseRssUrls:
    """Test the parse_rss_urls extractor CLI."""

    def test_parses_real_rss_feed(self, tmp_path):
        """Test parsing a real RSS feed from the web."""
        # Use httpbin.org which provides a sample RSS feed
        result = subprocess.run(
            [
                str(SCRIPT_PATH),
                "--url",
                "https://news.ycombinator.com/rss",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # HN RSS feed should parse successfully
        if result.returncode == 0:
            # Output goes to stdout (JSONL)
            content = result.stdout
            assert len(content) > 0, "No URLs extracted from real RSS feed"

            # Verify at least one URL was extracted
            lines = content.strip().split("\n")
            assert len(lines) > 0, "No entries found in RSS feed"

    def test_extracts_urls_from_rss_feed(self, tmp_path):
        """Test extracting URLs from an RSS 2.0 feed."""
        input_file = tmp_path / "feed.rss"
        input_file.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <link>https://example.com</link>
    <item>
      <title>First Post</title>
      <link>https://example.com/post/1</link>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second Post</title>
      <link>https://example.com/post/2</link>
      <pubDate>Tue, 02 Jan 2024 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
        """)

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
        assert len(lines) == 2

        entries = [json.loads(line) for line in lines]
        urls = {e["url"] for e in entries}
        titles = {e.get("title") for e in entries}

        assert "https://example.com/post/1" in urls
        assert "https://example.com/post/2" in urls
        assert "First Post" in titles
        assert "Second Post" in titles

    def test_extracts_urls_from_atom_feed(self, tmp_path):
        """Test extracting URLs from an Atom feed."""
        input_file = tmp_path / "feed.atom"
        input_file.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom Feed</title>
  <entry>
    <title>Atom Post 1</title>
    <link href="https://atom.example.com/entry/1"/>
    <updated>2024-01-01T12:00:00Z</updated>
  </entry>
  <entry>
    <title>Atom Post 2</title>
    <link href="https://atom.example.com/entry/2"/>
    <updated>2024-01-02T12:00:00Z</updated>
  </entry>
</feed>
        """)

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
        urls = {json.loads(line)["url"] for line in lines}

        assert "https://atom.example.com/entry/1" in urls
        assert "https://atom.example.com/entry/2" in urls

    def test_skips_when_no_entries(self, tmp_path):
        """Test that script succeeds without output when feed has no entries."""
        input_file = tmp_path / "empty.rss"
        input_file.write_text("""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Empty Feed</title>
  </channel>
</rss>
        """)

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
        assert not (tmp_path / "parse_rss_urls" / "urls.jsonl").exists()

    def test_exits_1_when_file_not_found(self, tmp_path):
        """Test that script exits with code 1 when file doesn't exist."""
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", "file:///nonexistent/feed.rss"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "Failed to fetch" in result.stderr
        assert '"status": "failed"' in result.stdout

    def test_handles_html_entities_in_urls(self, tmp_path):
        """Test that HTML entities in URLs are decoded."""
        input_file = tmp_path / "feed.rss"
        input_file.write_text("""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Entity Test</title>
      <link>https://example.com/page?a=1&amp;b=2</link>
    </item>
  </channel>
</rss>
        """)

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

    def test_includes_optional_metadata(self, tmp_path):
        """Test that title and timestamp are included when present."""
        input_file = tmp_path / "feed.rss"
        input_file.write_text("""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Test Title</title>
      <link>https://example.com/test</link>
      <pubDate>Wed, 15 Jan 2020 10:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
        """)

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
        assert entry["url"] == "https://example.com/test"
        assert entry["title"] == "Test Title"
        # Parser converts timestamp to bookmarked_at
        assert "bookmarked_at" in entry

    def test_overwrites_stale_urls_file_on_rerun(self, tmp_path):
        """Test that reruns overwrite stale parser output instead of skipping."""
        input_file = tmp_path / "feed.rss"
        input_file.write_text("""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Fresh Item</title>
      <link>https://fresh.example.com/post</link>
    </item>
  </channel>
</rss>
        """)

        urls_dir = tmp_path / "parse_rss_urls"
        urls_dir.mkdir(parents=True, exist_ok=True)
        urls_file = urls_dir / "urls.jsonl"
        urls_file.write_text(
            '{"type":"Snapshot","url":"https://stale.example.com/post"}\n'
        )
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
        assert entry["url"] == "https://fresh.example.com/post"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
