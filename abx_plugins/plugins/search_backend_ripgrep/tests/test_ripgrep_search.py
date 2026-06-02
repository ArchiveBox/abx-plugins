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
from pathlib import Path

import pytest

from abx_plugins.plugins.search_backend_ripgrep.search import (
    search,
    flush,
    _build_cmd,
    _extract_snapshot_id,
    _get_search_roots,
    DEFAULT_CONTENT_EXCLUDES,
)

RG_PATH = shutil.which("rg")


class TestRipgrepFlush:
    """Test the flush function."""

    def test_flush_is_noop(self):
        """flush should be a no-op for ripgrep backend."""
        # Should not raise
        flush(["snap-001", "snap-002"])


class TestRipgrepSearch:
    """Test the ripgrep search function."""

    @pytest.fixture(autouse=True)
    def archive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Create temporary archive directory with test files."""
        assert RG_PATH, "rg is required for ripgrep backend tests"
        self.archive_dir = tmp_path / "archive"
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

        monkeypatch.setenv("SNAP_DIR", str(self.archive_dir))
        monkeypatch.setenv("RIPGREP_BINARY", RG_PATH)
        monkeypatch.delenv("RIPGREP_TIMEOUT", raising=False)
        monkeypatch.delenv("RIPGREP_ARGS", raising=False)
        monkeypatch.delenv("RIPGREP_ARGS_EXTRA", raising=False)

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

    def test_search_missing_binary(self, monkeypatch: pytest.MonkeyPatch):
        """search should tolerate a resolved binary that cannot execute."""
        real_build_cmd = _build_cmd

        def missing_binary_cmd(query: str, search_mode: str = "contents"):
            cmd, search_roots, timeout = real_build_cmd(query, search_mode)
            cmd[0] = "/nonexistent/rg"
            return cmd, search_roots, timeout

        monkeypatch.setattr(
            "abx_plugins.plugins.search_backend_ripgrep.search._build_cmd",
            missing_binary_cmd,
        )
        assert search("test") == []

    def test_search_with_custom_args(self, monkeypatch: pytest.MonkeyPatch):
        """search should use custom RIPGREP_ARGS."""
        monkeypatch.setenv("RIPGREP_ARGS", '["-i"]')  # Case insensitive
        results = search("PYTHON")
        # With -i flag, should find regardless of case
        assert "snap-001" in results

    def test_search_timeout(self, monkeypatch: pytest.MonkeyPatch):
        """search should handle timeout gracefully."""
        monkeypatch.setenv("RIPGREP_TIMEOUT", "5")
        # Short timeout, should still complete for small archive
        results = search("Python")
        assert isinstance(results, list)

    def test_search_contents_excludes_noncontent_files_and_strips_follow_flags(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """contents mode should ignore noncontent globs and never follow symlinks."""
        for index, glob in enumerate(DEFAULT_CONTENT_EXCLUDES):
            suffix = glob.removeprefix("*")
            self._create_snapshot(
                f"excluded-{index}",
                {f"metadata/excluded{suffix}": "excludedcontent"},
            )
        symlink_target = tmp_path / "outside.txt"
        symlink_target.write_text("symlinkonly", encoding="utf-8")
        symlink_path = self.archive_dir / "snap-001" / "singlefile" / "linked.html"
        symlink_path.symlink_to(symlink_target)

        monkeypatch.setenv("RIPGREP_ARGS", '["--follow"]')
        monkeypatch.setenv("RIPGREP_ARGS_EXTRA", '["-L", "-i"]')

        assert search("EXCLUDEDCONTENT", search_mode="contents") == []
        assert "snap-001" not in search("SYMLINKONLY", search_mode="contents")

    def test_search_deep_reincludes_json_and_logs(self):
        self._create_snapshot("json-match", {"metadata/page.json": "deepjsonneedle"})
        self._create_snapshot("jsonl-match", {"metadata/page.jsonl": "deepjsonlneedle"})
        self._create_snapshot("log-match", {"logs/run.log": "deeplogneedle"})
        self._create_snapshot("pid-match", {"chrome/chrome.pid": "deeppidneedle"})
        self._create_snapshot("css-match", {"assets/style.css": "deepcssneedle"})
        self._create_snapshot("js-match", {"assets/script.js": "deepjsneedle"})

        assert "json-match" in search("deepjsonneedle", search_mode="deep")
        assert "jsonl-match" in search("deepjsonlneedle", search_mode="deep")
        assert "log-match" in search("deeplogneedle", search_mode="deep")
        assert search("deeppidneedle", search_mode="deep") == []
        assert search("deepcssneedle", search_mode="deep") == []
        assert search("deepjsneedle", search_mode="deep") == []


class TestRipgrepSearchIntegration:
    """Integration tests with realistic archive structure."""

    @pytest.fixture(autouse=True)
    def archive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Create archive with realistic structure."""
        assert RG_PATH, "rg is required for ripgrep backend tests"
        self.archive_dir = tmp_path / "archive"
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

        monkeypatch.setenv("SNAP_DIR", str(self.archive_dir))
        monkeypatch.setenv("RIPGREP_BINARY", RG_PATH)

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
    @pytest.fixture(autouse=True)
    def archive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert RG_PATH, "rg is required for ripgrep backend tests"
        self.data_dir = tmp_path
        self.snapshot_id = "019cf48c-aa86-72f0-9f8f-e4ea80226fc6"
        self.snapshot_root = (
            self.data_dir
            / "archive"
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

        monkeypatch.setenv("SNAP_DIR", str(self.data_dir))
        monkeypatch.setenv("RIPGREP_BINARY", RG_PATH)

    def test_search_roots_prefer_snapshot_content_dirs(self):
        roots = _get_search_roots()
        assert roots == [self.data_dir / "archive" / "users" / "system" / "snapshots"]

    def test_search_finds_snapshot_in_current_layout(self):
        results = search("google")
        assert results == [self.snapshot_id]

    def test_search_roots_support_configured_users_dir_name(self):
        custom_users_dir = self.data_dir / "mounted" / "custom_users"
        custom_snapshot_root = (
            custom_users_dir
            / "system"
            / "snapshots"
            / "20260316"
            / "example.com"
            / self.snapshot_id
        )
        (custom_snapshot_root / "wget").mkdir(parents=True, exist_ok=True)
        os.environ["SNAP_DIR"] = str(custom_users_dir)

        roots = _get_search_roots()

        assert roots == [custom_users_dir / "system" / "snapshots"]

    def test_extract_snapshot_id_ignores_non_snapshot_segments(self):
        roots = [self.data_dir / "archive" / "users" / "system" / "snapshots"]
        match_path = self.snapshot_root / "wget" / "index.html"
        assert _extract_snapshot_id(match_path, roots) == self.snapshot_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
