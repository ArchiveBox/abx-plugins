"""
Integration tests for git plugin

Tests verify:
1. Validate hook checks for git binary
2. Verify deps with abxpkg
3. Standalone git extractor execution
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pytest

from abx_plugins.plugins.base.test_utils import (
    install_required_binary_from_config,
    parse_jsonl_output,
)

PLUGIN_DIR = Path(__file__).parent.parent
_GIT_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_git.*"), None)
if _GIT_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
GIT_HOOK = _GIT_HOOK
TEST_URL = "https://github.com/ArchiveBox/abxpkg.git"


def test_hook_script_exists():
    assert GIT_HOOK.exists()


def test_verify_deps_with_abxpkg():
    """Verify git is available via abxpkg."""
    git_loaded = install_required_binary_from_config(PLUGIN_DIR, "git")

    assert git_loaded and git_loaded.abspath, "git is required for git plugin tests"


def test_reports_missing_git():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        missing_git = tmpdir_path / "missing-git-binary"

        env = os.environ.copy()
        env["GIT_BINARY"] = str(missing_git)
        result = subprocess.run(
            [
                sys.executable,
                str(GIT_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 1, result.stdout + result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None, f"stdout={result.stdout} stderr={result.stderr}"
        assert result_json["type"] == "ArchiveResult"
        assert result_json["status"] == "failed"
        assert "FileNotFoundError" in result_json["output_str"], result_json
        assert str(missing_git) in result_json["output_str"], result_json
        assert "ERROR: FileNotFoundError" in result.stderr


def test_handles_non_git_url():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [
                str(GIT_HOOK),
                "--url",
                "https://example.com",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0

        result_json = parse_jsonl_output(result.stdout)
        assert result_json == {
            "type": "ArchiveResult",
            "status": "noresults",
            "output_str": "Not a git URL",
        }, result_json
        assert (
            "Skipping git clone for non-git URL: https://example.com" in result.stderr
        )


def test_real_git_repo():
    """Test that git can clone a real GitHub repository."""
    git_loaded = install_required_binary_from_config(PLUGIN_DIR, "git")
    assert git_loaded and git_loaded.abspath, "git is required for git plugin tests"
    assert Path(git_loaded.abspath).is_file(), git_loaded.abspath

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Use a real but small GitHub repository
        git_url = "https://github.com/ArchiveBox/abxpkg"

        env = os.environ.copy()
        env["GIT_TIMEOUT"] = "120"  # Give it time to clone
        env["GIT_BINARY"] = str(git_loaded.abspath)
        env["SNAP_DIR"] = str(tmpdir)
        env["CRAWL_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                str(GIT_HOOK),
                "--url",
                git_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        elapsed_time = time.time() - start_time

        # Should succeed
        assert result.returncode == 0, (
            f"Should clone repository successfully: {result.stderr}"
        )

        # Parse JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, (
            f"Should have ArchiveResult JSONL output. stdout: {result.stdout}"
        )
        assert result_json == {
            "type": "ArchiveResult",
            "status": "succeeded",
            "output_str": "git",
        }, result_json

        output_path = tmpdir / "git"
        assert (output_path / ".git").is_dir(), (
            f"Should have cloned a git repository. Output path: {output_path}"
        )
        assert (output_path / "README.md").is_file(), (
            f"Expected repository file missing from cloned checkout: {output_path}"
        )

        print(f"Successfully cloned repository in {elapsed_time:.2f}s")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
