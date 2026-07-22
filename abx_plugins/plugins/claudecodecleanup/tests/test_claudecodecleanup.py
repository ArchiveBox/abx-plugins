"""
Tests for the claudecodecleanup plugin.

Tests verify:
1. Hook script exists
2. Config schema is valid and declares claudecode dependency
3. Hook runs at priority 92 (before hashes at 93)
4. Hook skips when disabled
5. Hook fails gracefully when API key is missing
6. Full cleanup pipeline runs against real snapshot with duplicates (integration, requires Claude Code auth)
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
    run_hook,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_CLEANUP_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudecodecleanup*")
if _CLEANUP_HOOK is None:
    raise FileNotFoundError(f"Cleanup hook not found in {PLUGIN_DIR}")
CLEANUP_HOOK = _CLEANUP_HOOK
TEST_URL = "https://example.com"


def create_snapshot_with_real_outputs(
    root: Path,
    snapshot_id: str,
    real_html_snapshot,
) -> Path:
    """Run real extractors, one real failed extractor, and hashes."""
    snap_dir = real_html_snapshot(root, TEST_URL, snapshot_id)
    env = os.environ.copy()
    env["SNAP_DIR"] = str(snap_dir)

    for plugin_name in ("readability", "htmltotext", "mercury"):
        plugin_dir = PLUGIN_DIR.parent / plugin_name
        hook = get_hook_script(plugin_dir, f"on_Snapshot__*_{plugin_name}.*")
        assert hook is not None
        output_dir = snap_dir / plugin_name
        output_dir.mkdir()
        returncode, stdout, stderr = run_hook(
            hook,
            TEST_URL,
            snapshot_id,
            cwd=output_dir,
            env=env,
            timeout=120,
        )
        record = parse_jsonl_output(stdout)
        assert returncode == 0, stderr
        assert record is not None and record["status"] == "succeeded", record
        (output_dir / "stdout.log").write_text(stdout)

    failed_dir = snap_dir / "screenshot"
    failed_dir.mkdir()
    screenshot_hook = get_hook_script(
        PLUGIN_DIR.parent / "screenshot",
        "on_Snapshot__*_screenshot.*",
    )
    assert screenshot_hook is not None
    returncode, stdout, stderr = run_hook(
        screenshot_hook,
        TEST_URL,
        snapshot_id,
        cwd=failed_dir,
        env=env,
        timeout=30,
    )
    assert returncode != 0, (stdout, stderr)
    (failed_dir / "stdout.log").write_text(stdout)
    (failed_dir / "stderr.log").write_text(stderr)

    hashes_dir = snap_dir / "hashes"
    hashes_dir.mkdir()
    hashes_hook = get_hook_script(
        PLUGIN_DIR.parent / "hashes",
        "on_Snapshot__*_hashes.*",
    )
    assert hashes_hook is not None
    returncode, stdout, stderr = run_hook(
        hashes_hook,
        TEST_URL,
        snapshot_id,
        cwd=hashes_dir,
        env=env,
        timeout=30,
    )
    record = parse_jsonl_output(stdout)
    assert returncode == 0, stderr
    assert record is not None and record["status"] == "succeeded", record
    return snap_dir


class TestClaudeCodeCleanupPlugin:
    """Test the claudecodecleanup plugin."""

    def test_hook_exists(self):
        """Hook script should exist."""
        assert CLEANUP_HOOK.exists(), f"Hook not found: {CLEANUP_HOOK}"

    def test_hook_runs_at_priority_92(self):
        """Hook should be at priority 92 (after extractors, before hashes at 93)."""
        assert "__92_" in CLEANUP_HOOK.name, (
            f"Expected priority 92 in hook name: {CLEANUP_HOOK.name}"
        )

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
        assert (
            "duplicate" in default_prompt.lower()
            or "redundant" in default_prompt.lower()
        )

    def test_config_has_higher_max_turns_than_extract(self):
        """Cleanup should have higher default max turns than extract."""
        cleanup_config = json.loads((PLUGIN_DIR / "config.json").read_text())
        extract_config_path = PLUGIN_DIR.parent / "claudecodeextract" / "config.json"
        extract_config = json.loads(extract_config_path.read_text())

        cleanup_max = cleanup_config["properties"]["CLAUDECODECLEANUP_MAX_TURNS"][
            "default"
        ]
        extract_max = extract_config["properties"]["CLAUDECODEEXTRACT_MAX_TURNS"][
            "default"
        ]
        assert cleanup_max >= extract_max, (
            f"Cleanup max_turns ({cleanup_max}) should be >= extract max_turns ({extract_max})"
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

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=output_dir,
                env=env,
                timeout=30,
            )

            assert returncode == 0, f"Hook failed: {stderr}"
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "skipped"

    def test_hook_reads_snapshot_id_from_extra_context_when_cli_flag_missing(self):
        """Hook should not require --snapshot-id when EXTRA_CONTEXT provides it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "false"
            env["EXTRA_CONTEXT"] = json.dumps({"snapshot_id": "ctx-snapshot"})

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                None,
                cwd=output_dir,
                env=env,
                timeout=30,
            )

            assert returncode == 0, f"Hook failed: {stderr}"
            assert "Missing option '--snapshot-id'" not in stderr
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "skipped"

    def test_hook_fails_without_api_key(self):
        """Hook should fail when no Claude Code credential is set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=output_dir,
                env=env,
                timeout=30,
            )

            assert returncode == 1
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "failed"
            assert "auth" in result["output_str"]


@pytest.mark.usefixtures("ensure_claude_code_prereqs")
class TestClaudeCodeCleanupIntegration:
    """Integration tests that run the full cleanup pipeline with real Claude Code.

    These tests require claude binary in PATH and ANTHROPIC_API_KEY or
    CLAUDE_CODE_OAUTH_TOKEN set.
    """

    def test_cleanup_produces_report(self, real_html_snapshot):
        """Cleanup hook should analyze snapshot and produce a cleanup report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = create_snapshot_with_real_outputs(
                Path(tmpdir),
                "test-cleanup-integration",
                real_html_snapshot,
            )

            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env["CLAUDECODECLEANUP_MODEL"] = "haiku"
            env["CLAUDECODECLEANUP_MAX_TURNS"] = "25"
            env["CLAUDECODECLEANUP_TIMEOUT"] = "180"

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-cleanup-integration",
                cwd=output_dir,
                env=env,
                timeout=180,
            )

            result = parse_jsonl_output(stdout)
            assert result is not None, f"No ArchiveResult. stderr: {stderr[:500]}"
            assert result["status"] == "succeeded", (
                f"Cleanup should succeed. status={result['status']}, "
                f"output={result.get('output_str', '')}, stderr: {stderr[:500]}"
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
            assert (snap_dir / "hashes" / "hashes.json").exists(), (
                "hashes.json should be preserved"
            )

    def test_cleanup_preserves_hashes(self, real_html_snapshot):
        """Cleanup should delete redundant outputs but never delete hashes/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = create_snapshot_with_real_outputs(
                Path(tmpdir),
                "test-preserve-hashes",
                real_html_snapshot,
            )

            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env["CLAUDECODECLEANUP_MODEL"] = "haiku"
            env["CLAUDECODECLEANUP_MAX_TURNS"] = "25"
            env["CLAUDECODECLEANUP_TIMEOUT"] = "90"
            env["CLAUDECODECLEANUP_PROMPT"] = (
                "Delete the screenshot/ directory because its real extractor run failed. "
                "Do NOT delete hashes/ or any other directories. "
                "Write a summary of what you deleted to "
                f"{output_dir}/cleanup_report.txt"
            )

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-preserve-hashes",
                cwd=output_dir,
                env=env,
                timeout=180,
            )

            result = parse_jsonl_output(stdout)
            assert result is not None, f"No ArchiveResult. stderr: {stderr[:500]}"
            assert result["status"] == "succeeded", f"Should succeed: {stderr[:500]}"

            assert not (snap_dir / "screenshot").exists(), (
                "failed screenshot output should have been deleted by cleanup"
            )

            # Verify hashes preserved (must survive even when deletion is enabled)
            assert (snap_dir / "hashes").exists(), "hashes/ must be preserved"
            assert (snap_dir / "hashes" / "hashes.json").exists(), (
                "hashes.json must be preserved"
            )

            # Verify cleanup report was written
            report_file = output_dir / "cleanup_report.txt"
            assert report_file.exists(), (
                f"Should create cleanup_report.txt. Dir: {list(output_dir.iterdir())}"
            )
            report_text = report_file.read_text()
            assert len(report_text) > 20, "Report should contain analysis"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
