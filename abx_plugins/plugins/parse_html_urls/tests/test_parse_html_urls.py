#!/usr/bin/env python3
"""Unit tests for parse_html_urls extractor."""

import json
import os
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).parent.parent
SCRIPT_PATH = next(PLUGIN_DIR.glob("on_Snapshot__*_parse_html_urls.*"), None)


class TestParseHtmlUrls:
    """Test the parse_html_urls extractor CLI."""

    @pytest.mark.parametrize(
        "invalid_url",
        ["https://…", "https://[broken", "https://\u200d.example/"],
    )
    def test_invalid_link_does_not_discard_other_saved_links(
        self,
        tmp_path,
        invalid_url,
    ):
        source = tmp_path / "singlefile/singlefile.html"
        source.parent.mkdir()
        source.write_text(
            '<a href="https://archivebox.io/before">Before</a>'
            f'<a href="{invalid_url}">Invalid</a> {invalid_url} '
            '<a href="https://münchen.de/über">Unicode</a>'
            '<a href="https://archivebox.io/after">After</a>',
        )
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", "https://archivebox.io/tests/"],
            cwd=tmp_path,
            env={**os.environ, "SNAP_DIR": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        expected = {
            "https://archivebox.io/before",
            "https://münchen.de/über",
            "https://archivebox.io/after",
        }
        assert {row["url"] for row in records if row["type"] == "Snapshot"} == expected
        assert (
            next(row for row in records if row["type"] == "ArchiveResult")["status"]
            == "succeeded"
        )
        saved = tmp_path / "parse_html_urls/urls.jsonl"
        assert {
            json.loads(line)["url"] for line in saved.read_text().splitlines()
        } == expected

    def test_saved_source_read_failure_preserves_existing_urls(self, tmp_path):
        source = tmp_path / "singlefile/singlefile.html"
        source.parent.mkdir(parents=True)
        source.write_text("https://archivebox.io/tests/new")
        saved = tmp_path / "parse_html_urls/urls.jsonl"
        saved.parent.mkdir()
        original = b'{"type":"Snapshot","url":"https://archivebox.io/tests/saved"}\n'
        saved.write_bytes(original)
        source.chmod(0)
        try:
            with pytest.raises(PermissionError):
                source.read_bytes()
            result = subprocess.run(
                [str(SCRIPT_PATH), "--url", "https://archivebox.io/tests/"],
                cwd=tmp_path,
                env={**os.environ, "SNAP_DIR": str(tmp_path)},
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            source.chmod(0o600)
        assert result.returncode == 1, result.stdout + result.stderr
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        status = next(row for row in records if row["type"] == "ArchiveResult")
        assert status["status"] == "failed"
        assert "Permission denied" in result.stderr
        assert saved.read_bytes() == original

    @pytest.mark.parametrize("tag", ["style", "script"])
    def test_streams_large_raw_text_without_reparsing_previous_chunks(
        self,
        tmp_path,
        tag,
    ):
        source_dir = tmp_path / "singlefile"
        source_dir.mkdir()
        source = source_dir / "singlefile.html"
        root_url = "https://archivebox.io/tests/streaming-html/"
        with source.open("w") as stream:
            stream.write(
                f'<{tag}>/* <a href="/not-an-anchor"> https://archivebox.io/tests/raw */',
            )
            for _ in range(256):
                stream.write("A" * 1024 * 1024)
            # Split the closing tag across parser reads, including its whitespace.
            stream.write(" " * ((262144 - 3 - stream.tell()) % 262144))
            stream.write(f'</{tag.upper()} \n><a href="../after">After</a>')
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", root_url],
            cwd=tmp_path,
            env={**os.environ, "SNAP_DIR": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        expected = {
            "https://archivebox.io/tests/raw",
            "https://archivebox.io/tests/after",
        }
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        assert {row["url"] for row in records if row["type"] == "Snapshot"} == expected
        assert [row for row in records if row["type"] == "ArchiveResult"] == [
            {
                "type": "ArchiveResult",
                "status": "succeeded",
                "output_str": "2 URLs parsed",
            },
        ]
        saved = tmp_path / "parse_html_urls" / "urls.jsonl"
        assert {
            json.loads(line)["url"] for line in saved.read_text().splitlines()
        } == expected

    @pytest.mark.parametrize("tag", ["style", "script"])
    @pytest.mark.parametrize("split", range(1, 11))
    def test_preserves_raw_text_closing_tag_across_reads(self, tmp_path, tag, split):
        source_dir = tmp_path / "singlefile"
        source_dir.mkdir()
        prefix = f'<{tag}>/* </{tag}x><a href="/not-an-anchor"> */'
        closing = f"</{tag.upper()} \n>"
        source_dir.joinpath("singlefile.html").write_text(
            prefix
            + " " * (262144 - split - len(prefix))
            + closing
            + '<a href="/tests/after">After</a>',
        )
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", "https://archivebox.io/tests/"],
            cwd=tmp_path,
            env={**os.environ, "SNAP_DIR": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        assert {row["url"] for row in records if row["type"] == "Snapshot"} == {
            "https://archivebox.io/tests/after",
        }

    @pytest.mark.parametrize(
        "padding",
        [0, 262144 - 8192 - len("<a href='"), 262144 - 8192 - 20, 262144 - 20],
    )
    def test_preserves_apostrophes_without_adding_truncated_raw_matches(
        self,
        tmp_path,
        padding,
    ):
        url = "https://aaib.gov.in/What's%20New%20Assets/Preliminary%20Report%20VT-EXO.pdf"
        input_file = tmp_path / "apostrophes.html"
        input_file.write_text(
            " " * padding
            + "<a href='https://example.com/quoted'>Link</a>\n"
            + f'<a href="{url}">Report</a>\n'
            + '<a href="https://example.com/What&#39;s">Entity</a>\n'
            + "<a href=\"https://example.com/?q='word'\">Query</a>\n"
            + " " * 9000,
        )
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", input_file.as_uri()],
            cwd=tmp_path,
            env={**os.environ, "SNAP_DIR": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        expected = {
            url,
            "https://example.com/quoted",
            "https://example.com/What's",
            "https://example.com/?q='word'",
        }
        assert {
            record["url"] for record in records if record["type"] == "Snapshot"
        } == expected
        saved = tmp_path / "parse_html_urls" / "urls.jsonl"
        assert {
            json.loads(line)["url"] for line in saved.read_text().splitlines()
        } == expected

    def test_fetches_and_parses_http_html(self, tmp_path, httpserver):
        """Fetch HTTP HTML and persist both relative and absolute outlinks."""
        httpserver.expect_request("/page").respond_with_data(
            '<a href="/linked">Local</a>'
            '<a href="https://www.iana.org/domains/example">IANA</a>',
            content_type="text/html",
        )
        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmp_path)
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", httpserver.url_for("/page")],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        expected_urls = {
            httpserver.url_for("/linked"),
            "https://www.iana.org/domains/example",
        }
        assert {
            record["url"] for record in records if record["type"] == "Snapshot"
        } == expected_urls
        assert [record for record in records if record["type"] == "ArchiveResult"] == [
            {
                "type": "ArchiveResult",
                "status": "succeeded",
                "output_str": "2 URLs parsed",
            },
        ]
        saved = tmp_path / "parse_html_urls" / "urls.jsonl"
        assert {
            json.loads(line)["url"] for line in saved.read_text().splitlines()
        } == expected_urls

    def test_uses_final_snapshot_url_for_base_expanded_same_page_links(self, tmp_path):
        """Parser rewrites base-expanded same-page links back to the final URL."""
        headers_dir = tmp_path / "headers"
        headers_dir.mkdir()
        (headers_dir / "headers.json").write_text(
            json.dumps({"final_url": "https://docs.sweeting.me/s/blog"}),
        )
        singlefile_dir = tmp_path / "singlefile"
        singlefile_dir.mkdir()
        (singlefile_dir / "singlefile.html").write_text(
            """
<!doctype html>
<html>
<head><base href="https://docs.monadical.com/"></head>
<body><a href="https://docs.monadical.com/#About">About</a></body>
</html>
""",
        )

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmp_path)
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", "https://blog.sweeting.me"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        urls_file = tmp_path / "parse_html_urls" / "urls.jsonl"
        assert urls_file.exists(), "urls.jsonl not created"
        urls = {
            json.loads(line)["url"]
            for line in urls_file.read_text().splitlines()
            if line.strip()
        }

        assert "https://docs.sweeting.me/s/blog#About" in urls
        assert "https://docs.monadical.com/#About" not in urls
        assert "https://blog.sweeting.me/#About" not in urls

    def test_extracts_href_urls(self, tmp_path):
        """Test extracting URLs from anchor tags."""
        input_file = tmp_path / "page.html"
        input_file.write_text("""
<!DOCTYPE html>
<html>
<body>
    <a href="https://example.com">Example</a>
    <a href="https://foo.bar/page">Foo</a>
    <a href="http://test.org">Test</a>
</body>
</html>
        """)

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
        assert "URLs parsed" in result.stderr

        # Parse Snapshot records from stdout
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip() and '"type": "Snapshot"' in line
        ]
        assert len(lines) == 3, f"Expected 3 Snapshot records, got {len(lines)}"

        urls = set()
        for line in lines:
            entry = json.loads(line)
            assert entry["type"] == "Snapshot"
            assert "url" in entry
            urls.add(entry["url"])

        assert "https://example.com" in urls
        assert "https://foo.bar/page" in urls
        assert "http://test.org" in urls

        # Verify ArchiveResult record
        assert '"type": "ArchiveResult"' in result.stdout
        assert '"status": "succeeded"' in result.stdout

        urls_file = tmp_path / "parse_html_urls" / "urls.jsonl"
        assert urls_file.exists(), "urls.jsonl not created"
        file_lines = [
            line for line in urls_file.read_text().splitlines() if line.strip()
        ]
        assert len(file_lines) == 3, (
            f"Expected 3 urls.jsonl entries, got {len(file_lines)}"
        )

    def test_ignores_non_http_schemes(self, tmp_path):
        """Test that non-http schemes are ignored."""
        input_file = tmp_path / "page.html"
        input_file.write_text("""
<html>
<body>
    <a href="mailto:test@example.com">Email</a>
    <a href="javascript:void(0)">JS</a>
    <a href="tel:+1234567890">Phone</a>
    <a href="https://valid.com">Valid</a>
</body>
</html>
        """)

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

        # Parse Snapshot records from stdout
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if line.strip() and '"type": "Snapshot"' in line
        ]
        assert len(lines) == 1, f"Expected 1 Snapshot record, got {len(lines)}"

        entry = json.loads(lines[0])
        assert entry["url"] == "https://valid.com"

    def test_handles_html_entities(self, tmp_path):
        """Test that HTML entities in URLs are decoded."""
        input_file = tmp_path / "page.html"
        input_file.write_text("""
<html>
<body>
    <a href="https://example.com/page?a=1&amp;b=2">Link</a>
</body>
</html>
        """)

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
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        entry = json.loads(lines[0])
        assert entry["url"] == "https://example.com/page?a=1&b=2"

    def test_text_url_stops_at_entity_decoded_quote(self, tmp_path):
        """Regex-discovered text URLs should stop at entity-decoded quotes."""
        input_file = tmp_path / "page.html"
        input_file.write_text("""
<html>
<body>
    https://example.com&quot;&gt;<img src="x">
</body>
</html>
        """)

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
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        entry = json.loads(lines[0])
        assert entry["url"] == "https://example.com"

    def test_deduplicates_urls(self, tmp_path):
        """Test that duplicate URLs are deduplicated."""
        input_file = tmp_path / "page.html"
        input_file.write_text("""
<html>
<body>
    <a href="https://example.com">Link 1</a>
    <a href="https://example.com">Link 2</a>
    <a href="https://example.com">Link 3</a>
</body>
</html>
        """)

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
        lines = [
            line
            for line in result.stdout.strip().split("\n")
            if '"type": "Snapshot"' in line
        ]
        assert len(lines) == 1

    def test_excludes_source_url(self, tmp_path):
        """Test that the source URL itself is excluded from results."""
        input_file = tmp_path / "page.html"
        source_url = f"file://{input_file}"
        input_file.write_text(f'''
<html>
<body>
    <a href="{source_url}">Self</a>
    <a href="https://other.com">Other</a>
</body>
</html>
        ''')

        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", source_url],
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
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["url"] == "https://other.com"

    def test_excludes_source_url_written_as_iri(self, tmp_path):
        """A page linking to itself as /über is not a new URL.

        The root URL comes from Chrome's headers.json in encoded URI form
        (/%C3%BCber), while the page's own href stays an IRI, so a plain string
        comparison emitted the page as its own child.
        """
        headers_dir = tmp_path / "headers"
        headers_dir.mkdir()
        (headers_dir / "headers.json").write_text(
            json.dumps({"final_url": "https://example.com/%C3%BCber?q=%C3%BC"}),
        )
        singlefile_dir = tmp_path / "singlefile"
        singlefile_dir.mkdir()
        (singlefile_dir / "singlefile.html").write_text(
            '<a href="/über?q=ü">Self</a><a href="/andere">Other</a>',
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(SCRIPT_PATH), "--url", "https://example.com/über?q=ü"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**os.environ, "SNAP_DIR": str(tmp_path)},
        )

        assert result.returncode == 0, result.stderr
        urls = [
            json.loads(line)["url"]
            for line in result.stdout.splitlines()
            if '"type": "Snapshot"' in line
        ]
        assert urls == ["https://example.com/andere"], result.stdout

    def test_runs_even_if_parse_dom_outlinks_urls_exist(self, tmp_path):
        """parse_html_urls should not skip just because parse_dom_outlinks already wrote urls.jsonl."""
        input_file = tmp_path / "page.html"
        input_file.write_text("""
<html>
<body>
    <a href="https://example.com/from-html">HTML Link</a>
</body>
</html>
        """)

        dom_urls_dir = tmp_path / "parse_dom_outlinks"
        dom_urls_dir.mkdir(parents=True, exist_ok=True)
        (dom_urls_dir / "urls.jsonl").write_text(
            '{"type":"Snapshot","url":"https://stale.example/outlink"}\n',
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

        assert result.returncode == 0, result.stderr
        assert "Skipping parse_html_urls" not in result.stderr
        assert "https://example.com/from-html" in result.stdout

        urls_file = tmp_path / "parse_html_urls" / "urls.jsonl"
        assert urls_file.exists(), "parse_html_urls urls.jsonl not created"
        file_lines = [
            line for line in urls_file.read_text().splitlines() if line.strip()
        ]
        assert len(file_lines) == 1
        entry = json.loads(file_lines[0])
        assert entry["url"] == "https://example.com/from-html"

    def test_skips_when_no_urls_found(self, tmp_path):
        """Test that script succeeds without output when no URLs are found."""
        input_file = tmp_path / "page.html"
        input_file.write_text("<html><body>No links here</body></html>")

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
        assert '"status": "noresults"' in result.stdout
        assert '"output_str": "0 URLs parsed"' in result.stdout
        assert "0 URLs parsed" in result.stderr
        assert not (tmp_path / "parse_html_urls" / "urls.jsonl").exists()

    def test_handles_malformed_html(self, tmp_path):
        """Test handling of malformed HTML."""
        input_file = tmp_path / "malformed.html"
        input_file.write_text("""
<html>
<body>
    <a href="https://example.com">Unclosed tag
    <a href="https://other.com">Another link</a>
</body>
        """)

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
        assert len(lines) == 2

    def test_output_is_valid_json(self, tmp_path):
        """Test that output contains required fields."""
        input_file = tmp_path / "page.html"
        input_file.write_text('<a href="https://example.com">Link</a>')

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
        assert entry["type"] == "Snapshot"
        assert entry["plugin"] == "parse_html_urls"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
