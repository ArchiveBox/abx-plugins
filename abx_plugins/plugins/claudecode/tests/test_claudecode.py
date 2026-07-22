"""
Tests for the claudecode base plugin.

Tests verify:
1. Hook scripts and utility modules exist
2. Config schema is valid
3. required_binaries declares the Claude CLI dependency correctly
4. dependency preflight respects CLAUDECODE_ENABLED
5. dependency preflight warns when Claude Code auth is missing
6. Utility functions work correctly (system prompt building, metadata)
7. Claude Code CLI actually runs and responds (integration, requires Claude Code auth)
"""

import json
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
)
from abx_plugins.plugins.base.testing import (
    get_plugin_dir,
)
from abx_plugins.plugins.claudecode.claudecode_utils import (
    build_system_prompt,
    get_crawl_metadata,
    get_snapshot_metadata,
    run_claude_code,
)


PLUGIN_DIR = get_plugin_dir(__file__)


class TestClaudeCodePlugin:
    """Test the claudecode base plugin."""

    def test_config_json_exists_and_valid(self):
        """config.json should exist and be valid JSON schema."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "CLAUDECODE_ENABLED" in config["properties"]
        assert "ANTHROPIC_API_KEY" in config["properties"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" in config["properties"]
        assert "CLAUDECODE_BINARY" in config["properties"]
        assert "CLAUDECODE_MODEL" in config["properties"]
        assert "CLAUDECODE_TIMEOUT" in config["properties"]
        assert "CLAUDECODE_MAX_TURNS" in config["properties"]

    def test_utils_module_exists(self):
        """claudecode_utils.py should exist."""
        utils_path = PLUGIN_DIR / "claudecode_utils.py"
        assert utils_path.exists(), "claudecode_utils.py not found"

    def test_templates_exist(self):
        """Template files should exist."""
        templates_dir = PLUGIN_DIR / "templates"
        assert (templates_dir / "icon.html").exists()
        assert (templates_dir / "card.html").exists()
        assert (templates_dir / "full.html").exists()


class TestClaudeCodeUtils:
    """Test the claudecode_utils module."""

    def test_import_utils(self):
        """Should be able to import claudecode_utils."""
        assert callable(build_system_prompt)
        assert callable(emit_archive_result_record)
        assert callable(get_crawl_metadata)
        assert callable(get_snapshot_metadata)

    def test_build_system_prompt_basic(self):
        """System prompt should contain key sections."""
        prompt = build_system_prompt()
        assert "ArchiveBox" in prompt
        assert "Directory Layout" in prompt
        assert "Snapshot Directory Layout" in prompt
        assert "CRAWL_DIR" in prompt
        assert "SNAP_DIR" in prompt

    def test_build_system_prompt_with_snap_dir(self):
        """System prompt should inventory existing checked-in plugin assets."""
        plugins_dir = PLUGIN_DIR.parent
        prompt = build_system_prompt(snap_dir=plugins_dir)
        assert "readability" in prompt
        assert "screenshot" in prompt
        assert "config.json" in prompt

    def test_build_system_prompt_with_extra_context(self):
        """System prompt should include extra context when provided."""
        prompt = build_system_prompt(extra_context="Custom instructions here")
        assert "Custom instructions here" in prompt
        assert "Additional Instructions" in prompt

    def test_get_snapshot_metadata(self):
        """Should collect metadata from existing checked-in plugin assets."""
        meta = get_snapshot_metadata(PLUGIN_DIR.parent)
        assert "extractor_outputs" in meta
        names = [e["name"] for e in meta["extractor_outputs"]]
        assert "dom" in names
        assert "mercury" in names


@pytest.mark.usefixtures("ensure_claude_code_prereqs")
class TestClaudeCodeIntegration:
    """Integration tests that actually run Claude Code CLI.

    These tests require claude binary in PATH and ANTHROPIC_API_KEY or
    CLAUDE_CODE_OAUTH_TOKEN set.
    """

    def test_run_claude_code_simple_prompt(self):
        """Claude Code CLI should respond to a simple prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout, stderr, returncode = run_claude_code(
                prompt="Respond with exactly: ARCHIVEBOX_TEST_OK",
                work_dir=tmpdir,
                timeout=60,
                max_turns=1,
                model="haiku",
            )

            assert returncode == 0, (
                f"Claude Code failed (rc={returncode}): "
                f"stdout={stdout[:1000]!r} stderr={stderr[:1000]!r}"
            )
            assert len(stdout.strip()) > 0, "Claude Code returned empty response"
            assert "ARCHIVEBOX_TEST_OK" in stdout, (
                f"Expected 'ARCHIVEBOX_TEST_OK' in response, got: {stdout[:200]}"
            )

    def test_run_claude_code_with_system_prompt(self):
        """Claude Code CLI should respect system prompts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir)
            (snap_dir / "readability").mkdir()
            (snap_dir / "readability" / "content.txt").write_text(
                "This is example.com content about domains.",
            )

            system_prompt = build_system_prompt(snap_dir=snap_dir)

            stdout, stderr, returncode = run_claude_code(
                prompt=(
                    "List the extractor output directories you can see in "
                    "the snapshot. Respond with just the directory names, "
                    "one per line."
                ),
                work_dir=tmpdir,
                system_prompt=system_prompt,
                timeout=60,
                max_turns=5,
                model="haiku",
            )

            assert returncode == 0, (
                f"Claude Code failed (rc={returncode}): "
                f"stdout={stdout[:1000]!r} stderr={stderr[:1000]!r}"
            )
            assert "readability" in stdout.lower(), (
                f"Claude should see readability dir, got: {stdout[:200]}"
            )

    def test_run_claude_code_writes_file(self):
        """Claude Code CLI should be able to write output files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "output"
            output_dir.mkdir()

            stdout, stderr, returncode = run_claude_code(
                prompt=(
                    f"Use exactly one Write tool call to write the text 'hello from claude' "
                    f"to {output_dir}/test_output.txt. Do not inspect, read, or verify the "
                    "file. Stop immediately after the Write tool call succeeds."
                ),
                work_dir=tmpdir,
                timeout=60,
                max_turns=3,
                model="haiku",
                allowed_tools=["Write"],
            )

            assert returncode == 0, (
                f"Claude Code failed (rc={returncode}): "
                f"stdout={stdout[:1000]!r} stderr={stderr[:1000]!r}"
            )

            output_file = output_dir / "test_output.txt"
            assert output_file.exists(), (
                f"Claude should have created test_output.txt. "
                f"stdout: {stdout[:300]}, stderr: {stderr[:300]}"
            )
            content = output_file.read_text()
            assert "hello from claude" in content.lower(), (
                f"Expected 'hello from claude' in file, got: {content[:200]}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
