"""
Tests for the claudecodecleanup plugin.

Tests verify:
1. Hook script exists
2. Config schema is valid and declares claudecode dependency
3. Hook runs at priority 92 (before hashes at 93)
4. Hook skips when disabled
5. Hook fails gracefully when API key is missing
6. Hook fails gracefully when claude binary is not found
7. Full cleanup pipeline runs against real snapshot with duplicates (integration, requires ANTHROPIC_API_KEY)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_plugin_dir,
    get_hook_script,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_CLEANUP_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudecodecleanup*")
if _CLEANUP_HOOK is None:
    raise FileNotFoundError(f"Cleanup hook not found in {PLUGIN_DIR}")
CLEANUP_HOOK = _CLEANUP_HOOK
TEST_URL = "https://example.com"



def create_snapshot_with_duplicates(snap_dir: Path) -> dict:
    """Create a snapshot directory with intentionally redundant/duplicate outputs.

    Returns a dict describing what was created for verification.
    """
    created = {"dirs": [], "files": []}

    # readability - good quality article extraction
    readability_dir = snap_dir / "readability"
    readability_dir.mkdir(parents=True)
    (readability_dir / "content.html").write_text("""
<div id="readability-page-1">
    <h1>Example Domain</h1>
    <p>This domain is for use in illustrative examples in documents.
    You may use this domain in literature without prior coordination
    or asking for permission.</p>
    <p>More information about example domains can be found at the
    IANA website.</p>
</div>
    """)
    (readability_dir / "content.txt").write_text(
        "Example Domain\n\n"
        "This domain is for use in illustrative examples in documents. "
        "You may use this domain in literature without prior coordination "
        "or asking for permission.\n\n"
        "More information about example domains can be found at the IANA website.\n"
    )
    (readability_dir / "article.json").write_text(json.dumps({
        "title": "Example Domain",
        "byline": None,
        "siteName": "example.com",
    }, indent=2))
    created["dirs"].append("readability")

    # htmltotext - lower quality text extraction (subset of readability)
    htmltotext_dir = snap_dir / "htmltotext"
    htmltotext_dir.mkdir()
    (htmltotext_dir / "content.txt").write_text(
        "Example Domain\n"
        "This domain is for use in illustrative examples.\n"
    )
    created["dirs"].append("htmltotext")

    # mercury - another article extraction (redundant with readability)
    mercury_dir = snap_dir / "mercury"
    mercury_dir.mkdir()
    (mercury_dir / "content.html").write_text(
        "<div><h1>Example Domain</h1>"
        "<p>This domain is for use in illustrative examples.</p></div>"
    )
    (mercury_dir / "content.txt").write_text(
        "Example Domain\nThis domain is for use in illustrative examples.\n"
    )
    (mercury_dir / "article.json").write_text(json.dumps({
        "title": "Example Domain",
        "content": "<p>This domain is for use in illustrative examples.</p>",
    }, indent=2))
    created["dirs"].append("mercury")

    # dom - raw DOM dump (large, mostly noise)
    dom_dir = snap_dir / "dom"
    dom_dir.mkdir()
    (dom_dir / "output.html").write_text("""
<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>Example Domain</title>
<style>body{background:#f0f0f2;margin:0}</style>
</head><body>
<div><h1>Example Domain</h1>
<p>This domain is for use in illustrative examples in documents.</p>
</div></body></html>
    """)
    created["dirs"].append("dom")

    # An empty/broken extractor output
    broken_dir = snap_dir / "broken_extractor"
    broken_dir.mkdir()
    (broken_dir / ".tmp.partial").write_text("")  # incomplete temp file
    created["dirs"].append("broken_extractor")

    # hashes - should NOT be deleted
    hashes_dir = snap_dir / "hashes"
    hashes_dir.mkdir()
    (hashes_dir / "hashes.json").write_text(json.dumps({
        "root_hash": "abc123",
        "files": [],
        "metadata": {"file_count": 0},
    }))
    created["dirs"].append("hashes")

    return created


class TestClaudeCodeCleanupPlugin:
    """Test the claudecodecleanup plugin."""

    def test_hook_exists(self):
        """Hook script should exist."""
        assert CLEANUP_HOOK.exists(), f"Hook not found: {CLEANUP_HOOK}"

    def test_hook_runs_at_priority_92(self):
        """Hook should be at priority 92 (after extractors, before hashes at 93)."""
        assert "__92_" in CLEANUP_HOOK.name, f"Expected priority 92 in hook name: {CLEANUP_HOOK.name}"

    def test_config_json_exists_and_valid(self):
        """config.json should exist and declare claudecode dependency."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "claudecode" in config.get("required_plugins", [])
        assert "CLAUDECODECLEANUP_ENABLED" in config["properties"]
        assert "CLAUDECODECLEANUP_PROMPT" in config["properties"]
        assert "CLAUDECODECLEANUP_MODEL" in config["properties"]
        assert "CLAUDECODECLEANUP_TIMEOUT" in config["properties"]
        assert "CLAUDECODECLEANUP_MAX_TURNS" in config["properties"]

    def test_config_has_default_prompt(self):
        """Config should have a sensible default prompt about cleanup."""
        config_path = PLUGIN_DIR / "config.json"
        config = json.loads(config_path.read_text())
        default_prompt = config["properties"]["CLAUDECODECLEANUP_PROMPT"]["default"]
        assert len(default_prompt) > 50, "Default prompt should be meaningful"
        assert "duplicate" in default_prompt.lower() or "redundant" in default_prompt.lower()

    def test_config_has_higher_max_turns_than_extract(self):
        """Cleanup should have higher default max turns than extract."""
        cleanup_config = json.loads((PLUGIN_DIR / "config.json").read_text())
        extract_config_path = PLUGIN_DIR.parent / "claudecodeextract" / "config.json"
        extract_config = json.loads(extract_config_path.read_text())

        cleanup_max = cleanup_config["properties"]["CLAUDECODECLEANUP_MAX_TURNS"]["default"]
        extract_max = extract_config["properties"]["CLAUDECODEEXTRACT_MAX_TURNS"]["default"]
        assert cleanup_max > extract_max, (
            f"Cleanup max_turns ({cleanup_max}) should exceed extract max_turns ({extract_max})"
        )

    def test_templates_exist(self):
        """Template files should exist."""
        templates_dir = PLUGIN_DIR / "templates"
        assert (templates_dir / "icon.html").exists()
        assert (templates_dir / "card.html").exists()
        assert (templates_dir / "full.html").exists()

    def test_hook_skips_when_disabled(self):
        """Hook should skip when CLAUDECODECLEANUP_ENABLED=false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "false"

            result = subprocess.run(
                [
                    sys.executable,
                    str(CLEANUP_HOOK),
                    "--url", TEST_URL,
                    "--snapshot-id", "test-snapshot",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=30,
            )

            assert result.returncode == 0, f"Hook failed: {result.stderr}"
            assert "skipped" in result.stdout

    def test_hook_fails_without_api_key(self):
        """Hook should fail when ANTHROPIC_API_KEY is not set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env.pop("ANTHROPIC_API_KEY", None)

            result = subprocess.run(
                [
                    sys.executable,
                    str(CLEANUP_HOOK),
                    "--url", TEST_URL,
                    "--snapshot-id", "test-snapshot",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=30,
            )

            assert result.returncode == 1
            records = [
                json.loads(line)
                for line in result.stdout.strip().split("\n")
                if line.strip().startswith("{")
            ]
            assert records
            assert records[-1]["type"] == "ArchiveResult"
            assert records[-1]["status"] == "failed"
            assert "ANTHROPIC_API_KEY" in records[-1]["output_str"]

    def test_hook_fails_gracefully_with_missing_binary(self):
        """Hook should fail gracefully when claude binary is not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"
            env["CLAUDECODE_BINARY"] = "/nonexistent/claude"

            result = subprocess.run(
                [
                    sys.executable,
                    str(CLEANUP_HOOK),
                    "--url", TEST_URL,
                    "--snapshot-id", "test-snapshot",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=30,
            )

            assert result.returncode == 1
            records = [
                json.loads(line)
                for line in result.stdout.strip().split("\n")
                if line.strip().startswith("{")
            ]
            assert records
            assert records[-1]["type"] == "ArchiveResult"
            assert records[-1]["status"] == "failed"
            assert "not found" in records[-1]["output_str"].lower(), (
                f"Error should mention missing binary: {records[-1]['output_str']}"
            )


class TestClaudeCodeCleanupIntegration:
    """Integration tests that run the full cleanup pipeline with real Claude Code.

    These tests require claude binary in PATH and ANTHROPIC_API_KEY set.
    No skip decorators — CI always has these prerequisites configured.
    """

    def test_cleanup_produces_report(self):
        """Cleanup hook should analyze snapshot and produce a cleanup report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            create_snapshot_with_duplicates(snap_dir)

            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env["CLAUDECODECLEANUP_MODEL"] = "haiku"
            env["CLAUDECODECLEANUP_MAX_TURNS"] = "10"
            env["CLAUDECODECLEANUP_TIMEOUT"] = "120"

            result = subprocess.run(
                [
                    sys.executable,
                    str(CLEANUP_HOOK),
                    "--url", TEST_URL,
                    "--snapshot-id", "test-cleanup-integration",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=180,
            )

            # Parse JSONL output
            records = [
                json.loads(line)
                for line in result.stdout.strip().split("\n")
                if line.strip().startswith("{")
            ]
            archive_results = [r for r in records if r.get("type") == "ArchiveResult"]
            assert archive_results, f"No ArchiveResult. stderr: {result.stderr[:500]}"

            ar = archive_results[-1]
            assert ar["status"] == "succeeded", (
                f"Cleanup should succeed. status={ar['status']}, "
                f"output={ar.get('output_str', '')}, stderr: {result.stderr[:500]}"
            )

            # Should produce cleanup_report.txt
            report_file = output_dir / "cleanup_report.txt"
            assert report_file.exists(), (
                f"Should create cleanup_report.txt. Dir: {list(output_dir.iterdir())}"
            )
            report_text = report_file.read_text()
            assert len(report_text) > 20, "Cleanup report should contain analysis"

            # hashes/ directory should NOT be deleted
            assert (snap_dir / "hashes").exists(), "hashes/ should be preserved"
            assert (snap_dir / "hashes" / "hashes.json").exists(), "hashes.json should be preserved"

    def test_cleanup_preserves_hashes(self):
        """Cleanup should delete redundant outputs but never delete hashes/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            create_snapshot_with_duplicates(snap_dir)

            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env["CLAUDECODECLEANUP_MODEL"] = "haiku"
            env["CLAUDECODECLEANUP_MAX_TURNS"] = "10"
            env["CLAUDECODECLEANUP_TIMEOUT"] = "90"
            env["CLAUDECODECLEANUP_PROMPT"] = (
                "Delete the broken_extractor/ directory (it contains only incomplete temp files). "
                "Do NOT delete hashes/ or any other directories. "
                "Write a summary of what you deleted to "
                f"{output_dir}/cleanup_report.txt"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(CLEANUP_HOOK),
                    "--url", TEST_URL,
                    "--snapshot-id", "test-preserve-hashes",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=180,
            )

            records = [
                json.loads(line)
                for line in result.stdout.strip().split("\n")
                if line.strip().startswith("{")
            ]
            archive_results = [r for r in records if r.get("type") == "ArchiveResult"]
            assert archive_results, f"No ArchiveResult. stderr: {result.stderr[:500]}"
            assert archive_results[-1]["status"] == "succeeded", (
                f"Should succeed: {result.stderr[:500]}"
            )

            # Verify broken_extractor/ was actually deleted
            assert not (snap_dir / "broken_extractor").exists(), (
                "broken_extractor/ should have been deleted by cleanup"
            )

            # Verify hashes preserved (must survive even when deletion is enabled)
            assert (snap_dir / "hashes").exists(), "hashes/ must be preserved"
            assert (snap_dir / "hashes" / "hashes.json").exists(), "hashes.json must be preserved"

            # Verify cleanup report was written
            report_file = output_dir / "cleanup_report.txt"
            assert report_file.exists(), (
                f"Should create cleanup_report.txt. Dir: {list(output_dir.iterdir())}"
            )
            report_text = report_file.read_text()
            assert len(report_text) > 20, "Report should contain analysis"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
