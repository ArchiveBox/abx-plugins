"""
Integration tests for git plugin

Tests verify:
1. Validate hook checks for git binary
2. Verify deps with abxpkg
3. Standalone git extractor execution
"""

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
import pytest

from abx_plugins.plugins.base.testing import (
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


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://wicg.github.io/file-system-access/", "Not a git URL"),
        (
            "https://wicg.github.io/__archivebox_missing_repository__.git",
            "No git repository found",
        ),
    ],
)
def test_expected_repository_absence_exits_cleanly(tmp_path, url, message):
    output = tmp_path / "git"
    output.mkdir()
    previous = output / "previous.txt"
    previous.write_text("Preserve the previous capture until replacement.\n")
    result = subprocess.run(
        [str(GIT_HOOK), "--url", url],
        cwd=tmp_path,
        env={**os.environ, "SNAP_DIR": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": message,
    }
    assert previous.read_text() == "Preserve the previous capture until replacement.\n"
    if message == "Not a git URL":
        assert not (output / ".git").exists()


def test_refused_git_connection_remains_failed(tmp_path):
    with socket.socket() as closed_service:
        closed_service.bind(("127.0.0.1", 0))
        port = closed_service.getsockname()[1]
        # A bound socket with no listener produces a real refused connection.
        result = subprocess.run(
            [str(GIT_HOOK), "--url", f"http://127.0.0.1:{port}/repository.git"],
            cwd=tmp_path,
            env={**os.environ, "SNAP_DIR": str(tmp_path), "NO_PROXY": "127.0.0.1"},
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 1
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout
    assert record["status"] == "failed"
    assert "git fetch failed" in record["output_str"]
    assert "Failed to connect" in record["output_str"]


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
