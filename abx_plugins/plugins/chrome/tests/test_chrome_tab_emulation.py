"""The snapshot tab's baseline emulation must outlive every hook's override."""

import json
import subprocess

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")
PLUGINS_DIR = CHROME_UTILS.parent.parent
SCREENSHOT_HOOK = PLUGINS_DIR / "screenshot" / "on_Snapshot__51_screenshot.js"
MOBILESIZE_HOOK = PLUGINS_DIR / "mobilesize" / "on_Snapshot__60_mobilesize.js"


def _tab_state(env: dict[str, str], chrome_dir) -> dict:
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            """
const {connectToPage} = require(process.argv[1]);
(async () => {
  const {browser, page} = await connectToPage({chromeSessionDir: process.argv[2]});
  try {
    console.log(JSON.stringify(await page.evaluate(() => ({
      width: innerWidth, height: innerHeight,
      visibility: document.visibilityState, focused: document.hasFocus(),
    }))));
  } finally { await browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
""",
            str(CHROME_UTILS),
            str(chrome_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_hook(hook, env: dict[str, str], url: str, cwd) -> dict:
    result = subprocess.run(
        [str(hook), f"--url={url}"],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    record = parse_jsonl_output(result.stdout)
    assert record is not None and record["status"] == "succeeded", (
        result.stdout,
        result.stderr,
    )
    return record


def test_snapshot_tab_keeps_persona_viewport_and_focus_after_hook_overrides(
    httpserver,
    tmp_path,
):
    """Hook viewport overrides end with the hook; the persona baseline stays.

    WHY: Chrome keeps one emulated size per tab, not one per CDP session. When
    a hook's session that set a viewport ends, Chrome reverts the whole tab to
    its unemulated window size (1440x1913 here), discarding the persona's
    CHROME_RESOLUTION override held by another live session. Hidden tabs
    defer that resize, so it surfaced once snapshot tabs were kept visible and
    focused (needed so hidden-tab media deferral cannot skip video), and later
    hooks captured the window size. The persona session owns the baseline.
    """
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><title>Viewport baseline</title><main>baseline</main>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("", status=404)
    url = httpserver.url_for("/")
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=45,
        env_overrides={"CHROME_RESOLUTION": "1440,2000"},
    ) as (_, _, chrome_dir, env):
        persona = {"width": 1440, "height": 2000}
        baseline = _tab_state(env, chrome_dir)
        assert baseline == {**persona, "visibility": "visible", "focused": True}

        screenshot_dir = tmp_path / "screenshot"
        screenshot_dir.mkdir()
        _run_hook(
            SCREENSHOT_HOOK,
            env | {"SCREENSHOT_RESOLUTION": "1280,800"},
            url,
            screenshot_dir,
        )
        assert _tab_state(env, chrome_dir) == baseline

        mobilesize_dir = tmp_path / "mobilesize"
        mobilesize_dir.mkdir()
        record = _run_hook(
            MOBILESIZE_HOOK,
            env
            | {
                "MOBILESIZE_WIDTH": "390",
                "MOBILESIZE_HEIGHT": "844",
                "MOBILESIZE_TIMEOUT": "16",
                "MOBILESIZE_WAIT": "1",
            },
            url,
            mobilesize_dir,
        )
        assert "390x844" in record["output_str"], record
        assert _tab_state(env, chrome_dir) == baseline
