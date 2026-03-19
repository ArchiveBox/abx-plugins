"""
Tests for the staticfile plugin.

Tests the real staticfile hook using deterministic local fixtures.
"""

import subprocess
import shutil
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
    chrome_session,
)


# Get the path to the staticfile hook
PLUGIN_DIR = get_plugin_dir(__file__)
STATICFILE_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_staticfile.*")
CHROME_STARTUP_TIMEOUT_SECONDS = 45
JSON_FIXTURE_BYTES = b'{"fixture":"staticfile","ok":true}\n'


@pytest.fixture
def staticfile_test_urls(httpserver):
    """Serve deterministic non-static and static responses."""
    httpserver.expect_request("/html").respond_with_data(
        """
        <!doctype html>
        <html>
          <head><title>Staticfile Fixture</title></head>
          <body><h1>Staticfile HTML Fixture</h1></body>
        </html>
        """.strip(),
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/test.json").respond_with_data(
        JSON_FIXTURE_BYTES,
        content_type="application/json",
    )
    httpserver.expect_request("/redirect-json").respond_with_data(
        "",
        status=302,
        headers={"Location": "/test.json"},
    )
    httpserver.expect_request("/redirect-html").respond_with_data(
        "",
        status=302,
        headers={"Location": "/html"},
    )
    return {
        "html_url": httpserver.url_for("/html"),
        "json_url": httpserver.url_for("/test.json"),
        "redirect_json_url": httpserver.url_for("/redirect-json"),
        "redirect_html_url": httpserver.url_for("/redirect-html"),
    }


def run_staticfile_capture(staticfile_dir, snapshot_chrome_dir, env, url, snapshot_id):
    """Launch staticfile hook, navigate, and collect its self-emitted final JSONL."""
    hook_proc = subprocess.Popen(
        [str(STATICFILE_HOOK),
            f"--url={url}",
            f"--snapshot-id={snapshot_id}",
        ],
        cwd=str(staticfile_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    # Ensure listeners attach before navigation starts.
    time.sleep(1)

    nav_result = subprocess.run(
        [str(CHROME_NAVIGATE_HOOK),
            f"--url={url}",
            f"--snapshot-id={snapshot_id}",
        ],
        cwd=str(snapshot_chrome_dir),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    stdout, stderr = hook_proc.communicate(timeout=5)

    archive_result = parse_jsonl_output(stdout)
    return hook_proc.returncode, stdout, stderr, nav_result, archive_result


class TestStaticfilePlugin:
    """Test the staticfile plugin."""

    def test_staticfile_hook_exists(self):
        """Staticfile hook script should exist."""
        assert STATICFILE_HOOK is not None, (
            "Staticfile hook not found in plugin directory"
        )
        assert STATICFILE_HOOK.exists(), f"Hook not found: {STATICFILE_HOOK}"


class TestStaticfileWithChrome:
    """Integration tests for staticfile plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_staticfile_skips_html_pages(self, staticfile_test_urls):
        """Staticfile hook should skip HTML pages (not static files)."""
        test_url = staticfile_test_urls["html_url"]
        snapshot_id = "test-staticfile-html"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-staticfile-crawl-html",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
            staticfile_dir = snapshot_chrome_dir.parent / "staticfile"
            staticfile_dir.mkdir(exist_ok=True)

            (
                hook_code,
                stdout,
                stderr,
                nav_result,
                archive_result,
            ) = run_staticfile_capture(
                staticfile_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                snapshot_id,
            )

        assert nav_result.returncode in (0, 1), (
            f"Unexpected navigation return code: {nav_result.returncode}\n"
            f"stderr={nav_result.stderr}\nstdout={nav_result.stdout}"
        )
        if nav_result.returncode == 1:
            assert "ERR_ABORTED" in nav_result.stderr, (
                "Direct static-file navigations may abort in Chromium while still "
                "emitting the response; expected ERR_ABORTED when returncode=1"
            )
        assert hook_code == 0, f"Staticfile hook failed: {stderr}"
        assert "Traceback" not in stderr
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "noresults", archive_result
        assert archive_result.get("output_str") == "text/html", archive_result
        assert archive_result.get("content_type", "").startswith("text/html"), (
            archive_result
        )
        assert not any(staticfile_dir.glob("*.pdf")), (
            "Should not download files for HTML pages"
        )

    def test_staticfile_downloads_static_file_pages(self, staticfile_test_urls):
        """Staticfile hook should download deterministic static-file fixtures."""
        test_url = staticfile_test_urls["json_url"]
        snapshot_id = "test-staticfile-json"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-staticfile-crawl-json",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
            staticfile_dir = snapshot_chrome_dir.parent / "staticfile"
            staticfile_dir.mkdir(exist_ok=True)

            (
                hook_code,
                stdout,
                stderr,
                nav_result,
                archive_result,
            ) = run_staticfile_capture(
                staticfile_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                snapshot_id,
            )

        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Staticfile hook failed: {stderr}"
        assert "Traceback" not in stderr
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "succeeded", archive_result
        assert archive_result.get("content_type") == "application/json", archive_result

        assert archive_result.get("output_str") == "application/json", archive_result
        output_files = [path for path in staticfile_dir.iterdir() if path.is_file()]
        assert len(output_files) == 1, f"Expected exactly one downloaded file, got: {output_files}"
        output_file = output_files[0]
        assert output_file.exists(), f"Expected downloaded file at {output_file}"
        output_bytes = output_file.read_bytes()
        assert output_bytes == JSON_FIXTURE_BYTES, "Downloaded JSON bytes mismatch"

    def test_staticfile_handles_redirected_main_document(self, staticfile_test_urls):
        """Staticfile hook should classify the final main-document response after redirects."""
        test_url = staticfile_test_urls["redirect_json_url"]
        snapshot_id = "test-staticfile-redirect-json"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-staticfile-crawl-redirect-json",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
            staticfile_dir = snapshot_chrome_dir.parent / "staticfile"
            staticfile_dir.mkdir(exist_ok=True)

            hook_code, stdout, stderr, nav_result, archive_result = run_staticfile_capture(
                staticfile_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                snapshot_id,
            )

        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Staticfile hook failed: {stderr}"
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "succeeded", archive_result
        assert archive_result.get("content_type") == "application/json", archive_result

    def test_staticfile_handles_redirected_html_pages_as_noresults(self, staticfile_test_urls):
        """Staticfile hook should emit noresults for redirected HTML main documents."""
        test_url = staticfile_test_urls["redirect_html_url"]
        snapshot_id = "test-staticfile-redirect-html"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-staticfile-crawl-redirect-html",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
            staticfile_dir = snapshot_chrome_dir.parent / "staticfile"
            staticfile_dir.mkdir(exist_ok=True)

            hook_code, stdout, stderr, nav_result, archive_result = run_staticfile_capture(
                staticfile_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                snapshot_id,
            )

        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Staticfile hook failed: {stderr}"
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "noresults", archive_result
        assert archive_result.get("content_type", "").startswith("text/html"), archive_result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
