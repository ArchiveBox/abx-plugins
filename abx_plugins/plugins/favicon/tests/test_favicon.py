"""
Integration tests for favicon plugin

Tests verify:
1. Plugin script exists
2. Favicon extraction works against deterministic local HTTP fixtures
3. Output file is actual image data
4. Tries multiple favicon URLs
5. Falls back to a configured favicon provider
6. Timeout config is honored
7. Handles failures gracefully
"""

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from werkzeug.wrappers import Response

from abx_plugins.plugins.base.testing import (
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_FAVICON_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_favicon.*")
if _FAVICON_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
FAVICON_HOOK = _FAVICON_HOOK
TEST_ICON_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0000004010100f7ff650000000049454e44ae426082",
)


def test_hook_script_exists():
    """Verify hook script exists."""
    assert FAVICON_HOOK.exists(), f"Hook script not found: {FAVICON_HOOK}"


def _run_favicon_hook(tmpdir: Path, url: str, env: dict[str, str]):
    return subprocess.run(
        [
            str(FAVICON_HOOK),
            "--url",
            url,
        ],
        cwd=tmpdir,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_extracts_linked_favicon_from_httpserver_page(httpserver):
    """Test full workflow: extract linked favicon from deterministic local page."""
    httpserver.expect_request("/").respond_with_data(
        '<html><head><link rel="icon" href="/assets/favicon.ico"></head><body></body></html>',
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/assets/favicon.ico").respond_with_data(
        TEST_ICON_BYTES,
        content_type="image/png",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)

        result = _run_favicon_hook(tmpdir, httpserver.url_for("/"), env)

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", result_json
        assert result_json["output_str"] == "favicon/favicon.ico"

        favicon_file = snap_dir / "favicon" / "favicon.ico"
        assert favicon_file.exists(), "favicon.ico not created"
        assert favicon_file.read_bytes() == TEST_ICON_BYTES


def test_config_timeout_honored(httpserver):
    """Test that FAVICON_TIMEOUT config is respected by real HTTP requests."""
    httpserver.expect_request("/").respond_with_data(
        "<html><head></head><body></body></html>",
        content_type="text/html; charset=utf-8",
    )

    release_slow_response = threading.Event()

    def slow_favicon(_request):
        release_slow_response.wait()
        return Response("too late", status=200, content_type="text/plain")

    httpserver.expect_request("/favicon.ico").respond_with_handler(slow_favicon)
    httpserver.expect_request("/favicon.png").respond_with_data("", status=404)
    httpserver.expect_request("/apple-touch-icon.png").respond_with_data("", status=404)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["FAVICON_TIMEOUT"] = "5"
        env["SNAP_DIR"] = str(tmpdir)
        env["FAVICON_PROVIDER"] = ""

        start = time.monotonic()
        try:
            result = _run_favicon_hook(tmpdir, httpserver.url_for("/"), env)
        finally:
            release_slow_response.set()
        elapsed = time.monotonic() - start

        assert result.returncode == 0, result.stderr
        assert elapsed < 8, f"Should honor FAVICON_TIMEOUT, took {elapsed:.1f}s"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None
        assert result_json["status"] == "noresults", result_json
        assert result_json["output_str"] == "No favicon found"


def test_handles_noresults_for_missing_favicon(httpserver):
    """Test that missing favicons report noresults with an exact ArchiveResult."""
    httpserver.expect_request("/").respond_with_data(
        "<html><head></head><body></body></html>",
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("", status=404)
    httpserver.expect_request("/favicon.png").respond_with_data("", status=404)
    httpserver.expect_request("/apple-touch-icon.png").respond_with_data("", status=404)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
        env["FAVICON_PROVIDER"] = ""
        result = _run_favicon_hook(tmpdir, httpserver.url_for("/"), env)

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None
        assert result_json["status"] == "noresults", result_json
        assert result_json["output_str"] == "No favicon found"
        assert not (tmpdir / "favicon" / "favicon.ico").exists()


def test_falls_back_to_configured_provider_and_emits_relative_output_path(httpserver):
    """Configured provider fallback should save favicon.ico and emit a relative path."""
    httpserver.expect_request("/").respond_with_data(
        "<html><head><title>favicon test</title></head><body></body></html>",
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("", status=404)
    httpserver.expect_request("/favicon.png").respond_with_data("", status=404)
    httpserver.expect_request("/apple-touch-icon.png").respond_with_data("", status=404)
    httpserver.expect_request(
        "/provider",
        query_string={"domain": "127.0.0.1"},
    ).respond_with_data(
        TEST_ICON_BYTES,
        content_type="image/png",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["FAVICON_PROVIDER"] = f"{httpserver.url_for('/provider')}?domain={{}}"

        result = _run_favicon_hook(
            tmpdir,
            httpserver.url_for("/").replace("localhost", "127.0.0.1", 1),
            env,
        )

        assert result.returncode == 0, result.stderr

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded"
        assert result_json["output_str"] == "favicon/favicon.ico"

        favicon_file = snap_dir / "favicon" / "favicon.ico"
        assert favicon_file.exists(), "favicon.ico not created"
        assert favicon_file.read_bytes() == TEST_ICON_BYTES
        assert any(
            request.path == "/provider"
            and request.query_string.decode() == "domain=127.0.0.1"
            for request, _response in httpserver.log
        ), "Configured fallback provider was not called"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
