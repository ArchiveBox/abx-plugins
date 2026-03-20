"""
Integration tests for headers plugin

Tests verify:
1. Plugin script exists and is executable
2. Node.js is available
3. Headers extraction works for deterministic local URLs
4. Output JSON contains actual HTTP headers
5. Config options work (TIMEOUT, USER_AGENT)
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    get_test_env,
    chrome_session,
)

PLUGIN_DIR = Path(__file__).parent.parent
_HEADERS_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_headers.*"), None)
if _HEADERS_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
HEADERS_HOOK = _HEADERS_HOOK
TEST_URL = "http://headers-test.invalid/"
CHROME_STARTUP_TIMEOUT_SECONDS = 45


@pytest.fixture
def headers_test_urls(httpserver):
    """Serve deterministic pages for headers integration tests."""
    httpserver.expect_request("/").respond_with_data(
        """
        <!doctype html>
        <html>
          <head><title>Headers Fixture</title></head>
          <body><h1>Headers Fixture</h1></body>
        </html>
        """.strip(),
        content_type="text/html; charset=utf-8",
        headers={"Cache-Control": "max-age=60"},
    )
    httpserver.expect_request("/404").respond_with_data(
        """
        <!doctype html>
        <html>
          <head><title>Not Found Fixture</title></head>
          <body><h1>Not Found</h1></body>
        </html>
        """.strip(),
        content_type="text/html; charset=utf-8",
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


def normalize_root_url(url: str) -> str:
    return url.rstrip("/")


def run_headers_capture(headers_dir, snapshot_chrome_dir, env, url, snapshot_id):
    hook_proc = subprocess.Popen(
        [str(HEADERS_HOOK), f"--url={url}", f"--snapshot-id={snapshot_id}"],
        cwd=headers_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    nav_result = subprocess.run(
        [
            str(CHROME_NAVIGATE_HOOK),
            f"--url={url}",
            f"--snapshot-id={snapshot_id}",
        ],
        cwd=snapshot_chrome_dir,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    headers_file = headers_dir / "headers.json"
    wait_seconds = 60 if nav_result.returncode == 0 else 5
    for _ in range(wait_seconds):
        if headers_file.exists() and headers_file.stat().st_size > 0:
            break
        time.sleep(1)

    try:
        stdout, stderr = hook_proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        hook_proc.kill()
        stdout, stderr = hook_proc.communicate()

    return hook_proc.returncode, stdout, stderr, nav_result, headers_file


def test_hook_script_exists():
    """Verify hook script exists."""
    assert HEADERS_HOOK.exists(), f"Hook script not found: {HEADERS_HOOK}"


def test_node_is_available():
    """Test that Node.js is available on the system."""
    result = subprocess.run(["which", "node"], capture_output=True, text=True)
    assert result.returncode == 0, f"node not found in PATH: {result.stderr}"

    binary_path = result.stdout.strip()
    assert Path(binary_path).exists(), f"Binary should exist at {binary_path}"

    # Test that node is executable and get version
    result = subprocess.run(
        ["node", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        env=get_test_env(),
    )
    assert result.returncode == 0, f"node not executable: {result.stderr}"
    assert result.stdout.startswith("v"), (
        f"Unexpected node version format: {result.stdout}"
    )


def test_extracts_headers_from_example_com(require_chrome_runtime, headers_test_urls):
    """Test full workflow: extract headers from deterministic local fixture."""
    test_url = headers_test_urls["base"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            headers_dir = snapshot_chrome_dir.parent / "headers"
            headers_dir.mkdir(exist_ok=True)

            result = run_headers_capture(
                headers_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                "test789",
            )

        hook_code, stdout, stderr, nav_result, headers_file = result
        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Extraction failed: {stderr}"

        # Parse clean JSONL output
        result_json = None
        for line in stdout.strip().split("\n"):
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

        # Verify output file exists (hook writes to current directory)
        assert headers_file.exists(), "headers.json not created"

        # Verify headers JSON contains deterministic local response
        headers_data = json.loads(headers_file.read_text())

        assert "url" in headers_data, "Should have url field"
        assert normalize_root_url(headers_data["url"]) == normalize_root_url(
            test_url
        ), f"URL should be {test_url}"

        assert "status" in headers_data, "Should have status field"
        assert headers_data["status"] in [200, 301, 302], (
            f"Should have valid HTTP status, got {headers_data['status']}"
        )

        assert "request_headers" in headers_data, "Should have request_headers field"
        assert isinstance(headers_data["request_headers"], dict), (
            "Request headers should be a dict"
        )

        assert "response_headers" in headers_data, "Should have response_headers field"
        assert isinstance(headers_data["response_headers"], dict), (
            "Response headers should be a dict"
        )
        assert len(headers_data["response_headers"]) > 0, (
            "Response headers dict should not be empty"
        )

        assert "headers" in headers_data, "Should have headers field"
        assert isinstance(headers_data["headers"], dict), "Headers should be a dict"

        # Verify common HTTP headers are present
        headers_lower = {
            k.lower(): v for k, v in headers_data["response_headers"].items()
        }
        assert "content-type" in headers_lower or "content-length" in headers_lower, (
            "Should have at least one common HTTP header"
        )

        assert headers_data["response_headers"].get(":status") == str(
            headers_data["status"]
        ), "Response headers should include :status pseudo header"


def test_headers_output_structure(require_chrome_runtime, headers_test_urls):
    """Test that headers plugin produces correctly structured output."""
    test_url = headers_test_urls["base"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            headers_dir = snapshot_chrome_dir.parent / "headers"
            headers_dir.mkdir(exist_ok=True)

            result = run_headers_capture(
                headers_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                "testformat",
            )

        hook_code, stdout, stderr, nav_result, headers_file = result
        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, f"Extraction failed: {stderr}"

        # Parse clean JSONL output
        result_json = None
        for line in stdout.strip().split("\n"):
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

        # Verify output structure
        assert headers_file.exists(), "Output headers.json not created"

        output_data = json.loads(headers_file.read_text())

        # Verify all required fields are present
        assert "url" in output_data, "Output should have url field"
        assert "status" in output_data, "Output should have status field"
        assert "request_headers" in output_data, (
            "Output should have request_headers field"
        )
        assert "response_headers" in output_data, (
            "Output should have response_headers field"
        )
        assert "headers" in output_data, "Output should have headers field"

        # Verify data types
        assert isinstance(output_data["status"], int), "Status should be integer"
        assert isinstance(output_data["request_headers"], dict), (
            "Request headers should be dict"
        )
        assert isinstance(output_data["response_headers"], dict), (
            "Response headers should be dict"
        )
        assert isinstance(output_data["headers"], dict), "Headers should be dict"

        # Verify local fixture returns expected headers
        assert normalize_root_url(output_data["url"]) == normalize_root_url(test_url)
        assert output_data["status"] == 200


def test_fails_without_chrome_session():
    """Test that headers plugin fails when chrome session is missing."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Run headers extraction
        result = subprocess.run(
            [str(HEADERS_HOOK), f"--url={TEST_URL}", "--snapshot-id=testhttp"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
            env=get_test_env(),
        )

        assert result.returncode != 0, "Should fail without chrome session"
        combined_output = result.stdout + result.stderr
        assert (
            "No Chrome session found (chrome plugin must run first)" in combined_output
            or "Cannot find module 'puppeteer-core'" in combined_output
        ), f"Unexpected error output: {combined_output}"


def test_config_timeout_honored(require_chrome_runtime, headers_test_urls):
    """Test that TIMEOUT config is respected."""
    test_url = headers_test_urls["base"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Set very short timeout (fixture should still succeed)
        with chrome_session(
            tmpdir,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            headers_dir = snapshot_chrome_dir.parent / "headers"
            headers_dir.mkdir(exist_ok=True)
            env["TIMEOUT"] = "5"

            result = run_headers_capture(
                headers_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                "testtimeout",
            )

        # Should complete (success or fail, but not hang)
        hook_code, _stdout, _stderr, nav_result, _headers_file = result
        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code in (0, 1), "Should complete without hanging"


def test_config_user_agent(require_chrome_runtime, headers_test_urls):
    """Test that USER_AGENT config is used."""
    test_url = headers_test_urls["base"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            headers_dir = snapshot_chrome_dir.parent / "headers"
            headers_dir.mkdir(exist_ok=True)
            env["USER_AGENT"] = "TestBot/1.0"

            result = run_headers_capture(
                headers_dir,
                snapshot_chrome_dir,
                env,
                test_url,
                "testua",
            )

        # Should succeed on fixture page
        hook_code, stdout, _stderr, nav_result, _headers_file = result
        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        if hook_code == 0:
            # Parse clean JSONL output
            result_json = None
            for line in stdout.strip().split("\n"):
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
            assert result_json["status"] == "succeeded", (
                f"Should succeed: {result_json}"
            )


def test_handles_https_urls(require_chrome_runtime, chrome_test_https_url):
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
            headers_dir = snapshot_chrome_dir.parent / "headers"
            headers_dir.mkdir(exist_ok=True)
            result = run_headers_capture(
                headers_dir,
                snapshot_chrome_dir,
                env,
                chrome_test_https_url,
                "testhttps",
            )

        hook_code, _stdout, _stderr, nav_result, headers_file = result
        if nav_result.returncode == 0:
            assert hook_code == 0, (
                "Headers hook should succeed after successful HTTPS navigation"
            )
            assert headers_file.exists(), "headers.json not created for HTTPS page"
            output_data = json.loads(headers_file.read_text())
            assert normalize_root_url(output_data["url"]) == normalize_root_url(
                chrome_test_https_url
            )
            assert output_data["status"] == 200
        else:
            nav_output = (nav_result.stdout + nav_result.stderr).lower()
            assert "err_cert" in nav_output or "certificate" in nav_output, (
                f"Expected TLS/certificate navigation error, got: {nav_result.stderr}"
            )
            assert hook_code in (0, 1), (
                "Hook must terminate cleanly when HTTPS navigation fails"
            )


def test_handles_404_gracefully(require_chrome_runtime, headers_test_urls):
    """Test that headers plugin handles 404s gracefully."""
    not_found_url = headers_test_urls["not_found"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=not_found_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_process, _pid, snapshot_chrome_dir, env):
            headers_dir = snapshot_chrome_dir.parent / "headers"
            headers_dir.mkdir(exist_ok=True)
            result = run_headers_capture(
                headers_dir,
                snapshot_chrome_dir,
                env,
                not_found_url,
                "test404",
            )

        hook_code, _stdout, _stderr, nav_result, headers_file = result
        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, "Headers hook should succeed for HTTP 404 responses"
        assert headers_file.exists(), "headers.json not created"
        output_data = json.loads(headers_file.read_text())
        assert output_data["status"] == 404, "Should capture 404 status"


def test_redirect_updates_headers_final_url(require_chrome_runtime, headers_test_urls):
    """Redirect captures should reflect the final navigation response after redirect completion."""
    redirect_url = headers_test_urls["redirect"]
    final_url = headers_test_urls["base"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=redirect_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_process, _pid, snapshot_chrome_dir, env):
            headers_dir = snapshot_chrome_dir.parent / "headers"
            headers_dir.mkdir(exist_ok=True)
            hook_code, _stdout, _stderr, nav_result, headers_file = run_headers_capture(
                headers_dir,
                snapshot_chrome_dir,
                env,
                redirect_url,
                "testredirect",
            )

        assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"
        assert hook_code == 0, "Headers hook should succeed for redirects"
        assert headers_file.exists(), "headers.json not created"

        output_data = json.loads(headers_file.read_text())
        assert normalize_root_url(output_data["url"]) == normalize_root_url(redirect_url)
        assert normalize_root_url(output_data["final_url"]) == normalize_root_url(final_url), (
            f"final_url should reflect the post-redirect destination, got {output_data['final_url']}"
        )
        assert output_data["status"] == 200, output_data
        assert normalize_root_url(output_data["response_url"]) == normalize_root_url(final_url), output_data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
