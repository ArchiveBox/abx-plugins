"""Persona hydration must retain client hints in JS and on the wire."""

import json
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_persona_keeps_user_agent_client_hints(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<title>Native browser client hints</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    url = httpserver.url_for("/")
    native = None
    for mode in ("native", "legacy", "synced"):
        overrides = {
            "CHROME_USER_AGENT": "",
            "USER_AGENT": "Mozilla/5.0 (compatible; ArchiveBox/1.0)",
            "BROWSER_LANGUAGE": "",
            "BROWSER_PLATFORM": "",
        }
        if native:
            overrides.update(
                {
                    "CHROME_USER_AGENT": native["ua"],
                    "BROWSER_LANGUAGE": "en-US",
                    "BROWSER_PLATFORM": native["platform"],
                },
            )
            if mode == "synced":
                overrides["BROWSER_USER_AGENT_METADATA"] = json.dumps(native["hints"])
        with chrome_session(tmp_path / mode, test_url=url, env_overrides=overrides) as (
            _,
            _,
            chrome_dir,
            env,
        ):
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
        const identity = await page.evaluate(async () => ({
          ua: navigator.userAgent, platform: navigator.platform,
          hints: await navigator.userAgentData.getHighEntropyValues([
            "architecture", "bitness", "fullVersionList", "model", "platformVersion", "wow64", "formFactors",
          ]),
        }));
        console.log(JSON.stringify({...identity, headers:response.request().headers()}));
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
            if native:
                assert observed["hints"] == native["hints"], (mode, observed)
                assert observed["ua"] == native["ua"], (mode, observed)
            else:
                native = observed
