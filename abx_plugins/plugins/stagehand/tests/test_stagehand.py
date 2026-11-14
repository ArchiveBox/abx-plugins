"""Real stagehand snapshot hooks without ArchiveBox."""

import os
from pathlib import Path

from abx_plugins.plugins.chrome.tests.browser_hook_helpers import (
    assert_unconfigured_hook,
    assert_challenge_capture,
    assert_script_capture,
)

PLUGIN = Path(__file__).parents[1]
HOOK = PLUGIN / "on_Snapshot__31_stagehand.js"


def test_unconfigured_hook(tmp_path):
    assert_unconfigured_hook(HOOK, tmp_path)


def test_script_capture(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'if (await page.evaluate({expression: "window.driverToken"}) !== "page-owned") throw new Error("Wrong world");\nawait page.evaluate({expression: "document.querySelector(\'#prepare\').click()"});',
        tmp_path,
        httpserver,
    )


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


def test_script_failure_preserves_tab(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'throw new Error("Requested failure");',
        tmp_path,
        httpserver,
        expected_status="failed",
        expected_title="Unprepared",
    )


def test_ten_challenges(tmp_path, ensure_chrome_test_prereqs):
    assert_challenge_capture(HOOK, tmp_path)


def test_saved_files(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'await (await import("node:fs/promises")).writeFile(outputDir + "/page.html", "<!DOCTYPE html>" + await page.evaluate({expression: "document.documentElement.outerHTML"}));',
        tmp_path,
        httpserver,
        expected_title="Unprepared",
        saved_file=True,
    )
