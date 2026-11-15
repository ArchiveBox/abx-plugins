"""The docs workflow refreshes Figma without changing other provider sessions."""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("base_extension", [False, True])
@pytest.mark.parametrize("refresh_extension", [False, True])
def test_docs_persona_merge_imports_both_formats(
    tmp_path,
    chrome_test_url,
    base_extension,
    refresh_extension,
):
    expires = int(time.time()) + 3600

    def cookie(domain, name, value, extension, same_site="Strict", persistent=True):
        item = {
            "domain": domain,
            "path": "/",
            "name": name,
            "value": value,
            "secure": True,
            "httpOnly": True,
            "sameSite": (
                {"Strict": "strict", "Lax": "lax", "None": "no_restriction"}[same_site]
                if extension
                else same_site
            ),
        }
        if persistent:
            item["expirationDate" if extension else "expires"] = expires
        return item

    def state(extension, cookies, origins, tabs):
        return {
            **({"SOURCE": "archivebox-browser-extension"} if extension else {}),
            "cookies": cookies,
            "origins": origins,
            "tabs": tabs,
        }

    unrelated_origin = {
        "origin": chrome_test_url.rstrip("/"),
        "localStorage": [{"name": "provider", "value": "keep"}],
    }
    unrelated_tab = {
        "url": chrome_test_url,
        "sessionStorage": [{"name": "tab", "value": "keep"}],
    }
    old_origin = {
        "origin": "https://www.figma.com",
        "sessionStorage": [{"name": "session", "value": "old"}],
    }
    new_origin = {
        "origin": "https://www.figma.com",
        "sessionStorage": [{"name": "session", "value": "new"}],
    }
    old_tab = {"url": "https://www.figma.com/old", "sessionStorage": []}
    new_tab = {"url": "https://www.figma.com/new", "sessionStorage": []}
    unrelated_cookie = cookie("login.example", "unrelated", "keep", base_extension)
    base = state(
        base_extension,
        [unrelated_cookie, cookie(".figma.com", "stale", "old", base_extension)],
        [unrelated_origin, old_origin],
        [unrelated_tab, old_tab],
    )
    refresh = state(
        refresh_extension,
        [
            cookie(".figma.com", "fresh", "new", refresh_extension),
            cookie("www.figma.com", "lax", "new", refresh_extension, "Lax"),
            cookie(
                "www.figma.com",
                "session",
                "new",
                refresh_extension,
                "None",
                persistent=False,
            ),
            cookie("login.example", "unrelated", "overwrite", refresh_extension),
            cookie("figma.com.attacker.example", "excluded", "no", refresh_extension),
        ],
        [
            new_origin,
            {"origin": "https://figma.com.attacker.example", "sessionStorage": []},
        ],
        [new_tab, {"url": "https://figma.com.attacker.example/", "sessionStorage": []}],
    )
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps(base))
    workflow = Path(__file__).resolve().parents[4] / ".github/workflows/docs.yml"
    script = textwrap.dedent(
        workflow.read_text()
        .split("<<'PYAUTH'\n", 1)[1]
        .split("          PYAUTH", 1)[0],
    )
    merged = subprocess.run(
        [sys.executable, "-", str(auth_file)],
        input=script,
        env={**os.environ, "FIGMA_AUTH_STORAGE_JSON": json.dumps(refresh)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert merged.returncode == 0, merged.stderr
    auth = json.loads(auth_file.read_text())
    assert auth.get("SOURCE") != "archivebox-browser-extension"
    assert auth["origins"] == [unrelated_origin, new_origin]
    assert auth["tabs"] == [unrelated_tab, new_tab]
    assert {item["name"] for item in auth["cookies"]} == {
        "unrelated",
        "fresh",
        "lax",
        "session",
    }
    with chrome_session(
        tmp_path,
        test_url=chrome_test_url,
        navigate=True,
        env_overrides={"AUTH_STORAGE_FILE": str(auth_file)},
    ) as (_, _, chrome_dir, env):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const utils = require(process.argv[1]);
(async () => {
  const browser = await utils.resolvePuppeteerModule().connect({browserWSEndpoint: fs.readFileSync(process.argv[2] + '/cdp_url.txt', 'utf8').trim()});
  try {
    const cookies = await browser.cookies();
    assert.deepEqual(cookies.map(cookie => cookie.name).sort(), ['fresh', 'lax', 'session', 'unrelated']);
    for (const name of ['fresh', 'lax', 'session', 'unrelated']) {
      const cookie = cookies.find(cookie => cookie.name === name);
      assert.equal(cookie.value, name === 'unrelated' ? 'keep' : 'new');
      assert.equal(cookie.httpOnly, true);
      assert.equal(cookie.secure, true);
      assert.equal(cookie.sameSite, name === 'lax' ? 'Lax' : name === 'session' ? 'None' : 'Strict');
      assert.equal(cookie.expires, name === 'session' ? -1 : Number(process.argv[3]));
      assert.equal(cookie.session, name === 'session');
    }
    const page = (await browser.pages()).find(page => page.url() === process.argv[4]);
    assert.ok(page, 'real local provider page was opened');
    assert.equal(await page.evaluate(() => localStorage.getItem('provider')), 'keep');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('tab')), 'keep');
  } finally { browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                str(expires),
                chrome_test_url,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

    marker = {"SOURCE": "archivebox-browser-extension"} if refresh_extension else {}
    for update, expected in (
        (marker, auth),
        (
            {**marker, "cookies": []},
            {
                **auth,
                "cookies": [
                    item for item in auth["cookies"] if item["name"] == "unrelated"
                ],
            },
        ),
        (
            {**marker, "origins": [], "tabs": []},
            {**auth, "origins": [unrelated_origin], "tabs": [unrelated_tab]},
        ),
    ):
        auth_file.write_text(json.dumps(auth))
        merged = subprocess.run(
            [sys.executable, "-", str(auth_file)],
            input=script,
            env={**os.environ, "FIGMA_AUTH_STORAGE_JSON": json.dumps(update)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert merged.returncode == 0, merged.stderr
        assert json.loads(auth_file.read_text()) == expected
