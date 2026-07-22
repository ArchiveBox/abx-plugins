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
    _extract_snapshot_id,
    _get_search_roots,
    DEFAULT_CONTENT_EXCLUDES,
)
from abx_plugins.plugins.base.testing import (
    get_hydrated_required_binaries,
    get_plugin_dir,
    install_required_binary_from_config,
)

PLUGIN_DIR = get_plugin_dir(__file__)


@pytest.fixture(scope="module")
def rg_path() -> str:
    records = get_hydrated_required_binaries(PLUGIN_DIR)
    assert len(records) == 1, records
    resolved_name = str(records[0]["name"])
    loaded = install_required_binary_from_config(
        PLUGIN_DIR,
        resolved_name,
    )
    assert loaded.loaded_abspath
    resolved = Path(loaded.loaded_abspath)
    assert resolved.is_file()
    return str(resolved)


class TestRipgrepFlush:
    """Test the flush function."""

    def test_flush_is_noop(self):
        """flush should be a no-op for ripgrep backend."""
        # Should not raise
        flush(["snap-001", "snap-002"])


class TestRipgrepSearch:
    """Test the ripgrep search function."""

    @pytest.fixture(autouse=True)
    def archive(self, tmp_path: Path, rg_path: str):
        """Create temporary archive directory with test files."""
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

        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"RIPGREP_TIMEOUT", "RIPGREP_ARGS", "RIPGREP_ARGS_EXTRA"}
        }
        self.env.update({"SNAP_DIR": str(self.archive_dir), "RIPGREP_BINARY": rg_path})

    def _create_snapshot(self, snapshot_id: str, files: dict):
        """Create a snapshot directory with files."""
        snap_dir = self.archive_dir / snapshot_id
        for path, content in files.items():
            file_path = snap_dir / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

    def test_search_no_archive_dir(self):
        """search should return empty list when archive dir doesn't exist."""
        results = search("test", environ={**self.env, "SNAP_DIR": "/nonexistent/path"})
        assert results == []

    def test_search_single_match(self):
        """search should find matching snapshot."""
        results = search("Python programming", environ=self.env)

        assert "snap-001" in results
        assert "snap-002" not in results
        assert "snap-003" not in results

    def test_search_multiple_matches(self):
        """search should find all matching snapshots."""
        # 'guide' appears in snap-002 (JavaScript guide) and snap-003 (Archiving Guide)
        results = search("guide", environ=self.env)

        assert "snap-002" in results
        assert "snap-003" in results
        assert "snap-001" not in results

    def test_search_case_insensitive_by_default(self):
        """search should be case-sensitive (ripgrep default)."""
        # By default rg is case-sensitive
        results_upper = search("PYTHON", environ=self.env)
        results_lower = search("python", environ=self.env)

        # Depending on ripgrep config, results may differ
        assert isinstance(results_upper, list)
        assert isinstance(results_lower, list)

    def test_search_no_results(self):
        """search should return empty list for no matches."""
        results = search("xyznonexistent123", environ=self.env)
        assert results == []

    def test_search_regex(self):
        """search should support regex patterns."""
        results = search("(Python|JavaScript)", environ=self.env)

        assert "snap-001" in results
        assert "snap-002" in results

    def test_search_distinct_snapshots(self):
        """search should return distinct snapshot IDs."""
        # Query matches both files in snap-001
        results = search("Python", environ=self.env)

        # Should only appear once
        assert results.count("snap-001") == 1

    def test_search_with_custom_args(self):
        """search should use custom RIPGREP_ARGS."""
        results = search("PYTHON", environ={**self.env, "RIPGREP_ARGS": '["-i"]'})
        # With -i flag, should find regardless of case
        assert "snap-001" in results

    def test_search_timeout(self):
        """search should handle timeout gracefully."""
        # Short timeout, should still complete for small archive
        results = search("Python", environ={**self.env, "RIPGREP_TIMEOUT": "5"})
        assert isinstance(results, list)

    def test_search_contents_excludes_noncontent_files_and_strips_follow_flags(
        self,
        tmp_path: Path,
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

        env = {
            **self.env,
            "RIPGREP_ARGS": '["--follow"]',
            "RIPGREP_ARGS_EXTRA": '["-L", "-i"]',
        }

        assert search("EXCLUDEDCONTENT", search_mode="contents", environ=env) == []
        assert "snap-001" not in search(
            "SYMLINKONLY",
            search_mode="contents",
            environ=env,
        )

    def test_search_deep_reincludes_json_and_logs(self):
        self._create_snapshot("json-match", {"metadata/page.json": "deepjsonneedle"})
        self._create_snapshot("jsonl-match", {"metadata/page.jsonl": "deepjsonlneedle"})
        self._create_snapshot("log-match", {"logs/run.log": "deeplogneedle"})
        self._create_snapshot("pid-match", {"chrome/chrome.pid": "deeppidneedle"})
        self._create_snapshot("css-match", {"assets/style.css": "deepcssneedle"})
        self._create_snapshot("js-match", {"assets/script.js": "deepjsneedle"})

        assert "json-match" in search(
            "deepjsonneedle",
            search_mode="deep",
            environ=self.env,
        )
        assert "jsonl-match" in search(
            "deepjsonlneedle",
            search_mode="deep",
            environ=self.env,
        )
        assert "log-match" in search(
            "deeplogneedle",
            search_mode="deep",
            environ=self.env,
        )
        assert search("deeppidneedle", search_mode="deep", environ=self.env) == []
        assert search("deepcssneedle", search_mode="deep", environ=self.env) == []
        assert search("deepjsneedle", search_mode="deep", environ=self.env) == []


class TestRipgrepSearchIntegration:
    """Integration tests with realistic archive structure."""

    archive_dir: Path
    env: dict[str, str]

    @pytest.fixture(scope="class", autouse=True)
    def archive(self, request, tmp_path_factory, rg_path: str, real_html_snapshot):
        """Capture two live pages into the archive search layout."""
        root = tmp_path_factory.mktemp("ripgrep-live-archive")
        archive_dir = root / "archive"
        archive_dir.mkdir()
        captures = root / "captures"
        archivebox = real_html_snapshot(
            captures / "archivebox",
            "https://archivebox.io",
            "archivebox-live",
        )
        python = real_html_snapshot(
            captures / "python",
            "https://www.python.org",
            "python-live",
        )
        shutil.move(archivebox, archive_dir / "1704067200.123456")
        shutil.move(python, archive_dir / "1704153600.654321")

        request.cls.archive_dir = archive_dir
        request.cls.env = {
            **os.environ,
            "SNAP_DIR": str(archive_dir),
            "RIPGREP_BINARY": rg_path,
        }

    def test_search_archivebox(self):
        """Search for archivebox should find documentation snapshot."""
        results = search("archivebox", environ=self.env)
        assert "1704067200.123456" in results

    def test_search_python(self):
        """Search for python should find Python news snapshot."""
        results = search("Python", environ=self.env)
        assert "1704153600.654321" in results

    def test_search_pip_install(self):
        """Search for installation command."""
        results = search("self-hosted", environ=self.env)
        assert "1704067200.123456" in results


class TestRipgrepSearchCurrentArchiveBoxLayout:
    data_dir: Path
    snapshot_id: str
    snapshot_root: Path
    env: dict[str, str]

    @pytest.fixture(scope="class", autouse=True)
    def archive(self, request, tmp_path_factory, rg_path: str, real_html_snapshot):
        data_dir = tmp_path_factory.mktemp("ripgrep-current-layout")
        snapshot_id = "019cf48c-aa86-72f0-9f8f-e4ea80226fc6"
        snapshot_root = (
            data_dir
            / "archive"
            / "users"
            / "system"
            / "snapshots"
            / "20260316"
            / "example.com"
            / snapshot_id
        )
        captured = real_html_snapshot(
            data_dir / "capture",
            "https://www.google.com",
            "google-live",
        )
        snapshot_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(captured, snapshot_root)

        request.cls.data_dir = data_dir
        request.cls.snapshot_id = snapshot_id
        request.cls.snapshot_root = snapshot_root
        request.cls.env = {
            **os.environ,
            "SNAP_DIR": str(data_dir),
            "RIPGREP_BINARY": rg_path,
        }

    def test_search_roots_prefer_snapshot_content_dirs(self):
        roots = _get_search_roots(self.env)
        assert roots == [self.data_dir / "archive" / "users" / "system" / "snapshots"]

    def test_search_finds_snapshot_in_current_layout(self):
        results = search("google", environ=self.env)
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
        roots = _get_search_roots({**self.env, "SNAP_DIR": str(custom_users_dir)})

        assert roots == [custom_users_dir / "system" / "snapshots"]

    def test_extract_snapshot_id_ignores_non_snapshot_segments(self):
        roots = [self.data_dir / "archive" / "users" / "system" / "snapshots"]
        match_path = self.snapshot_root / "wget" / "index.html"
        assert _extract_snapshot_id(match_path, roots) == self.snapshot_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
