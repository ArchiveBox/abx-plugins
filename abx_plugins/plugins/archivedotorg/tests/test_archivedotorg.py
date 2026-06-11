"""
Integration tests for archivedotorg plugin

Tests verify standalone archive.org extractor execution.
"""

import os
import subprocess
import tempfile
from pathlib import Path
import pytest
from werkzeug.wrappers import Response

from abx_plugins.plugins.base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
_ARCHIVEDOTORG_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_archivedotorg.*"), None)
if _ARCHIVEDOTORG_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
ARCHIVEDOTORG_HOOK = _ARCHIVEDOTORG_HOOK
TEST_URL = "https://example.com"


def test_hook_script_exists():
    assert ARCHIVEDOTORG_HOOK.exists()


def _run_archivedotorg_hook(
    tmpdir: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    return subprocess.run(
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


def test_submits_to_configured_archivedotorg_endpoint(httpserver):
    archived_path = "/web/20260610123456/https://example.com"
    httpserver.expect_request("/save/https://example.com").respond_with_data(
        "saved",
        status=200,
        headers={
            "Content-Location": archived_path,
            "X-Archive-Orig-Url": TEST_URL,
        },
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
        env["ARCHIVEDOTORG_ENDPOINT"] = f"{httpserver.url_for('/save')}/{{url}}"

        result = _run_archivedotorg_hook(tmpdir, env)

        assert result.returncode == 0, result.stderr

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json == {
            "type": "ArchiveResult",
            "status": "succeeded",
            "output_str": "archivedotorg/archive.org.txt",
        }, result_json
        output_path = tmpdir / "archivedotorg" / "archive.org.txt"
        assert output_path.is_file(), f"Archive.org output missing: {output_path}"
        archived_url = output_path.read_text(encoding="utf-8").strip()
        assert archived_url == f"https://web.archive.org{archived_path}"
        assert len(httpserver.log) == 1


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


def test_archivedotorg_http_429_is_deterministic_noresults(httpserver):
    def rate_limited(_request):
        return Response("rate limited", status=429, content_type="text/plain")

    httpserver.expect_request("/save/https://example.com").respond_with_handler(
        rate_limited,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
        env["ARCHIVEDOTORG_ENDPOINT"] = f"{httpserver.url_for('/save')}/{{url}}"

        result = _run_archivedotorg_hook(tmpdir, env)

        assert result.returncode == 0, result.stderr

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit ArchiveResult JSONL"
        assert result_json == {
            "type": "ArchiveResult",
            "status": "noresults",
            "output_str": "HTTP 429",
        }, result_json
        output_path = tmpdir / "archivedotorg" / "archive.org.txt"
        assert not output_path.exists(), (
            f"Archive.org output should not exist: {output_path}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
