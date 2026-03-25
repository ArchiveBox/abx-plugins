"""
Tests for the claudechrome plugin.

Tests verify:
1. Hook scripts exist (install, config, snapshot)
2. Config schema is valid and declares chrome dependency
3. Snapshot hook skips when disabled
4. Snapshot hook fails gracefully when API key is missing
5. Snapshot hook fails gracefully without Chrome session
6. Templates exist
7. Full integration: launches Chrome, runs Claude computer-use, produces output files
"""

import json
import tempfile
from pathlib import Path


import pytest

from abx_plugins.plugins.base.test_utils import (
    get_hydrated_required_binaries,
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
    run_hook,
)

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    chrome_session,
)


PLUGIN_DIR = get_plugin_dir(__file__)

_CONFIG_HOOK = get_hook_script(PLUGIN_DIR, "on_CrawlSetup__*_claudechrome_config*")
_SNAPSHOT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudechrome*")

if _SNAPSHOT_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
SNAPSHOT_HOOK = _SNAPSHOT_HOOK
TEST_URL = "https://example.com"
CHROME_STARTUP_TIMEOUT_SECONDS = 45

CLAUDECHROME_TEST_PAGE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Claude Chrome Test Page</title>
  <style>
    body { margin: 20px; font-family: sans-serif; }
    .hidden-content { display: none; }
    #expand-btn {
      padding: 10px 20px;
      font-size: 16px;
      cursor: pointer;
      background: #4a90d9;
      color: white;
      border: none;
      border-radius: 4px;
    }
  </style>
</head>
<body>
  <h1>Test Page for Claude Chrome</h1>
  <p>This page has a button that reveals hidden content.</p>
  <button id="expand-btn" onclick="document.getElementById('hidden').style.display='block'; this.textContent='Expanded!';">
    Show More
  </button>
  <div id="hidden" class="hidden-content">
    <p>This content was hidden and is now visible after clicking the button.</p>
  </div>
</body>
</html>
""".strip()


@pytest.fixture
def claudechrome_test_url(httpserver):
    """Serve a test page with a 'Show More' button for Claude to click."""
    httpserver.expect_request("/").respond_with_data(
        CLAUDECHROME_TEST_PAGE_HTML,
        content_type="text/html",
    )
    return httpserver.url_for("/")


class TestClaudeChromePlugin:
    """Test the claudechrome plugin."""

    def test_hook_exists(self):
        """All hook scripts should exist."""
        assert _CONFIG_HOOK is not None and _CONFIG_HOOK.exists(), (
            "Config hook not found"
        )
        assert SNAPSHOT_HOOK.exists(), "Snapshot hook not found"

    def test_hook_runs_at_correct_priorities(self):
        """Hooks should run at the expected priorities."""
        assert _CONFIG_HOOK is not None
        assert "__96_" in _CONFIG_HOOK.name, (
            f"Config hook should be priority 96: {_CONFIG_HOOK.name}"
        )
        assert "__47_" in SNAPSHOT_HOOK.name, (
            f"Snapshot hook should be priority 47: {SNAPSHOT_HOOK.name}"
        )

    def test_snapshot_runs_after_infiniscroll_before_singlefile(self):
        """Snapshot hook priority 47 is after infiniscroll (45) and before singlefile (50)."""
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

    def test_required_binaries_include_claudechrome(self):
        """required_binaries should declare the claudechrome extension asset."""
        env = get_test_env()
        required_binaries = get_hydrated_required_binaries(PLUGIN_DIR, env=env)
        claudechrome_binary = next(
            binary
            for binary in required_binaries
            if binary.get("name") == "claudechrome"
        )
        assert claudechrome_binary["binproviders"] == "chromewebstore"

    def test_config_hook_reports_skipped_when_disabled(self):
        """Config hook should report skipped when CLAUDECHROME_ENABLED=false."""
        assert _CONFIG_HOOK is not None
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "false"

        with tempfile.TemporaryDirectory() as tmpdir:
            env["CRAWL_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                _CONFIG_HOOK,
                TEST_URL,
                "test-config-disabled",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode == 10, f"Hook failed: {stderr}"
            assert stdout.strip() == "CLAUDECHROME_ENABLED=False"

    def test_snapshot_hook_skips_when_disabled(self):
        """Snapshot hook should skip when CLAUDECHROME_ENABLED=false."""
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "false"

        with tempfile.TemporaryDirectory() as tmpdir:
            env["SNAP_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                SNAPSHOT_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode == 0, f"Hook failed: {stderr}"
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "skipped"

    def test_snapshot_hook_fails_without_api_key(self):
        """Snapshot hook should fail when ANTHROPIC_API_KEY is not set."""
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "true"
        env.pop("ANTHROPIC_API_KEY", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            env["SNAP_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                SNAPSHOT_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode == 1
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "failed"
            assert "ANTHROPIC_API_KEY" in result["output_str"]

    def test_snapshot_hook_fails_without_chrome_session(self):
        """Snapshot hook should fail gracefully when no Chrome session exists."""
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "true"
        env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"

        with tempfile.TemporaryDirectory() as tmpdir:
            env["SNAP_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                SNAPSHOT_HOOK,
                TEST_URL,
                "test-no-chrome",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode != 0, "Should fail when no Chrome session exists"
            # Hook may crash before emitting JSONL (puppeteer not loaded) or
            # emit a failed ArchiveResult — either is acceptable
            result = parse_jsonl_output(stdout)
            if result is not None:
                assert result["status"] == "failed"
            else:
                err_lower = stderr.lower()
                assert any(
                    x in err_lower for x in ["chrome", "cdp", "puppeteer", "module"]
                ), f"Should mention chrome/CDP/puppeteer in error: {stderr}"


@pytest.mark.usefixtures("ensure_chrome_test_prereqs", "ensure_anthropic_api_key")
class TestClaudeChromeIntegration:
    """Integration tests requiring Chrome session and ANTHROPIC_API_KEY."""

    def test_full_pipeline_with_chrome_session(self, claudechrome_test_url):
        """Full pipeline: launch Chrome, run Claude computer-use, verify output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with chrome_session(
                Path(tmpdir),
                crawl_id="test-claudechrome",
                snapshot_id="snap-claudechrome",
                test_url=claudechrome_test_url,
                timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
                # Create claudechrome output directory (sibling to chrome)
                output_dir = snapshot_chrome_dir.parent / "claudechrome"
                output_dir.mkdir()

                # Configure claudechrome
                env["CLAUDECHROME_ENABLED"] = "true"
                env["CLAUDECHROME_MAX_ACTIONS"] = "3"
                env["CLAUDECHROME_TIMEOUT"] = "60"
                env["CLAUDECHROME_MODEL"] = "haiku"
                env["CLAUDECHROME_PROMPT"] = (
                    "Look at the page. If you see a 'Show More' button, click it. "
                    "Report what you did."
                )

                returncode, stdout, stderr = run_hook(
                    SNAPSHOT_HOOK,
                    claudechrome_test_url,
                    "snap-claudechrome",
                    cwd=str(output_dir),
                    env=env,
                    timeout=120,
                )

                result = parse_jsonl_output(stdout)
                assert result is not None, (
                    f"Expected JSONL output.\nStdout: {stdout}\nStderr: {stderr}"
                )
                assert result["status"] == "succeeded", (
                    f"Hook should succeed: {result}\nStderr: {stderr}"
                )
                assert returncode == 0, f"Hook failed (rc={returncode}): {stderr}"

                # Verify output files were created
                assert (output_dir / "conversation.json").exists(), (
                    f"conversation.json should exist. Files: {list(output_dir.iterdir())}"
                )
                assert (output_dir / "conversation.txt").exists(), (
                    "conversation.txt should exist"
                )
                assert (output_dir / "screenshot_initial.png").exists(), (
                    "screenshot_initial.png should exist"
                )
                assert (output_dir / "screenshot_final.png").exists(), (
                    "screenshot_final.png should exist"
                )

                # Verify conversation.json structure
                conversation_data = json.loads(
                    (output_dir / "conversation.json").read_text(),
                )
                assert conversation_data["url"] == claudechrome_test_url
                assert conversation_data["success"] is True
                assert "conversation" in conversation_data
                assert conversation_data["actionCount"] >= 0

                # Verify screenshots are valid PNGs (start with PNG magic bytes)
                initial_png = (output_dir / "screenshot_initial.png").read_bytes()
                assert initial_png[:4] == b"\x89PNG", (
                    "Initial screenshot should be valid PNG"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
