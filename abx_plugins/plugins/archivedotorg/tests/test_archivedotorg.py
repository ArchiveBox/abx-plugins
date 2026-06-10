"""
Integration tests for archivedotorg plugin

Tests verify standalone archive.org extractor execution.
"""

import os
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
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
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
        assert archived_url.startswith("https://web.archive.org/"), archived_url


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


def test_handles_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(tmpdir)
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

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit ArchiveResult JSONL"
        assert result_json == {
            "type": "ArchiveResult",
            "status": "succeeded",
            "output_str": "archivedotorg/archive.org.txt",
        }, result_json
        output_path = tmpdir / "archivedotorg" / "archive.org.txt"
        assert output_path.is_file(), f"Archive.org output missing: {output_path}"
        archived_url = output_path.read_text(encoding="utf-8").strip()
        assert archived_url.startswith("https://web.archive.org/"), archived_url


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
