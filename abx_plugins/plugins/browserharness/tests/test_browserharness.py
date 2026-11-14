"""Real browserharness snapshot hooks without ArchiveBox."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from abx_plugins.plugins.chrome.tests.browser_hook_helpers import (
    assert_unconfigured_hook,
    read_config_macro,
    assert_challenge_capture,
    assert_script_capture,
)

PLUGIN = Path(__file__).parents[1]
HOOK = PLUGIN / "on_Snapshot__31_browserharness.py"


def test_unconfigured_hook(tmp_path):
    assert_unconfigured_hook(HOOK, tmp_path)


def test_script_capture(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'assert js("window.driverToken") == "page-owned"\njs("document.querySelector(\'#prepare\').click()")',
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
            "BROWSERHARNESS_PROMPT": "Click the Prepare page button, confirm the page title becomes Prepared, then finish.",
            "BROWSERHARNESS_MODEL": model,
            "BROWSERHARNESS_TIMEOUT": "120",
        },
    )


def test_script_failure_preserves_tab(tmp_path, httpserver, ensure_chrome_test_prereqs):
    assert_script_capture(
        HOOK,
        'raise RuntimeError("Requested failure")',
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
        'from pathlib import Path\nimport os\nPath("page.html").write_text("<!DOCTYPE html>" + js("document.documentElement.outerHTML"))',
        tmp_path,
        httpserver,
        expected_title="Unprepared",
        saved_file=True,
    )


@pytest.mark.parametrize(
    "domain, status, title",
    [
        ("example.com", "skipped", "Unprepared"),
        (None, "succeeded", "Prepared"),
    ],
)
def test_domain_filtered_script(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    domain,
    status,
    title,
):
    domain = domain or urlsplit(httpserver.url_for("/")).hostname
    script = read_config_macro(HOOK, "+ Domain guard").replace("example.com", domain)
    assert_script_capture(
        HOOK,
        script + "js(\"document.querySelector('#prepare').click()\")",
        tmp_path,
        httpserver,
        expected_status=status,
        expected_title=title,
        expected_output=f"Domain not selected: {urlsplit(httpserver.url_for('/')).hostname}"
        if status == "skipped"
        else None,
    )
