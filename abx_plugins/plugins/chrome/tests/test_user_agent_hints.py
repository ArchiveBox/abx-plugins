"""Persona hydration must retain client hints in JS and on the wire."""

import json
import subprocess
import sys
import time

import pytest
from werkzeug.wrappers import Response

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


def observe_identity(url, chrome_dir, env):
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
    return json.loads(result.stdout)


@pytest.fixture
def identity_url(httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<title>Native browser client hints</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    return httpserver.url_for("/")


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_persona_keeps_user_agent_client_hints(tmp_path, identity_url):
    url = identity_url
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
            observed = observe_identity(url, chrome_dir, env)
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
                # Headless Chromium must not announce itself in its UA.
                assert "HeadlessChrome" not in observed["ua"], observed
                assert "HeadlessChrome" not in observed["headers"]["user-agent"], (
                    observed
                )
                native = observed


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_persona_user_agent_from_another_os_gets_matching_hints(tmp_path, httpserver):
    # A persona UA synced from another OS without exported client hints, e.g.
    # a macOS browser profile replayed by a Linux server, must not ship this
    # host's OS in the navigated page's hints, headers, or navigator.platform.
    if sys.platform == "darwin":
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        platform, navigator_platform = "Windows", "Win32"
    else:
        user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        platform, navigator_platform = "macOS", "MacIntel"
    requests = []
    reports = []

    def page(request):
        requests.append(dict(request.headers))
        # Report what the page's own scripts see while loading, as bot checks
        # do; any later DevTools client attaching would change the answer.
        return Response(
            "<title>Persona page</title><script>"
            "const seen = {ua: navigator.userAgent, platform: navigator.platform};"
            "navigator.userAgentData.getHighEntropyValues(['fullVersionList', 'platformVersion'])"
            ".then(hints => fetch('/report', {method: 'POST', body: JSON.stringify({...seen, hints})}));"
            "</script>",
            content_type="text/html",
        )

    def report(request):
        reports.append(json.loads(request.get_data()))
        return Response("")

    httpserver.expect_request("/persona").respond_with_handler(page)
    httpserver.expect_request("/report", method="POST").respond_with_handler(report)
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    url = httpserver.url_for("/persona")
    overrides = {
        "CHROME_USER_AGENT": user_agent,
        "BROWSER_LANGUAGE": "en-US",
        "BROWSER_PLATFORM": "",
    }
    with chrome_session(tmp_path, test_url=url, env_overrides=overrides):
        deadline = time.monotonic() + 10
        while not reports and time.monotonic() < deadline:
            time.sleep(0.1)
    assert len(reports) == 1, reports
    observed = reports[0]
    major = next(
        entry["version"]
        for entry in observed["hints"]["fullVersionList"]
        if entry["brand"] == "Chromium"
    ).split(".")[0]
    assert observed["ua"] == user_agent.replace("Chrome/120.", f"Chrome/{major}."), (
        observed
    )
    assert observed["hints"]["platform"] == platform, observed
    assert observed["hints"]["platformVersion"] == "", observed
    assert observed["hints"]["mobile"] is False, observed
    assert observed["platform"] == navigator_platform, observed
    headers = requests[0]
    assert headers["User-Agent"] == observed["ua"], headers
    assert headers["Sec-Ch-Ua-Platform"] == f'"{platform}"', headers
    assert headers["Sec-Ch-Ua-Mobile"] == "?0", headers
