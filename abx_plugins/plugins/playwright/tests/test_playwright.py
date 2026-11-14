"""Real playwright snapshot hooks without ArchiveBox."""

from pathlib import Path

from abx_plugins.plugins.chrome.tests.browser_hook_helpers import (
    assert_unconfigured_hook,
    assert_challenge_capture,
    assert_script_capture,
)

PLUGIN = Path(__file__).parents[1]
HOOK = PLUGIN / "on_Snapshot__31_playwright.js"


def test_unconfigured_hook(tmp_path):
    assert_unconfigured_hook(HOOK, tmp_path)


def test_script_capture(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'if (await page.evaluate(() => window.driverToken) !== "page-owned") throw new Error("Wrong world");\nawait page.click("#prepare");',
        tmp_path,
        httpserver,
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


def test_three_challenges(tmp_path, ensure_chrome_test_prereqs):
    assert_challenge_capture(HOOK, tmp_path)


def test_saved_files(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'await (await import("node:fs/promises")).writeFile(outputDir + "/page.html", "<!DOCTYPE html>" + await page.content());',
        tmp_path,
        httpserver,
        expected_title="Unprepared",
        saved_file=True,
    )
