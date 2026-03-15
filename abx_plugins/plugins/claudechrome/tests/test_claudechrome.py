"""
Tests for the claudechrome plugin.

Tests verify:
1. Hook scripts exist (install, config, snapshot)
2. Config schema is valid and declares chrome dependency
3. Snapshot hook skips when disabled
4. Snapshot hook fails gracefully when API key is missing
5. Templates exist
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

# Find hooks
_INSTALL_HOOK = get_hook_script(PLUGIN_DIR, "on_Crawl__*_claudechrome_install*")
_CONFIG_HOOK = get_hook_script(PLUGIN_DIR, "on_Crawl__*_claudechrome_config*")
_SNAPSHOT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudechrome*")

if _SNAPSHOT_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
SNAPSHOT_HOOK = _SNAPSHOT_HOOK
TEST_URL = "https://example.com"


class TestClaudeChromePlugin:
    """Test the claudechrome plugin."""

    def test_hook_exists(self):
        """All hook scripts should exist."""
        assert _INSTALL_HOOK is not None and _INSTALL_HOOK.exists(), "Install hook not found"
        assert _CONFIG_HOOK is not None and _CONFIG_HOOK.exists(), "Config hook not found"
        assert SNAPSHOT_HOOK.exists(), "Snapshot hook not found"

    def test_hook_runs_at_correct_priorities(self):
        """Hooks should run at the expected priorities."""
        assert _INSTALL_HOOK is not None
        assert "__84_" in _INSTALL_HOOK.name, f"Install hook should be priority 84: {_INSTALL_HOOK.name}"
        assert "__96_" in _CONFIG_HOOK.name, f"Config hook should be priority 96: {_CONFIG_HOOK.name}"
        assert "__47_" in SNAPSHOT_HOOK.name, f"Snapshot hook should be priority 47: {SNAPSHOT_HOOK.name}"

    def test_snapshot_runs_after_infiniscroll_before_singlefile(self):
        """Snapshot hook priority 47 is after infiniscroll (45) and before singlefile (50)."""
        # Extract priority number from hook filename
        name = SNAPSHOT_HOOK.name
        priority = int(name.split("__")[1].split("_")[0])
        assert 45 < priority < 50, (
            f"Priority {priority} should be between infiniscroll (45) and singlefile (50)"
        )

    def test_config_json_exists_and_valid(self):
        """config.json should exist and declare chrome dependency."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "chrome" in config.get("required_plugins", [])
        assert "CLAUDECHROME_ENABLED" in config["properties"]
        assert "CLAUDECHROME_PROMPT" in config["properties"]
        assert "CLAUDECHROME_TIMEOUT" in config["properties"]
        assert "CLAUDECHROME_MODEL" in config["properties"]
        assert "ANTHROPIC_API_KEY" in config["properties"]

    def test_config_has_default_prompt(self):
        """Config should have a sensible default prompt."""
        config_path = PLUGIN_DIR / "config.json"
        config = json.loads(config_path.read_text())
        default_prompt = config["properties"]["CLAUDECHROME_PROMPT"]["default"]
        assert len(default_prompt) > 30, "Default prompt should be meaningful"
        assert "expand" in default_prompt.lower() or "click" in default_prompt.lower()

    def test_templates_exist(self):
        """Template files should exist."""
        templates_dir = PLUGIN_DIR / "templates"
        assert (templates_dir / "icon.html").exists()
        assert (templates_dir / "card.html").exists()
        assert (templates_dir / "full.html").exists()

    def test_snapshot_hook_skips_when_disabled(self):
        """Snapshot hook should skip when CLAUDECHROME_ENABLED=false."""
        env = os.environ.copy()
        env["SNAP_DIR"] = tempfile.mkdtemp()
        env["CLAUDECHROME_ENABLED"] = "false"
        # Ensure NODE_MODULES_DIR is set so node can find puppeteer-core
        if "NODE_MODULES_DIR" not in env:
            from abx_plugins.plugins.chrome.tests.chrome_test_helpers import NODE_MODULES_DIR
            if NODE_MODULES_DIR:
                env["NODE_MODULES_DIR"] = str(NODE_MODULES_DIR)

        result = subprocess.run(
            [
                "node",
                str(SNAPSHOT_HOOK),
                "--url", TEST_URL,
                "--snapshot-id", "test-snapshot",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, f"Hook failed: {result.stderr}"
        assert "skipped" in result.stdout

    def test_snapshot_hook_fails_without_api_key(self):
        """Snapshot hook should fail when ANTHROPIC_API_KEY is not set."""
        env = os.environ.copy()
        env["SNAP_DIR"] = tempfile.mkdtemp()
        env["CLAUDECHROME_ENABLED"] = "true"
        env.pop("ANTHROPIC_API_KEY", None)
        if "NODE_MODULES_DIR" not in env:
            from abx_plugins.plugins.chrome.tests.chrome_test_helpers import NODE_MODULES_DIR
            if NODE_MODULES_DIR:
                env["NODE_MODULES_DIR"] = str(NODE_MODULES_DIR)

        result = subprocess.run(
            [
                "node",
                str(SNAPSHOT_HOOK),
                "--url", TEST_URL,
                "--snapshot-id", "test-snapshot",
            ],
            capture_output=True,
            text=True,
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


class TestClaudeChromeIntegration:
    """Integration tests requiring a Chrome session and API key.

    These tests require:
    - Chrome session running (chrome plugin)
    - ANTHROPIC_API_KEY set
    - Claude for Chrome extension installed
    """

    def test_full_pipeline_with_chrome_session(self):
        """Full pipeline: connect to Chrome, run Claude, capture output."""
        # This test is a placeholder - it requires a running Chrome session
        # which is set up by the chrome plugin during actual crawls.
        # It's included here for documentation and future CI with Chrome.
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
