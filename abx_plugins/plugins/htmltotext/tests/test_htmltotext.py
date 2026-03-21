"""
Integration tests for htmltotext plugin

Tests verify standalone htmltotext extractor execution.
"""

import os
import subprocess
import tempfile
from pathlib import Path
import pytest

from abx_plugins.plugins.base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
_HTMLTOTEXT_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_htmltotext.*"), None)
if _HTMLTOTEXT_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
HTMLTOTEXT_HOOK = _HTMLTOTEXT_HOOK
TEST_URL = "https://example.com"


def test_hook_script_exists():
    assert HTMLTOTEXT_HOOK.exists()


def test_extracts_text_from_html():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        # Create HTML source
        (snap_dir / "singlefile").mkdir(parents=True, exist_ok=True)
        (snap_dir / "singlefile" / "singlefile.html").write_text(
            "<html><body><h1>Example Domain</h1><p>This domain is for examples.</p></body></html>",
        )

        result = subprocess.run(
            [
                str(HTMLTOTEXT_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test789",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Verify output file (hook writes to current directory)
        output_file = snap_dir / "htmltotext" / "htmltotext.txt"
        assert output_file.exists(), (
            f"htmltotext.txt not created. Files: {list(snap_dir.rglob('*'))}"
        )
        content = output_file.read_text()
        assert len(content) > 0, "Content should not be empty"
        assert "Example Domain" in content, "Should contain text from HTML"


def test_fails_gracefully_without_html():
    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        result = subprocess.run(
            [
                str(HTMLTOTEXT_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test999",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should emit ArchiveResult JSONL"
        assert result_json["status"] == "noresults", (
            f"Should noresult without HTML source: {result_json}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
