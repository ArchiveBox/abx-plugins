"""
Integration tests for archivedotorg plugin

Tests verify standalone archive.org extractor execution.
"""

import subprocess
import tempfile
from pathlib import Path
import pytest

from abx_plugins.plugins.base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
_ARCHIVEDOTORG_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_archivedotorg.*"), None)
if _ARCHIVEDOTORG_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
ARCHIVEDOTORG_HOOK = _ARCHIVEDOTORG_HOOK
TEST_URL = "https://example.com"


def test_hook_script_exists():
    assert ARCHIVEDOTORG_HOOK.exists()


def test_submits_to_archivedotorg():
    with tempfile.TemporaryDirectory() as tmpdir:
        import os

        env = os.environ.copy()
        # Keep the hook's own network timeout below subprocess timeout so failures
        # return cleanly as exit=1 instead of being killed by pytest.
        env["ARCHIVEDOTORG_TIMEOUT"] = "45"

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

        assert result.returncode == 0, result.stderr

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should have ArchiveResult JSONL output"

        if result_json["status"] == "succeeded":
            assert result_json["status"] == "succeeded", (
                f"Should succeed: {result_json}"
            )
            assert result_json["output_str"] == "archivedotorg/archive.org.txt", (
                result_json
            )
        else:
            assert result_json["status"] == "noresults", result_json
            assert result_json["output_str"], "Should include Archive.org response"
            assert "NORESULTS:" in result.stderr, result.stderr


def test_config_save_archivedotorg_false_skips():
    with tempfile.TemporaryDirectory() as tmpdir:
        import os

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


def test_handles_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        import os

        env = os.environ.copy()
        env["ARCHIVEDOTORG_TIMEOUT"] = "10"

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

        assert result.returncode == 0, result.stderr

        # With a low-but-valid timeout the hook may time out or get an HTTP error from
        # archive.org (e.g. 403/429). Either way it should emit proper JSONL and
        # never turn a normal Archive.org refusal into a failed hook.
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit ArchiveResult JSONL"
        assert result_json["status"] in {"succeeded", "noresults"}, result_json
        assert result_json["output_str"], (
            "Should include output path or error description"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
