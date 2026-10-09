"""
Integration tests for archivedotorg plugin

Tests verify standalone archive.org extractor execution.
"""

import os
import socket
import subprocess
import tempfile
from pathlib import Path
import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
_ARCHIVEDOTORG_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_archivedotorg.*"), None)
if _ARCHIVEDOTORG_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
ARCHIVEDOTORG_HOOK = _ARCHIVEDOTORG_HOOK
TEST_URL = "https://example.com"


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
        if "ERROR: Archive.org returned HTTP 429" in result.stderr:
            # A real service rate limit must be a failed extraction, never a
            # successful retry-link artifact. This branch still verifies the
            # complete hook contract; it does not skip a rate-limited run.
            assert result.returncode == 1, result.stdout + result.stderr
            assert result_json == {
                "type": "ArchiveResult",
                "status": "failed",
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
