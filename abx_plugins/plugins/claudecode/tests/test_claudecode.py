"""
Tests for the claudecode base plugin.

Tests verify:
1. Hook scripts and utility modules exist
2. Config schema is valid
3. Install hook emits correct Binary JSONL
4. Install hook respects CLAUDECODE_ENABLED
5. Install hook warns when ANTHROPIC_API_KEY is missing
6. Utility functions work correctly (system prompt building, metadata)
7. Claude Code CLI actually runs and responds (integration, requires ANTHROPIC_API_KEY)
"""

import json
import os
import shutil
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
_INSTALL_HOOK = get_hook_script(PLUGIN_DIR, "on_Crawl__*_claudecode_install*")
if _INSTALL_HOOK is None:
    raise FileNotFoundError(f"Install hook not found in {PLUGIN_DIR}")
INSTALL_HOOK = _INSTALL_HOOK

# Detect whether real Claude Code integration tests can run
CLAUDE_BINARY = shutil.which(os.environ.get("CLAUDECODE_BINARY", "claude"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CAN_RUN_CLAUDE = bool(CLAUDE_BINARY and ANTHROPIC_API_KEY)
SKIP_INTEGRATION = pytest.mark.skipif(
    not CAN_RUN_CLAUDE,
    reason="Integration tests require claude binary in PATH and ANTHROPIC_API_KEY set",
)


class TestClaudeCodePlugin:
    """Test the claudecode base plugin."""

    def test_install_hook_exists(self):
        """Install hook script should exist."""
        assert INSTALL_HOOK.exists(), f"Hook not found: {INSTALL_HOOK}"

    def test_config_json_exists_and_valid(self):
        """config.json should exist and be valid JSON schema."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "CLAUDECODE_ENABLED" in config["properties"]
        assert "ANTHROPIC_API_KEY" in config["properties"]
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

    def test_install_hook_emits_binary_record(self):
        """Install hook should emit Binary JSONL for claude."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["CRAWL_DIR"] = tmpdir
            env["CLAUDECODE_ENABLED"] = "true"
            env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"

            result = subprocess.run(
                [sys.executable, str(INSTALL_HOOK)],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )

            assert result.returncode == 0, f"Hook failed: {result.stderr}"

            records = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

            # Should emit Binary record for claude
            binary_records = [r for r in records if r.get("type") == "Binary"]
            assert len(binary_records) == 1, f"Expected 1 Binary record, got {len(binary_records)}"
            assert binary_records[0]["name"] == "claude"
            assert "npm" in binary_records[0]["binproviders"]
            assert binary_records[0]["overrides"]["npm"]["packages"] == ["@anthropic-ai/claude-code"]

            # Should emit ArchiveResult
            result_records = [r for r in records if r.get("type") == "ArchiveResult"]
            assert len(result_records) == 1
            assert result_records[0]["status"] == "succeeded"

    def test_install_hook_skips_when_disabled(self):
        """Install hook should exit cleanly when CLAUDECODE_ENABLED=false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["CRAWL_DIR"] = tmpdir
            env["CLAUDECODE_ENABLED"] = "false"

            result = subprocess.run(
                [sys.executable, str(INSTALL_HOOK)],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )

            assert result.returncode == 0, f"Hook failed: {result.stderr}"
            # Should not emit any Binary records
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        record = json.loads(line)
                        assert record.get("type") != "Binary", "Should not emit Binary when disabled"
                    except json.JSONDecodeError:
                        pass

    def test_install_hook_skips_by_default(self):
        """Install hook should skip when CLAUDECODE_ENABLED is not set (default=false)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["CRAWL_DIR"] = tmpdir
            env.pop("CLAUDECODE_ENABLED", None)

            result = subprocess.run(
                [sys.executable, str(INSTALL_HOOK)],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )

            assert result.returncode == 0, f"Hook failed: {result.stderr}"
            # Should not emit any Binary records
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        record = json.loads(line)
                        assert record.get("type") != "Binary", "Should not emit Binary when disabled by default"
                    except json.JSONDecodeError:
                        pass

    def test_install_hook_warns_missing_api_key(self):
        """Install hook should warn when ANTHROPIC_API_KEY is not set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["CRAWL_DIR"] = tmpdir
            env["CLAUDECODE_ENABLED"] = "true"
            env.pop("ANTHROPIC_API_KEY", None)

            result = subprocess.run(
                [sys.executable, str(INSTALL_HOOK)],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )

            assert result.returncode == 0
            assert "ANTHROPIC_API_KEY" in result.stderr, "Should warn about missing API key"


class TestClaudeCodeUtils:
    """Test the claudecode_utils module."""

    def test_import_utils(self):
        """Should be able to import claudecode_utils."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import (
                build_system_prompt,
                emit_archive_result,
                get_env,
                get_env_bool,
                get_env_int,
                get_crawl_metadata,
                get_snapshot_metadata,
            )
            assert callable(build_system_prompt)
            assert callable(emit_archive_result)
            assert callable(get_env)
            assert callable(get_env_bool)
            assert callable(get_env_int)
            assert callable(get_crawl_metadata)
            assert callable(get_snapshot_metadata)
        finally:
            sys.path.pop(0)

    def test_build_system_prompt_basic(self):
        """System prompt should contain key sections."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import build_system_prompt

            prompt = build_system_prompt()
            assert "ArchiveBox" in prompt
            assert "Directory Layout" in prompt
            assert "Snapshot Directory Layout" in prompt
            assert "CRAWL_DIR" in prompt
            assert "SNAP_DIR" in prompt
        finally:
            sys.path.pop(0)

    def test_build_system_prompt_with_snap_dir(self):
        """System prompt should include snapshot metadata when snap_dir provided."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import build_system_prompt

            with tempfile.TemporaryDirectory() as tmpdir:
                snap_dir = Path(tmpdir) / "snap"
                snap_dir.mkdir()

                # Create some fake extractor dirs
                (snap_dir / "readability").mkdir()
                (snap_dir / "readability" / "content.html").write_text("<p>test</p>")
                (snap_dir / "readability" / "content.txt").write_text("test")
                (snap_dir / "screenshot").mkdir()
                (snap_dir / "screenshot" / "screenshot.png").write_bytes(b"PNG")

                prompt = build_system_prompt(snap_dir=snap_dir)
                assert "readability" in prompt
                assert "screenshot" in prompt
                assert "content.html" in prompt
        finally:
            sys.path.pop(0)

    def test_build_system_prompt_with_extra_context(self):
        """System prompt should include extra context when provided."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import build_system_prompt

            prompt = build_system_prompt(extra_context="Custom instructions here")
            assert "Custom instructions here" in prompt
            assert "Additional Instructions" in prompt
        finally:
            sys.path.pop(0)

    def test_get_snapshot_metadata(self):
        """Should collect snapshot directory metadata."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import get_snapshot_metadata

            with tempfile.TemporaryDirectory() as tmpdir:
                snap_dir = Path(tmpdir)
                (snap_dir / "dom").mkdir()
                (snap_dir / "dom" / "output.html").write_text("<html>test</html>")
                (snap_dir / "mercury").mkdir()
                (snap_dir / "mercury" / "content.txt").write_text("text")

                meta = get_snapshot_metadata(snap_dir)
                assert "extractor_outputs" in meta
                names = [e["name"] for e in meta["extractor_outputs"]]
                assert "dom" in names
                assert "mercury" in names
        finally:
            sys.path.pop(0)


class TestClaudeCodeIntegration:
    """Integration tests that actually run Claude Code CLI.

    These tests require:
    - claude binary available in PATH
    - ANTHROPIC_API_KEY set in environment

    They are automatically skipped when these prerequisites are not met.
    """

    @SKIP_INTEGRATION
    def test_run_claude_code_simple_prompt(self):
        """Claude Code CLI should respond to a simple prompt."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import run_claude_code

            with tempfile.TemporaryDirectory() as tmpdir:
                stdout, stderr, returncode = run_claude_code(
                    prompt="Respond with exactly: ARCHIVEBOX_TEST_OK",
                    work_dir=tmpdir,
                    timeout=60,
                    max_turns=1,
                    model="haiku",
                )

                assert returncode == 0, f"Claude Code failed (rc={returncode}): {stderr}"
                assert len(stdout.strip()) > 0, "Claude Code returned empty response"
                assert "ARCHIVEBOX_TEST_OK" in stdout, (
                    f"Expected 'ARCHIVEBOX_TEST_OK' in response, got: {stdout[:200]}"
                )
        finally:
            sys.path.pop(0)

    @SKIP_INTEGRATION
    def test_run_claude_code_with_system_prompt(self):
        """Claude Code CLI should respect system prompts."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import run_claude_code, build_system_prompt

            with tempfile.TemporaryDirectory() as tmpdir:
                snap_dir = Path(tmpdir)
                (snap_dir / "readability").mkdir()
                (snap_dir / "readability" / "content.txt").write_text(
                    "This is example.com content about domains."
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

                assert returncode == 0, f"Claude Code failed (rc={returncode}): {stderr}"
                assert "readability" in stdout.lower(), (
                    f"Claude should see readability dir, got: {stdout[:200]}"
                )
        finally:
            sys.path.pop(0)

    @SKIP_INTEGRATION
    def test_run_claude_code_writes_file(self):
        """Claude Code CLI should be able to write output files."""
        sys.path.insert(0, str(PLUGIN_DIR.parent))
        try:
            from claudecode.claudecode_utils import run_claude_code

            with tempfile.TemporaryDirectory() as tmpdir:
                output_dir = Path(tmpdir) / "output"
                output_dir.mkdir()

                stdout, stderr, returncode = run_claude_code(
                    prompt=(
                        f"Write the text 'hello from claude' to the file "
                        f"{output_dir}/test_output.txt"
                    ),
                    work_dir=tmpdir,
                    timeout=60,
                    max_turns=3,
                    model="haiku",
                    allowed_tools=["Read", "Write", "Bash(cat:*)"],
                )

                assert returncode == 0, f"Claude Code failed (rc={returncode}): {stderr}"

                output_file = output_dir / "test_output.txt"
                assert output_file.exists(), (
                    f"Claude should have created test_output.txt. "
                    f"stdout: {stdout[:300]}, stderr: {stderr[:300]}"
                )
                content = output_file.read_text()
                assert "hello from claude" in content.lower(), (
                    f"Expected 'hello from claude' in file, got: {content[:200]}"
                )
        finally:
            sys.path.pop(0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
