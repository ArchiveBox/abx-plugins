"""Default persona hydration must retain the real browser's client hints."""

import json
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_default_persona_keeps_native_user_agent_client_hints(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<title>Native browser client hints</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    url = httpserver.url_for("/")
    with chrome_session(
        tmp_path,
        test_url=url,
        env_overrides={
            "CHROME_USER_AGENT": "",
            "USER_AGENT": "Mozilla/5.0 (compatible; ArchiveBox/1.0)",
            "BROWSER_LANGUAGE": "",
            "BROWSER_PLATFORM": "",
        },
    ) as (_, _, chrome_dir, env):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                r"""
const fs = require('fs');
const utils = require(process.argv[1]);
(async () => {
  const browser = await utils.connectToBrowserEndpoint(utils.resolvePuppeteerModule(),
    fs.readFileSync(process.argv[2] + '/cdp_url.txt', 'utf8').trim(), {defaultViewport:null});
  const page = await browser.newPage();
  try {
    const response = await page.goto(process.argv[3]);
    const hints = await page.evaluate(() => navigator.userAgentData.toJSON());
    console.log(JSON.stringify({hints, headers:response.request().headers()}));
  } finally {
    await page.close();
    await browser.disconnect();
  }
})().catch(error => { console.error(error); process.exit(1); });
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                url,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert observed["hints"]["brands"], observed
        assert observed["hints"]["platform"], observed
        assert observed["headers"]["sec-ch-ua"], observed
        assert (
            observed["headers"]["sec-ch-ua-platform"].strip('"')
            == observed["hints"]["platform"]
        )
