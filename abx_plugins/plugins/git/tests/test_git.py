"""
Integration tests for git plugin

Tests verify:
    pass
1. Validate hook checks for git binary
2. Verify deps with abx-pkg
3. Standalone git extractor execution
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pytest

from abx_plugins.plugins.base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
_GIT_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_git.*"), None)
if _GIT_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
GIT_HOOK = _GIT_HOOK
TEST_URL = "https://github.com/ArchiveBox/abx-pkg.git"


def test_hook_script_exists():
    assert GIT_HOOK.exists()


def test_verify_deps_with_abx_pkg():
    """Verify git is available via abx-pkg."""
    from abx_pkg import Binary, AptProvider, BrewProvider, EnvProvider

    try:
        apt_provider = AptProvider()
        brew_provider = BrewProvider()
        env_provider = EnvProvider()
    except Exception as exc:
        pytest.fail(f"System package providers unavailable in this runtime: {exc}")

    git_binary = Binary(
        name="git", binproviders=[apt_provider, brew_provider, env_provider]
    )
    git_loaded = git_binary.load()

    assert git_loaded and git_loaded.abspath, "git is required for git plugin tests"


def test_reports_missing_git():
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {"PATH": "/nonexistent"}
        result = subprocess.run(
            [
                sys.executable,
                str(GIT_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test123",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            assert (
                "DEPENDENCY_NEEDED" in combined
                or "git" in combined.lower()
                or "ERROR=" in combined
            )


def test_handles_non_git_url():
    assert shutil.which("git"), "git binary not available"

    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [
                str(GIT_HOOK),
                "--url",
                "https://example.com",
                "--snapshot-id",
                "test789",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Should fail or skip for non-git URL
        assert result.returncode in (0, 1)

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        if result_json:
            assert result_json["status"] == "noresults", (
                f"Non-git URL should report noresults: {result_json}"
            )
            assert result_json["output_str"] == "Not a git URL", result_json


def test_real_git_repo():
    """Test that git can clone a real GitHub repository."""
    import os

    assert shutil.which("git"), "git binary not available"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Use a real but small GitHub repository
        git_url = "https://github.com/ArchiveBox/abx-pkg"

        env = os.environ.copy()
        env["GIT_TIMEOUT"] = "120"  # Give it time to clone
        env["SNAP_DIR"] = str(tmpdir)
        env["CRAWL_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                str(GIT_HOOK),
                "--url",
                git_url,
                "--snapshot-id",
                "testgit",
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
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Check that the git repo was cloned in the hook's output path.
        assert result_json.get("output_str", "").startswith("git"), result_json
        output_path = tmpdir / (result_json.get("output_str") or "git")
        git_dirs = list(output_path.glob("**/.git"))
        assert len(git_dirs) > 0, (
            f"Should have cloned a git repository. Output path: {output_path}"
        )

        print(f"Successfully cloned repository in {elapsed_time:.2f}s")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
