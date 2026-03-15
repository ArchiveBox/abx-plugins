"""
Integration tests for archivedotorg plugin

Tests verify standalone archive.org extractor execution.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from base.test_utils import parse_jsonl_output, parse_jsonl_records

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
                sys.executable,
                str(ARCHIVEDOTORG_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test789",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )

        assert result.returncode in (0, 1)

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        if result.returncode == 0:
            # Success - should have ArchiveResult
            assert result_json, "Should have ArchiveResult JSONL output on success"
            assert result_json["status"] == "succeeded", (
                f"Should succeed: {result_json}"
            )
        else:
            # Transient errors still emit failed ArchiveResult JSONL.
            assert result_json, "Should emit failed ArchiveResult JSONL on error"
            assert result_json["status"] == "failed", result_json
            assert result.stderr, "Should have error message in stderr"


def test_config_save_archivedotorg_false_skips():
    with tempfile.TemporaryDirectory() as tmpdir:
        import os

        env = os.environ.copy()
        env["ARCHIVEDOTORG_ENABLED"] = "False"

        result = subprocess.run(
            [
                sys.executable,
                str(ARCHIVEDOTORG_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test999",
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

        records = parse_jsonl_records(result.stdout)
        assert len(records) == 1, f"Expected exactly one JSONL record, got: {records}"
        result_json = records[0]
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "ARCHIVEDOTORG_ENABLED=False", result_json


def test_handles_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        import os

        env = os.environ.copy()
        env["TIMEOUT"] = "1"

        result = subprocess.run(
            [
                sys.executable,
                str(ARCHIVEDOTORG_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "testtimeout",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        # Timeout is a transient error - should exit 1 with failed JSONL
        assert result.returncode in (0, 1), "Should complete without hanging"

        # With a 1s timeout the hook may time out or get an HTTP error from
        # archive.org (e.g. 403).  Either way it should emit proper JSONL.
        if result.returncode == 1:
            records = parse_jsonl_records(result.stdout)
            assert len(records) == 1, f"Should emit exactly one failed JSONL record, got: {records}"
            result_json = records[0]
            assert result_json["status"] == "failed", result_json
            assert result_json["output_str"], "Should include error description"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
