"""
Tests for the claudecodeextract plugin.

Tests verify:
1. Hook script exists
2. Config schema is valid and declares claudecode dependency
3. Hook skips when disabled
4. Hook fails gracefully when API key is missing
5. Hook fails gracefully when claude binary is not found
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
_EXTRACT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudecodeextract*")
if _EXTRACT_HOOK is None:
    raise FileNotFoundError(f"Extract hook not found in {PLUGIN_DIR}")
EXTRACT_HOOK = _EXTRACT_HOOK
TEST_URL = "https://example.com"


class TestClaudeCodeExtractPlugin:
    """Test the claudecodeextract plugin."""

    def test_hook_exists(self):
        """Hook script should exist."""
        assert EXTRACT_HOOK.exists(), f"Hook not found: {EXTRACT_HOOK}"

    def test_config_json_exists_and_valid(self):
        """config.json should exist and declare claudecode dependency."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "claudecode" in config.get("required_plugins", [])
        assert "CLAUDECODEEXTRACT_ENABLED" in config["properties"]
        assert "CLAUDECODEEXTRACT_PROMPT" in config["properties"]
        assert "CLAUDECODEEXTRACT_MODEL" in config["properties"]
        assert "CLAUDECODEEXTRACT_TIMEOUT" in config["properties"]
        assert "CLAUDECODEEXTRACT_MAX_TURNS" in config["properties"]

    def test_config_has_default_prompt(self):
        """Config should have a sensible default prompt."""
        config_path = PLUGIN_DIR / "config.json"
        config = json.loads(config_path.read_text())
        default_prompt = config["properties"]["CLAUDECODEEXTRACT_PROMPT"]["default"]
        assert len(default_prompt) > 50, "Default prompt should be meaningful"
        assert "markdown" in default_prompt.lower() or "Markdown" in default_prompt

    def test_templates_exist(self):
        """Template files should exist."""
        templates_dir = PLUGIN_DIR / "templates"
        assert (templates_dir / "icon.html").exists()
        assert (templates_dir / "card.html").exists()
        assert (templates_dir / "full.html").exists()

    def test_hook_skips_when_disabled(self):
        """Hook should skip when CLAUDECODEEXTRACT_ENABLED=false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODEEXTRACT_ENABLED"] = "false"

            result = subprocess.run(
                [
                    sys.executable,
                    str(EXTRACT_HOOK),
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
            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODEEXTRACT_ENABLED"] = "true"
            env.pop("ANTHROPIC_API_KEY", None)

            result = subprocess.run(
                [
                    sys.executable,
                    str(EXTRACT_HOOK),
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
            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODEEXTRACT_ENABLED"] = "true"
            env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"
            env["CLAUDECODE_BINARY"] = "/nonexistent/claude"

            result = subprocess.run(
                [
                    sys.executable,
                    str(EXTRACT_HOOK),
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
