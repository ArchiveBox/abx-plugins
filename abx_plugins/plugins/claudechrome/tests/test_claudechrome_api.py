# ci-environment: provider-api
"""Real Claude Chrome capture using the Anthropic API."""

import json
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    get_hook_script,
    get_plugin_dir,
    parse_jsonl_output,
    run_hook,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGIN_DIR = get_plugin_dir(__file__)
_SNAPSHOT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudechrome*")
if _SNAPSHOT_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
SNAPSHOT_HOOK = _SNAPSHOT_HOOK
CHROME_STARTUP_TIMEOUT_SECONDS = 45


@pytest.fixture
def claudechrome_test_url(chrome_test_urls):
    return chrome_test_urls["claudechrome_url"]


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
