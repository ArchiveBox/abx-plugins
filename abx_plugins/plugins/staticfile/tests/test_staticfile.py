"""
Tests for the staticfile plugin.

Tests the real staticfile hook using deterministic local fixtures.
"""

import posixpath
import subprocess
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

from abx_plugins.plugins.base.test_utils import (
    get_hook_script,
    get_plugin_dir,
    parse_jsonl_output,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


# Get the path to the staticfile hook
PLUGIN_DIR = get_plugin_dir(__file__)
STATICFILE_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_staticfile.*")
RESPONSES_HOOK = get_hook_script(
    PLUGIN_DIR.parent / "responses",
    "on_Snapshot__*_responses.*",
)
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


def expected_responses_output(url: str) -> str:
    parsed_url = urlparse(url)
    pathname = parsed_url.path or "/"
    filename = posixpath.basename(pathname) or "index"
    dir_path_raw = posixpath.dirname(pathname)
    dir_path = "" if dir_path_raw == "." else dir_path_raw.lstrip("/")
    return posixpath.join("responses", parsed_url.hostname or "", dir_path, filename)


def terminate_process(proc: subprocess.Popen[str]) -> tuple[str, str]:
    if proc.poll() is None:
        proc.terminate()
        try:
            return proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return proc.communicate()


def run_staticfile_capture(
    staticfile_dir,
    snapshot_chrome_dir,
    env,
    url,
    snapshot_id,
    *,
    start_responses=True,
):
    """Launch staticfile hook, optionally run responses, navigate, and collect final JSONL."""
    responses_dir = snapshot_chrome_dir.parent / "responses"
    responses_proc = None
    responses_stdout = ""
    responses_stderr = ""

    if start_responses:
        responses_dir.mkdir(exist_ok=True)
        responses_proc = subprocess.Popen(
            [
                str(RESPONSES_HOOK),
                f"--url={url}",
                f"--snapshot-id={snapshot_id}",
            ],
            cwd=str(responses_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    hook_proc = subprocess.Popen(
        [
            str(STATICFILE_HOOK),
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
        [
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

    stdout, stderr = hook_proc.communicate(timeout=5)
    if responses_proc is not None:
        responses_stdout, responses_stderr = terminate_process(responses_proc)

    archive_result = parse_jsonl_output(stdout)
    return (
        hook_proc.returncode,
        stdout,
        stderr,
        nav_result,
        archive_result,
        responses_dir,
        responses_stdout,
        responses_stderr,
    )


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
                _responses_dir,
                _responses_stdout,
                responses_stderr,
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
        assert "Traceback" not in responses_stderr
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "noresults", archive_result
        assert archive_result.get("output_str") == "Page is HTML (not staticfile)", (
            archive_result
        )
        assert archive_result.get("content_type", "").startswith("text/html"), (
            archive_result
        )
        assert not any(path.is_file() for path in staticfile_dir.iterdir()), (
            "Should not save files under staticfile/ for HTML pages"
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
                responses_dir,
                _responses_stdout,
                responses_stderr,
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
        assert "Traceback" not in responses_stderr
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "succeeded", archive_result
        assert archive_result.get("content_type") == "application/json", archive_result
        expected_output = expected_responses_output(test_url)
        assert archive_result.get("output_str") == expected_output, archive_result
        assert (snapshot_chrome_dir.parent / expected_output).exists(), (
            f"Expected responses artifact at {expected_output}"
        )
        assert not any(path.is_file() for path in staticfile_dir.iterdir()), (
            "Staticfile should not duplicate files when responses saved the main response"
        )
        responses_output = snapshot_chrome_dir.parent / expected_output
        output_bytes = responses_output.read_bytes()
        assert output_bytes == JSON_FIXTURE_BYTES, "Responses JSON bytes mismatch"

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

            (
                hook_code,
                stdout,
                stderr,
                nav_result,
                archive_result,
                _responses_dir,
                _responses_stdout,
                responses_stderr,
            ) = run_staticfile_capture(
                staticfile_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                snapshot_id,
            )

        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Staticfile hook failed: {stderr}"
        assert "Traceback" not in responses_stderr
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "succeeded", archive_result
        assert archive_result.get("content_type") == "application/json", archive_result
        expected_output = expected_responses_output(staticfile_test_urls["json_url"])
        assert archive_result.get("output_str") == expected_output, archive_result
        assert (snapshot_chrome_dir.parent / expected_output).exists(), (
            f"Expected responses artifact at {expected_output}"
        )
        assert not any(path.is_file() for path in staticfile_dir.iterdir()), (
            "Staticfile should not duplicate redirected main-response files"
        )

    def test_staticfile_handles_redirected_html_pages_as_noresults(
        self,
        staticfile_test_urls,
    ):
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

            (
                hook_code,
                stdout,
                stderr,
                nav_result,
                archive_result,
                _responses_dir,
                _responses_stdout,
                responses_stderr,
            ) = run_staticfile_capture(
                staticfile_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                snapshot_id,
            )

        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Staticfile hook failed: {stderr}"
        assert "Traceback" not in responses_stderr
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "noresults", archive_result
        assert archive_result.get("output_str") == "Page is HTML (not staticfile)", (
            archive_result
        )
        assert archive_result.get("content_type", "").startswith("text/html"), (
            archive_result
        )

    def test_staticfile_falls_back_to_own_file_when_responses_disabled(
        self,
        staticfile_test_urls,
    ):
        """Staticfile should save its own file when responses is disabled."""
        test_url = staticfile_test_urls["json_url"]
        snapshot_id = "test-staticfile-json-no-responses"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-staticfile-crawl-json-no-responses",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
            staticfile_dir = snapshot_chrome_dir.parent / "staticfile"
            staticfile_dir.mkdir(exist_ok=True)
            env["RESPONSES_ENABLED"] = "false"

            (
                hook_code,
                stdout,
                stderr,
                nav_result,
                archive_result,
                _responses_dir,
                _responses_stdout,
                _responses_stderr,
            ) = run_staticfile_capture(
                staticfile_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                snapshot_id,
                start_responses=False,
            )

        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Staticfile hook failed: {stderr}"
        assert archive_result is not None, f"Missing ArchiveResult in stdout:\n{stdout}"
        assert archive_result.get("status") == "succeeded", archive_result
        assert archive_result.get("content_type") == "application/json", archive_result
        assert archive_result.get("output_str") == "staticfile/test.json", (
            archive_result
        )
        output_file = staticfile_dir / "test.json"
        assert output_file.exists(), (
            f"Expected downloaded fallback file at {output_file}"
        )
        assert output_file.read_bytes() == JSON_FIXTURE_BYTES


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
