"""The managed capture browser must use the committed stable Chrome release."""

import json
import subprocess

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
    get_test_env,
)


def test_managed_browser_is_stable_chrome(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
):
    env = get_test_env()
    version = subprocess.run(
        [env["CHROME_BINARY"], "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    # Official Chrome for Testing stable release, independently checked against
    # its published channel manifest when updating the dependency pin.
    assert "Google Chrome for Testing 155.0.8059.84" in version.stdout
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><title>Stable Chrome capture</title><h1>ArchiveBox</h1>",
        content_type="text/html",
    )
    with chrome_session(tmp_path, test_url=httpserver.url_for("/"), timeout=30) as (
        _,
        _,
        chrome,
        runtime,
    ):
        result = subprocess.run(
            [
                runtime["NODE_BINARY"],
                "-e",
                """
const utils = require(process.argv[1]);
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir: process.argv[2]});
  try {
    console.log(JSON.stringify({version: await browser.version(), title: await page.title()}));
  } finally { await browser.disconnect(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
                str(CHROME_UTILS),
                str(chrome),
            ],
            env=runtime,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "version": "Chrome/155.0.8059.84",
            "title": "Stable Chrome capture",
        }
