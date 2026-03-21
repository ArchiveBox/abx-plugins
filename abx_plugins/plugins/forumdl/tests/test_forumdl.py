"""
Integration tests for forumdl plugin

Tests verify:
    pass
1. Hook script exists
2. Dependencies installed via validation hooks
3. Verify deps with abx-pkg
4. Forum extraction works on forum URLs
5. JSONL output is correct
6. Config options work
7. Handles non-forum URLs gracefully
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path
import pytest

from abx_plugins.plugins.base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_FORUMDL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_forumdl.*"), None)
if _FORUMDL_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
FORUMDL_HOOK = _FORUMDL_HOOK
TEST_URL = "http://example.com"

# Module-level cache for binary path
_forumdl_binary_path = None


def require_forumdl_binary() -> str:
    """Return forum-dl binary path or fail with actionable context."""
    binary_path = get_forumdl_binary_path()
    assert binary_path, (
        "forum-dl installation failed. Install hook should install forum-dl automatically "
        "with macOS-compatible dependencies."
    )
    assert Path(binary_path).is_file(), f"forum-dl binary path invalid: {binary_path}"
    return binary_path


def get_forumdl_binary_path() -> str | None:
    """Get forum-dl binary path, installing via abx_pkg if needed."""
    global _forumdl_binary_path
    if _forumdl_binary_path:
        return _forumdl_binary_path

    from abx_pkg import Binary, PipProvider, EnvProvider

    binary = Binary(
        name="forum-dl",
        binproviders=[PipProvider(), EnvProvider()],
        overrides={
            "pip": {
                "install_args": [
                    "--no-deps",
                    "--prefer-binary",
                    "forum-dl",
                    "chardet==5.2.0",
                    "beautifulsoup4",
                    "soupsieve",
                    "lxml",
                    "requests",
                    "urllib3",
                    "tenacity",
                    "python-dateutil",
                    "six",
                    "html2text",
                    "warcio",
                ],
            },
        },
    ).load_or_install()
    if binary and binary.abspath:
        _forumdl_binary_path = str(binary.abspath)
        return _forumdl_binary_path

    return None


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert FORUMDL_HOOK.exists(), f"Hook not found: {FORUMDL_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify forum-dl is installed by calling the REAL installation hooks."""
    binary_path = require_forumdl_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_handles_non_forum_url(local_http_base_url):
    """Test that forum-dl extractor handles non-forum URLs gracefully via hook."""

    binary_path = require_forumdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["FORUMDL_BINARY"] = binary_path
        env["SNAP_DIR"] = str(tmpdir)
        env.pop("LIB_DIR", None)

        # Run forum-dl extraction hook on non-forum URL
        result = subprocess.run(
            [
                str(FORUMDL_HOOK),
                "--url",
                local_http_base_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # Should exit 0 even for non-forum URL (graceful handling)
        assert result.returncode == 0, (
            f"Should handle non-forum URL gracefully: {result.stderr}"
        )

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "noresults", (
            f"Should report noresults for non-forum URL: {result_json}"
        )
        assert result_json["output_str"] == "No forum found", result_json


def test_config_save_forumdl_false_skips():
    """Test that FORUMDL_ENABLED=False exits without emitting JSONL."""

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["FORUMDL_ENABLED"] = "False"
        env["SNAP_DIR"] = str(tmpdir)
        env.pop("LIB_DIR", None)

        result = subprocess.run(
            [
                str(FORUMDL_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )

        # Feature disabled should emit skipped JSONL
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected skipped JSONL output"
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "FORUMDL_ENABLED=False", result_json


def test_config_timeout():
    """Test that FORUMDL_TIMEOUT config is respected."""

    binary_path = require_forumdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["FORUMDL_BINARY"] = binary_path
        env["FORUMDL_TIMEOUT"] = "5"
        env["SNAP_DIR"] = str(tmpdir)
        env.pop("LIB_DIR", None)

        start_time = time.time()
        result = subprocess.run(
            [
                str(FORUMDL_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,  # Should complete in 5s, use 10s as safety margin
        )
        elapsed_time = time.time() - start_time

        assert result.returncode == 0, (
            f"Should complete without hanging: {result.stderr}"
        )
        # Allow 1 second overhead for subprocess startup and Python interpreter
        assert elapsed_time <= 6.0, (
            f"Should complete within 6 seconds (5s timeout + 1s overhead), took {elapsed_time:.2f}s"
        )


def test_real_forum_url():
    """Test that forum-dl extracts content from a real HackerNews thread with jsonl output.

    Uses our Pydantic v2 compatible wrapper to fix forum-dl 0.3.0's incompatibility.
    """

    binary_path = require_forumdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Use HackerNews - one of the most reliable forum-dl extractors
        forum_url = "https://news.ycombinator.com/item?id=1"

        env = os.environ.copy()
        env["FORUMDL_BINARY"] = binary_path
        env["FORUMDL_TIMEOUT"] = "60"
        env["FORUMDL_OUTPUT_FORMAT"] = "jsonl"  # Use jsonl format
        env["SNAP_DIR"] = str(tmpdir)
        env.pop("LIB_DIR", None)
        # HTML output could be added via: env['FORUMDL_ARGS_EXTRA'] = json.dumps(['--files-output', './files'])

        start_time = time.time()
        result = subprocess.run(
            [
                str(FORUMDL_HOOK),
                "--url",
                forum_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        elapsed_time = time.time() - start_time

        # Should succeed with our Pydantic v2 wrapper
        assert result.returncode == 0, (
            f"Should extract forum successfully: {result.stderr}"
        )

        # Parse JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, (
            f"Should have ArchiveResult JSONL output. stdout: {result.stdout}"
        )
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Check that forum files were downloaded
        output_files = list(tmpdir.glob("**/*"))
        forum_files = [f for f in output_files if f.is_file()]

        assert len(forum_files) > 0, (
            f"Should have downloaded at least one forum file. Files: {output_files}"
        )

        # Verify the JSONL file has content
        jsonl_file = tmpdir / "forumdl" / "forum.jsonl"
        assert jsonl_file.exists(), "Should have created forum.jsonl"
        assert jsonl_file.stat().st_size > 0, "forum.jsonl should not be empty"

        print(
            f"Successfully extracted {len(forum_files)} file(s) in {elapsed_time:.2f}s",
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
