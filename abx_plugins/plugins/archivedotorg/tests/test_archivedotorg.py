"""
Integration tests for archivedotorg plugin

Tests verify standalone archive.org extractor execution.
"""

import os
import re
import socket
import subprocess
import sys
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
_ARCHIVEDOTORG_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_archivedotorg.*"), None)
if _ARCHIVEDOTORG_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
ARCHIVEDOTORG_HOOK = _ARCHIVEDOTORG_HOOK
TEST_URL = "https://archivebox.io/tests/"


@pytest.mark.parametrize(
    "method",
    ["GET", "X" * 4096],
    ids=["missing-file", "oversized-error"],
)
def test_http_error_diagnostics_from_real_static_server(tmp_path, method):
    # The standard static server supplies real 404/501 errors, not a replacement
    # Wayback handler. The unsupported method also gives us an oversized body.
    with ThreadingHTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import runpy, sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
hook = runpy.run_path(sys.argv[1])
try:
    urlopen(Request(sys.argv[2], method=sys.argv[3]), timeout=5)
except HTTPError as error:
    hook['log_http_error'](error)
else:
    raise AssertionError('Static server request should fail')
""",
                    str(ARCHIVEDOTORG_HOOK),
                    f"http://127.0.0.1:{server.server_port}/missing-archivebox-diagnostic-file",
                    method,
                ],
                cwd=tmp_path,
                env={**os.environ, "SNAP_DIR": str(tmp_path)},
                capture_output=True,
                text=True,
                timeout=15,
            )
        finally:
            server.shutdown()
            thread.join()
    assert result.returncode == 0, result.stderr
    assert f"HTTP {404 if method == 'GET' else 501}" in result.stderr
    assert "Content-Type: text/html;charset=utf-8" in result.stderr
    assert "[archivedotorg] Server:" not in result.stderr
    assert "[archivedotorg] Date:" not in result.stderr
    assert "Error response" in result.stderr
    assert (
        "File not found" if method == "GET" else "Unsupported method"
    ) in result.stderr
    assert len(result.stderr) < 2600, result.stderr
    assert not (tmp_path / "archivedotorg" / "archive.org.txt").exists()


def test_connection_failure_preserves_previous_archive(tmp_path):
    output = tmp_path / "archivedotorg" / "archive.org.txt"
    output.parent.mkdir()
    previous = "https://web.archive.org/web/20200101000000/https://example.com/"
    output.write_text(previous)
    # A real refused proxy connection: bind the port without listening, so no
    # other process can acquire it and no request reaches the public service.
    with socket.socket() as unavailable_proxy:
        unavailable_proxy.bind(("127.0.0.1", 0))
        proxy = f"http://127.0.0.1:{unavailable_proxy.getsockname()[1]}"
        result = subprocess.run(
            [str(ARCHIVEDOTORG_HOOK), "--url", TEST_URL],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SNAP_DIR": str(tmp_path),
                "https_proxy": proxy,
                "HTTPS_PROXY": proxy,
                "no_proxy": "",
                "NO_PROXY": "",
                "ARCHIVEDOTORG_ENABLED": "True",
            },
            timeout=30,
        )
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout + result.stderr
    assert result.returncode == 1, result.stdout + result.stderr
    assert record["status"] == "failed", record
    assert "URLError" in record["output_str"], record
    assert output.read_text() == previous


@pytest.mark.parametrize(
    ("target_url", "expected_request_uri"),
    [
        (
            "https://example.com/über?q=ü",
            "/save/https://example.com/%C3%BCber?q=%C3%BC",
        ),
        (
            "https://münchen.example/straße/ü dir/",
            "/save/https://xn--mnchen-3ya.example/stra%C3%9Fe/%C3%BC%20dir/",
        ),
    ],
    ids=["non-ascii-path-and-query", "idn-hostname"],
)
def test_non_ascii_url_is_submitted_as_encoded_uri(
    tmp_path,
    httpserver,
    target_url,
    expected_request_uri,
):
    # Production snapshots of URLs like https://example.com/über died with
    # UnicodeEncodeError before any request left the process, because
    # http.client writes the request line as ASCII. The local server stands in
    # for Save Page Now so we can assert the exact bytes Wayback would receive.
    replay_path = f"/web/20261012000000/{expected_request_uri.removeprefix('/save/')}"
    httpserver.expect_request(re.compile(r"^/save/")).respond_with_data(
        "",
        headers={"Content-Location": replay_path},
    )
    result = subprocess.run(
        [str(ARCHIVEDOTORG_HOOK), "--url", target_url],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "ARCHIVEDOTORG_ENABLED": "True",
            "ARCHIVEDOTORG_SAVE_URL": httpserver.url_for("/save/"),
            "no_proxy": "*",
            "NO_PROXY": "*",
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "archivedotorg/archive.org.txt",
    }, result.stdout + result.stderr
    assert len(httpserver.log) == 1, httpserver.log
    request, _response = httpserver.log[0]
    assert request.environ["REQUEST_URI"] == expected_request_uri
    output = tmp_path / "archivedotorg" / "archive.org.txt"
    assert output.read_text(encoding="utf-8") == httpserver.url_for(replay_path)


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (403, "Forbidden", "Archive.org returned HTTP 403"),
        (429, "Too many requests", "Archive.org returned HTTP 429"),
        (503, "Unavailable", "Archive.org returned HTTP 503"),
        (
            200,
            "RobotAccessControlException",
            "Archive.org blocked submission by robots.txt",
        ),
        (200, "No capture", "Archive.org returned no archive URL"),
    ],
)
def test_remote_rejection_preserves_previous_archive_as_noresults(
    tmp_path,
    httpserver,
    status,
    body,
    reason,
):
    output = tmp_path / "archivedotorg" / "archive.org.txt"
    output.parent.mkdir()
    previous = "https://web.archive.org/web/20200101000000/https://example.com/"
    output.write_text(previous)
    httpserver.expect_request(re.compile(r"^/save/")).respond_with_data(
        body,
        status=status,
    )
    result = subprocess.run(
        [str(ARCHIVEDOTORG_HOOK), "--url", TEST_URL],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "ARCHIVEDOTORG_ENABLED": "True",
            "ARCHIVEDOTORG_SAVE_URL": httpserver.url_for("/save/"),
            "no_proxy": "*",
            "NO_PROXY": "*",
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": reason,
    }
    assert f"WARNING: {reason}" in result.stderr
    assert output.read_text() == previous
    assert len(httpserver.log) == 1


def test_local_write_failure_is_failed(tmp_path, httpserver):
    output = tmp_path / "archivedotorg" / "archive.org.txt"
    output.mkdir(parents=True)
    preserved = output / "keep.txt"
    preserved.write_text("Existing local data")
    httpserver.expect_request(re.compile(r"^/save/")).respond_with_data(
        "",
        headers={"Content-Location": "/web/20261012000000/https://example.com/"},
    )
    result = subprocess.run(
        [str(ARCHIVEDOTORG_HOOK), "--url", TEST_URL],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "ARCHIVEDOTORG_ENABLED": "True",
            "ARCHIVEDOTORG_SAVE_URL": httpserver.url_for("/save/"),
            "no_proxy": "*",
            "NO_PROXY": "*",
        },
        timeout=30,
    )
    assert result.returncode == 1, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record is not None
    assert record["status"] == "failed"
    assert record["output_str"].startswith("IsADirectoryError:")
    assert preserved.read_text() == "Existing local data"


def test_private_url_has_noresults(tmp_path):
    result = subprocess.run(
        [str(ARCHIVEDOTORG_HOOK), "--url", "http://127.0.0.1/private"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "SNAP_DIR": str(tmp_path), "ARCHIVEDOTORG_ENABLED": "True"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": "URL is local/private",
    }
    assert not (tmp_path / "archivedotorg" / "archive.org.txt").exists()


def test_hook_script_exists():
    assert ARCHIVEDOTORG_HOOK.exists()


@pytest.mark.parametrize("request_timeout", [10, 45])
def test_live_submission_classifies_service_response(request_timeout):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
        # Keep the hook's own network timeout below subprocess timeout so failures
        # return cleanly as exit=1 instead of being killed by pytest.
        env["ARCHIVEDOTORG_TIMEOUT"] = str(request_timeout)

        result = subprocess.run(
            [
                str(ARCHIVEDOTORG_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should have ArchiveResult JSONL output"
        output_path = tmpdir / "archivedotorg" / "archive.org.txt"
        if "WARNING: Archive.org returned HTTP 429" in result.stderr:
            # A service rate limit has no capture, and must never create a
            # successful retry-link artifact.
            assert result.returncode == 0, result.stdout + result.stderr
            assert result_json == {
                "type": "ArchiveResult",
                "status": "noresults",
                "output_str": "Archive.org returned HTTP 429",
            }, result_json
            assert not output_path.exists()
            return
        assert result.returncode == 0, result.stdout + result.stderr
        assert result_json == {
            "type": "ArchiveResult",
            "status": "succeeded",
            "output_str": "archivedotorg/archive.org.txt",
        }, result_json
        output_path = tmpdir / "archivedotorg" / "archive.org.txt"
        assert output_path.is_file(), f"Archive.org output missing: {output_path}"
        archived_url = output_path.read_text(encoding="utf-8").strip()
        assert archived_url.startswith("https://web.archive.org/web/"), archived_url


def test_config_save_archivedotorg_false_skips():
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["ARCHIVEDOTORG_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(ARCHIVEDOTORG_HOOK),
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
        assert result_json["output_str"] == "ARCHIVEDOTORG_ENABLED=False", result_json


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
