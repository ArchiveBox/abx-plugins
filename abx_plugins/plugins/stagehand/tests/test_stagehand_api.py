# ci-environment: provider-api
"""Real stagehand capture using billed model APIs."""

import os
from pathlib import Path

from abx_plugins.plugins.chrome.tests.browser_hook_helpers import (
    assert_challenge_capture,
    assert_script_capture,
)

HOOK = Path(__file__).parents[1] / "on_Snapshot__31_stagehand.js"


def test_prompt_capture(tmp_path, httpserver, ensure_chrome_test_prereqs):
    if os.environ.get("OPENAI_API_KEY"):
        model = "openai/gpt-6.1-sol"
    else:
        assert os.environ.get("ANTHROPIC_API_KEY"), (
            "Set OPENAI_API_KEY or ANTHROPIC_API_KEY for the real agent test"
        )
        model = "anthropic/claude-haiku-4-5-20251001"
    assert_script_capture(
        HOOK,
        "",
        tmp_path,
        httpserver,
        {
            "STAGEHAND_PROMPT": "Click the Prepare page button, confirm the page title becomes Prepared, then finish.",
            "STAGEHAND_MODEL": model,
            "STAGEHAND_TIMEOUT": "120",
        },
    )


def test_ten_challenges(tmp_path, ensure_chrome_test_prereqs):
    assert_challenge_capture(HOOK, tmp_path)
