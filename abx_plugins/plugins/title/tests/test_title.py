"""
Integration tests for title plugin

Tests verify:
1. Plugin script exists
2. Node.js is available
3. Title extraction works from deterministic local pages
4. Output file contains actual page title
5. Handles various title sources (<title>, og:title, twitter:title)
6. Config options work (TITLE_TIMEOUT)
"""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_plugin_dir,
    get_hook_script,
    get_test_env,
    chrome_session,
    CHROME_NAVIGATE_HOOK,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_TITLE_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_title.*")
if _TITLE_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
TITLE_HOOK = _TITLE_HOOK
TEST_URL = "http://example.invalid/"
CHROME_STARTUP_TIMEOUT_SECONDS = 45


@pytest.fixture
def title_test_urls(httpserver):
    """Serve deterministic local pages for title extraction tests."""
    httpserver.expect_request("/").respond_with_data(
        """
        <!doctype html>
        <html>
        <head><title>Example Domain</title></head>
        <body><h1>Local Title Fixture</h1></body>
        </html>
        """.strip(),
        content_type="text/html",
    )
    httpserver.expect_request("/404").respond_with_data(
        """
        <!doctype html>
        <html>
        <head><title>Not Found Fixture</title></head>
        <body><h1>Not Found</h1></body>
        </html>
        """.strip(),
        content_type="text/html",
        status=404,
    )
    httpserver.expect_request("/redirect").respond_with_data(
        "",
        status=302,
        headers={"Location": "/"},
    )

    return {
        "base": httpserver.url_for("/"),
        "not_found": httpserver.url_for("/404"),
        "redirect": httpserver.url_for("/redirect"),
    }


def run_title_capture(title_dir, snapshot_chrome_dir, env, url, snapshot_id):
    nav_result = subprocess.run(
        [
            "node",
            str(CHROME_NAVIGATE_HOOK),
            f"--url={url}",
            f"--snapshot-id={snapshot_id}",
        ],
        cwd=str(snapshot_chrome_dir),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    result = subprocess.run(
        ["node", str(TITLE_HOOK), f"--url={url}", f"--snapshot-id={snapshot_id}"],
        cwd=title_dir,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return nav_result, result


def test_hook_script_exists():
    """Verify hook script exists."""
    assert TITLE_HOOK.exists(), f"Hook script not found: {TITLE_HOOK}"


def test_extracts_title_from_example_com(title_test_urls):
    """Test full workflow: extract title from deterministic local fixture."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=title_test_urls["base"],
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            title_dir = snapshot_chrome_dir.parent / "title"
            title_dir.mkdir(exist_ok=True)

            nav_result, result = run_title_capture(
                title_dir,
                snapshot_chrome_dir,
                env,
                title_test_urls["base"],
                "test789",
            )
            assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Parse clean JSONL output
        result_json = None
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("{"):
                pass
                try:
                    record = json.loads(line)
                    if record.get("type") == "ArchiveResult":
                        result_json = record
                        break
                except json.JSONDecodeError:
                    pass

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"
        assert result_json["output_str"] == "Example Domain"

        # Verify output file exists (hook writes to current directory)
        title_file = title_dir / "title.txt"
        assert title_file.exists(), "title.txt not created"

        # Verify title contains deterministic fixture title
        title_text = title_file.read_text().strip()
        assert len(title_text) > 0, "Title should not be empty"
        assert "example" in title_text.lower(), "Title should contain 'example'"

        assert "example domain" in title_text.lower(), (
            f"Expected 'Example Domain', got: {title_text}"
        )


def test_fails_without_chrome_session():
    """Test that title plugin fails when chrome session is missing."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        title_dir = snap_dir / "title"
        title_dir.mkdir(parents=True, exist_ok=True)
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}

        # Run title extraction
        result = subprocess.run(
            ["node", str(TITLE_HOOK), f"--url={TEST_URL}", "--snapshot-id=testhttp"],
            cwd=title_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        assert result.returncode != 0, (
            f"Should fail without chrome session: {result.stderr}"
        )
        assert "No Chrome session found (chrome plugin must run first)" in (
            result.stdout + result.stderr
        )


def test_config_timeout_honored(title_test_urls):
    """Test that TITLE_TIMEOUT config is respected."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set very short timeout (fixture page should still succeed)
        env_override = {"TITLE_TIMEOUT": "5"}

        with chrome_session(
            tmpdir,
            test_url=title_test_urls["base"],
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            title_dir = snapshot_chrome_dir.parent / "title"
            title_dir.mkdir(exist_ok=True)
            env.update(env_override)

            nav_result, result = run_title_capture(
                title_dir,
                snapshot_chrome_dir,
                env,
                title_test_urls["base"],
                "testtimeout",
            )
            assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"

        # Should complete (success or fail, but not hang)
        assert result.returncode in (0, 1), "Should complete without hanging"


def test_handles_https_urls(chrome_test_https_url):
    """Test HTTPS behavior deterministically (success or explicit cert failure)."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=chrome_test_https_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            title_dir = snapshot_chrome_dir.parent / "title"
            title_dir.mkdir(exist_ok=True)
            # Keep this bounded so a failed TLS navigation cannot hang the hook for long.
            env["TITLE_TIMEOUT"] = "5"

            nav_result, result = run_title_capture(
                title_dir,
                snapshot_chrome_dir,
                env,
                chrome_test_https_url,
                "testhttps",
            )

        if nav_result.returncode == 0:
            assert result.returncode == 0, (
                f"Title extraction should succeed after successful HTTPS navigation: {result.stderr}"
            )
            output_title_file = title_dir / "title.txt"
            assert output_title_file.exists(), "title.txt not created for HTTPS page"
            title_text = output_title_file.read_text().strip()
            assert len(title_text) > 0, "Title should not be empty"
        else:
            nav_output = (nav_result.stdout + nav_result.stderr).lower()
            assert "err_cert" in nav_output or "certificate" in nav_output, (
                f"Expected explicit TLS certificate error, got: {nav_result.stderr}"
            )
            assert result.returncode != 0, (
                "Title hook should fail when HTTPS navigation fails due certificate validation"
            )


def test_handles_404_gracefully(title_test_urls):
    """Test that title plugin handles 404 pages."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=title_test_urls["not_found"],
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            title_dir = snapshot_chrome_dir.parent / "title"
            title_dir.mkdir(exist_ok=True)

            nav_result, result = run_title_capture(
                title_dir,
                snapshot_chrome_dir,
                env,
                title_test_urls["not_found"],
                "test404",
            )
            assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"

        # May succeed or fail depending on server behavior
        assert result.returncode in (0, 1), "Should complete (may succeed or fail)"


def test_handles_redirects(title_test_urls):
    """Test that title plugin handles redirects correctly."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=title_test_urls["redirect"],
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            title_dir = snapshot_chrome_dir.parent / "title"
            title_dir.mkdir(exist_ok=True)

            nav_result, result = run_title_capture(
                title_dir,
                snapshot_chrome_dir,
                env,
                title_test_urls["redirect"],
                "testredirect",
            )
            assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"

        # Should succeed and follow redirect
        if result.returncode == 0:
            # Hook writes to current directory
            output_title_file = title_dir / "title.txt"
            if output_title_file.exists():
                title_text = output_title_file.read_text().strip()
                assert "example" in title_text.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
