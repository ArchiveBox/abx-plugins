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
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
import pytest
from werkzeug.wrappers import Response

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
    # Reserve a real local port without listening: the connection is refused
    # without any request or DNS lookup reaching the public service.
    with socket.socket() as unavailable_server:
        unavailable_server.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{unavailable_server.getsockname()[1]}/save/"
        result = subprocess.run(
            [str(ARCHIVEDOTORG_HOOK), "--url", TEST_URL],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SNAP_DIR": str(tmp_path),
                "ARCHIVEDOTORG_SAVE_URL": endpoint,
                "no_proxy": "*",
                "NO_PROXY": "*",
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
        (404, "Not found", "Archive.org returned HTTP 404"),
        (429, "Too many requests", "Archive.org returned HTTP 429"),
        (500, "Internal error", "Archive.org returned HTTP 500"),
        (502, "Bad gateway", "Archive.org returned HTTP 502"),
        (503, "Unavailable", "Archive.org returned HTTP 503"),
        (
            200,
            "RobotAccessControlException",
            "Archive.org blocked submission by robots.txt",
        ),
        (200, "No capture", "Archive.org returned no archive URL"),
    ],
)
@pytest.mark.parametrize("previous_output", [False, True])
def test_remote_rejection_fails_and_preserves_previous_archive(
    tmp_path,
    httpserver,
    status,
    body,
    reason,
    previous_output,
):
    output = tmp_path / "archivedotorg" / "archive.org.txt"
    output.parent.mkdir()
    previous = "https://web.archive.org/web/20200101000000/https://example.com/"
    if previous_output:
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
    assert result.returncode == 1, result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "failed",
        "output_str": reason,
    }
    assert f"ERROR: {reason}" in result.stderr
    if previous_output:
        assert output.read_text() == previous
    else:
        assert not output.exists()
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


@pytest.mark.parametrize(
    "proof",
    ["relative", "absolute", "redirect", "replay-modifier"],
)
def test_confirmed_replay_is_saved(tmp_path, httpserver, proof):
    modifier = "id_" if proof == "replay-modifier" else ""
    replay_path = f"/web/20261012000000{modifier}/{TEST_URL}"
    archive_url = httpserver.url_for(replay_path)
    if proof == "redirect":
        httpserver.expect_request(re.compile(r"^/save/")).respond_with_data(
            "",
            status=302,
            headers={"Location": replay_path},
        )
        httpserver.expect_request(replay_path).respond_with_data("Archived page")
    else:
        location = archive_url if proof == "absolute" else replay_path
        httpserver.expect_request(re.compile(r"^/save/")).respond_with_data(
            "",
            headers={"Content-Location": location},
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
            "NO_PROXY": "*",
            "no_proxy": "*",
        },
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "archivedotorg/archive.org.txt",
    }
    assert (tmp_path / "archivedotorg/archive.org.txt").read_text() == archive_url
    assert len(httpserver.log) == (2 if proof == "redirect" else 1)


def test_confirmed_replay_does_not_wait_for_unneeded_body(tmp_path, httpserver):
    release = Event()
    archive_path = f"/web/20261012000000/{TEST_URL}"

    def slow_body():
        yield b"x"
        release.wait(5)
        yield b"Unused response body"

    httpserver.expect_request(re.compile(r"^/save/")).respond_with_handler(
        lambda request: Response(
            slow_body(),
            headers={"Content-Location": archive_path},
        ),
    )
    output = tmp_path / "archivedotorg/archive.org.txt"
    output.parent.mkdir()
    output.write_text("Previously confirmed archive")
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(ARCHIVEDOTORG_HOOK), "--url", TEST_URL],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SNAP_DIR": str(tmp_path),
                "ARCHIVEDOTORG_ENABLED": "True",
                "ARCHIVEDOTORG_TIMEOUT": "3",
                "ARCHIVEDOTORG_SAVE_URL": httpserver.url_for("/save/"),
                "NO_PROXY": "*",
                "no_proxy": "*",
            },
            timeout=5,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "archivedotorg/archive.org.txt",
    }
    assert elapsed < 2, elapsed
    assert output.read_text() == httpserver.url_for(archive_path)


@pytest.mark.parametrize(
    "proof",
    [
        "empty-replay",
        "wildcard",
        "foreign-host",
        "foreign-scheme",
        "foreign-port",
        "save-url",
    ],
)
def test_unconfirmed_locations_fail_without_overwriting(tmp_path, httpserver, proof):
    locations = {
        "empty-replay": "/web/",
        "wildcard": f"/web/*/{TEST_URL}",
        "foreign-host": httpserver.url_for(f"/web/20261012000000/{TEST_URL}").replace(
            httpserver.host,
            "archive.invalid",
            1,
        ),
        "foreign-scheme": httpserver.url_for(f"/web/20261012000000/{TEST_URL}").replace(
            "http:",
            "https:",
            1,
        ),
        "foreign-port": httpserver.url_for(f"/web/20261012000000/{TEST_URL}").replace(
            f":{httpserver.port}/",
            f":{httpserver.port - 1}/",
            1,
        ),
        "save-url": f"/save/{TEST_URL}",
    }
    output = tmp_path / "archivedotorg/archive.org.txt"
    output.parent.mkdir()
    output.write_text("Previously confirmed archive")
    httpserver.expect_request(re.compile(r"^/save/")).respond_with_data(
        "",
        headers={"Content-Location": locations[proof]},
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
            "NO_PROXY": "*",
            "no_proxy": "*",
        },
        timeout=10,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "failed",
        "output_str": "Archive.org returned no archive URL",
    }
    assert output.read_text() == "Previously confirmed archive"
    assert len(httpserver.log) == 1


@pytest.mark.parametrize("mode", ["silent", "trickle", "error-trickle", "redirects"])
def test_total_submission_deadline_preserves_previous_output(
    tmp_path,
    httpserver,
    mode,
):
    release = Event()

    def trickle():
        while not release.wait(0.1):
            yield b"x"

    def respond(request):
        if mode == "silent":
            release.wait(5)
            return Response("No archive URL")
        if mode == "redirects":
            time.sleep(0.4)
            step = (
                int(request.path.rsplit("/", 1)[-1])
                if request.path.startswith("/redirect/")
                else 0
            )
            if step < 5:
                return Response(
                    "",
                    status=302,
                    headers={"Location": f"/redirect/{step + 1}"},
                )
            return Response(
                "",
                headers={"Content-Location": f"/web/20261012000000/{TEST_URL}"},
            )
        return Response(trickle(), status=503 if mode == "error-trickle" else 200)

    httpserver.expect_request(re.compile(r"^/(save|redirect)/")).respond_with_handler(
        respond,
    )
    output = tmp_path / "archivedotorg/archive.org.txt"
    output.parent.mkdir()
    output.write_text("Previously confirmed archive")
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(ARCHIVEDOTORG_HOOK), "--url", TEST_URL],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "SNAP_DIR": str(tmp_path),
                "ARCHIVEDOTORG_ENABLED": "True",
                "ARCHIVEDOTORG_TIMEOUT": "1",
                "ARCHIVEDOTORG_SAVE_URL": httpserver.url_for("/save/"),
                "NO_PROXY": "*",
                "no_proxy": "*",
            },
            timeout=5,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert result.returncode == 1, result.stdout + result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "failed",
        "output_str": "Request timed out after 1 seconds",
    }
    assert 0.8 < elapsed < 3, elapsed
    assert output.read_text() == "Previously confirmed archive"


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
