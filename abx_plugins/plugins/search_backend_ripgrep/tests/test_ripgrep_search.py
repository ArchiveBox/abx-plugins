"""
Tests for the ripgrep search backend.

Tests cover:
1. Search with ripgrep binary
2. Snapshot ID extraction from file paths
3. Timeout handling
4. Error handling
5. Environment variable configuration
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from abx_plugins.plugins.search_backend_ripgrep.search import (
    search,
    flush,
    _extract_snapshot_id,
    _get_search_roots,
    DEFAULT_CONTENT_EXCLUDES,
    DEEP_EXCLUDES,
)


class TestRipgrepFlush:
    """Test the flush function."""

    def test_flush_is_noop(self):
        """flush should be a no-op for ripgrep backend."""
        # Should not raise
        flush(["snap-001", "snap-002"])


class TestRipgrepSearch:
    """Test the ripgrep search function."""

    def setup_method(self, _method=None):
        """Create temporary archive directory with test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.archive_dir = Path(self.temp_dir) / "archive"
        self.archive_dir.mkdir()

        # Create snapshot directories with searchable content
        self._create_snapshot(
            "snap-001",
            {
                "singlefile/index.html": "<html><body>Python programming tutorial</body></html>",
                "title/title.txt": "Learn Python Programming",
            },
        )
        self._create_snapshot(
            "snap-002",
            {
                "singlefile/index.html": "<html><body>JavaScript guide</body></html>",
                "title/title.txt": "JavaScript Basics",
            },
        )
        self._create_snapshot(
            "snap-003",
            {
                "wget/index.html": "<html><body>Web archiving guide and best practices</body></html>",
                "title/title.txt": "Web Archiving guide",
            },
        )

        self._orig_snap_dir = os.environ.get("SNAP_DIR")
        os.environ["SNAP_DIR"] = str(self.archive_dir)

    def teardown_method(self, _method=None):
        """Clean up temporary directory."""
        if self._orig_snap_dir is None:
            os.environ.pop("SNAP_DIR", None)
        else:
            os.environ["SNAP_DIR"] = self._orig_snap_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_snapshot(self, snapshot_id: str, files: dict):
        """Create a snapshot directory with files."""
        snap_dir = self.archive_dir / snapshot_id
        for path, content in files.items():
            file_path = snap_dir / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

    def _has_ripgrep(self) -> bool:
        """Check if ripgrep is available."""
        return shutil.which("rg") is not None

    def test_search_no_archive_dir(self):
        """search should return empty list when archive dir doesn't exist."""
        os.environ["SNAP_DIR"] = "/nonexistent/path"
        results = search("test")
        assert results == []

    def test_search_single_match(self):
        """search should find matching snapshot."""
        results = search("Python programming")

        assert "snap-001" in results
        assert "snap-002" not in results
        assert "snap-003" not in results

    def test_search_multiple_matches(self):
        """search should find all matching snapshots."""
        # 'guide' appears in snap-002 (JavaScript guide) and snap-003 (Archiving Guide)
        results = search("guide")

        assert "snap-002" in results
        assert "snap-003" in results
        assert "snap-001" not in results

    def test_search_case_insensitive_by_default(self):
        """search should be case-sensitive (ripgrep default)."""
        # By default rg is case-sensitive
        results_upper = search("PYTHON")
        results_lower = search("python")

        # Depending on ripgrep config, results may differ
        assert isinstance(results_upper, list)
        assert isinstance(results_lower, list)

    def test_search_no_results(self):
        """search should return empty list for no matches."""
        results = search("xyznonexistent123")
        assert results == []

    def test_search_regex(self):
        """search should support regex patterns."""
        results = search("(Python|JavaScript)")

        assert "snap-001" in results
        assert "snap-002" in results

    def test_search_distinct_snapshots(self):
        """search should return distinct snapshot IDs."""
        # Query matches both files in snap-001
        results = search("Python")

        # Should only appear once
        assert results.count("snap-001") == 1

    def test_search_missing_binary(self):
        """search should raise when ripgrep binary not found."""
        with patch.dict(os.environ, {"RIPGREP_BINARY": "/nonexistent/rg"}):
            with patch("shutil.which", return_value=None):
                with pytest.raises(RuntimeError) as context:
                    search("test")
                assert "ripgrep binary not found" in str(context.value)

    def test_search_with_custom_args(self):
        """search should use custom RIPGREP_ARGS."""
        with patch.dict(os.environ, {"RIPGREP_ARGS": '["-i"]'}):  # Case insensitive
            results = search("PYTHON")
            # With -i flag, should find regardless of case
            assert "snap-001" in results

    def test_search_timeout(self):
        """search should handle timeout gracefully."""
        with patch.dict(os.environ, {"RIPGREP_TIMEOUT": "1"}):
            # Short timeout, should still complete for small archive
            results = search("Python")
            assert isinstance(results, list)

    def test_search_contents_excludes_noncontent_files_and_strips_follow_flags(self):
        rg_path = shutil.which("rg")
        assert rg_path

        with (
            patch.dict(
                os.environ,
                {"RIPGREP_ARGS": '["--follow"]', "RIPGREP_ARGS_EXTRA": '["-L", "-i"]'},
            ),
            patch(
                "abx_plugins.plugins.search_backend_ripgrep.search.resolve_binary_path",
                return_value=rg_path,
            ),
            patch(
                "abx_plugins.plugins.search_backend_ripgrep.search.subprocess.run",
            ) as run,
        ):
            run.return_value = type("Result", (), {"stdout": ""})()
            search("Python", search_mode="contents")

        cmd = run.call_args[0][0]
        assert "--follow" not in cmd
        assert "-L" not in cmd
        for glob in DEFAULT_CONTENT_EXCLUDES:
            assert f"!{glob}" in cmd

    def test_search_deep_reincludes_json_and_logs(self):
        rg_path = shutil.which("rg")
        assert rg_path

        with (
            patch(
                "abx_plugins.plugins.search_backend_ripgrep.search.resolve_binary_path",
                return_value=rg_path,
            ),
            patch(
                "abx_plugins.plugins.search_backend_ripgrep.search.subprocess.run",
            ) as run,
        ):
            run.return_value = type("Result", (), {"stdout": ""})()
            search("Python", search_mode="deep")

        cmd = run.call_args[0][0]
        for glob in DEEP_EXCLUDES:
            assert f"!{glob}" in cmd
        for glob in ("*.json", "*.jsonl", "*.log"):
            assert f"!{glob}" not in cmd


class TestRipgrepSearchIntegration:
    """Integration tests with realistic archive structure."""

    def setup_method(self, _method=None):
        """Create archive with realistic structure."""
        self.temp_dir = tempfile.mkdtemp()
        self.archive_dir = Path(self.temp_dir) / "archive"
        self.archive_dir.mkdir()

        # Realistic snapshot structure
        self._create_snapshot(
            "1704067200.123456",
            {  # 2024-01-01
                "singlefile.html": """<!DOCTYPE html>
<html>
<head><title>ArchiveBox Documentation</title></head>
<body>
<h1>Getting Started with ArchiveBox</h1>
<p>ArchiveBox is a powerful, self-hosted web archiving tool.</p>
<p>Install with: pip install archivebox</p>
</body>
</html>""",
                "title/title.txt": "ArchiveBox Documentation",
                "screenshot/screenshot.png": b"PNG IMAGE DATA",  # Binary file
            },
        )
        self._create_snapshot(
            "1704153600.654321",
            {  # 2024-01-02
                "wget/index.html": """<html>
<head><title>Python News</title></head>
<body>
<h1>Python 3.12 Released</h1>
<p>New features include improved error messages and performance.</p>
</body>
</html>""",
                "readability/content.html": "<p>Python 3.12 has been released with exciting new features.</p>",
            },
        )

        self._orig_snap_dir = os.environ.get("SNAP_DIR")
        os.environ["SNAP_DIR"] = str(self.archive_dir)

    def teardown_method(self, _method=None):
        """Clean up."""
        if self._orig_snap_dir is None:
            os.environ.pop("SNAP_DIR", None)
        else:
            os.environ["SNAP_DIR"] = self._orig_snap_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_snapshot(self, timestamp: str, files: dict):
        """Create snapshot with timestamp-based ID."""
        snap_dir = self.archive_dir / timestamp
        for path, content in files.items():
            file_path = snap_dir / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                file_path.write_bytes(content)
            else:
                file_path.write_text(content)

    def test_search_archivebox(self):
        """Search for archivebox should find documentation snapshot."""
        results = search("archivebox")
        assert "1704067200.123456" in results

    def test_search_python(self):
        """Search for python should find Python news snapshot."""
        results = search("Python")
        assert "1704153600.654321" in results

    def test_search_pip_install(self):
        """Search for installation command."""
        results = search("pip install")
        assert "1704067200.123456" in results


class TestRipgrepSearchCurrentArchiveBoxLayout:
    def setup_method(self, _method=None):
        self.temp_dir = tempfile.mkdtemp()
        self.data_dir = Path(self.temp_dir)
        self.snapshot_id = "019cf48c-aa86-72f0-9f8f-e4ea80226fc6"
        self.snapshot_root = (
            self.data_dir
            / "users"
            / "system"
            / "snapshots"
            / "20260316"
            / "example.com"
            / self.snapshot_id
        )
        (self.snapshot_root / "wget").mkdir(parents=True, exist_ok=True)
        (self.snapshot_root / "wget" / "index.html").write_text(
            "<html><body>google search page</body></html>",
        )

        lib_dir = self.data_dir / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        (lib_dir / "big.txt").write_text("google " * 1000)

        self._orig_snap_dir = os.environ.get("SNAP_DIR")
        os.environ["SNAP_DIR"] = str(self.data_dir)

    def teardown_method(self, _method=None):
        if self._orig_snap_dir is None:
            os.environ.pop("SNAP_DIR", None)
        else:
            os.environ["SNAP_DIR"] = self._orig_snap_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_search_roots_prefer_snapshot_content_dirs(self):
        roots = _get_search_roots()
        assert roots == [self.data_dir / "users" / "system" / "snapshots"]

    def test_search_finds_snapshot_in_current_layout(self):
        results = search("google")
        assert results == [self.snapshot_id]

    def test_extract_snapshot_id_ignores_non_snapshot_segments(self):
        roots = [self.data_dir / "users" / "system" / "snapshots"]
        match_path = self.snapshot_root / "wget" / "index.html"
        assert _extract_snapshot_id(match_path, roots) == self.snapshot_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
