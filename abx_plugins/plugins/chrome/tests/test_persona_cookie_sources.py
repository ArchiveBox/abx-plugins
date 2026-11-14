"""Persona cookie artifacts are merged before importing into a real Chrome session."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("extension_export", [False, True])
def test_persona_imports_both_cookie_sources_and_reloads_changed_jar(
    tmp_path,
    extension_export,
    chrome_test_url,
):
    auth_file = tmp_path / "auth.json"
    cookies_file = tmp_path / "cookies.txt"
    expires = int(time.time()) + 3600
    cookies_file.write_text(
        "# Netscape HTTP Cookie File\n"
        f"login.example\tFALSE\t/\tTRUE\t{expires}\tshared\tjar-value\n"
        f"login.example\tFALSE\t/\tTRUE\t{expires}\tjar-only\tinitial\n",
    )
    cookie = {
        "domain": "login.example",
        "path": "/",
        "name": "shared",
        "value": "auth-value",
        "httpOnly": True,
        "secure": True,
        "sameSite": "strict" if extension_export else "Strict",
        "expirationDate" if extension_export else "expires": expires,
    }
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [cookie],
                **(
                    {"SOURCE": "archivebox-browser-extension"}
                    if extension_export
                    else {}
                ),
            },
        ),
    )
    with chrome_session(
        tmp_path,
        test_url=chrome_test_url,
        navigate=False,
        env_overrides={
            "AUTH_STORAGE_FILE": str(auth_file),
            "COOKIES_FILE": str(cookies_file),
        },
    ) as (_, _, chrome_dir, env):
        script = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const utils = require(process.argv[1]);
(async () => {
  const cdpUrl = fs.readFileSync(process.argv[2] + '/cdp_url.txt', 'utf8').trim();
  const browser = await utils.resolvePuppeteerModule().connect({browserWSEndpoint: cdpUrl});
  const config = JSON.parse(process.argv[3]);
  const assertImported = async value => {
    const cookies = await browser.cookies();
    assert.equal(cookies.find(c => c.name === 'jar-only')?.value, value, 'Netscape jar must import alongside auth.json');
    const auth = cookies.find(c => c.name === 'shared');
    assert.equal(auth.value, 'auth-value', 'auth.json wins duplicate cookie identities');
    assert.equal(auth.sameSite, 'Strict');
    assert.equal(auth.httpOnly, true);
    assert.equal(auth.secure, true);
    assert.equal(auth.expires, Number(process.argv[4]));
  };
  const reimport = async config => {
    const session = await utils.ensureChromeSession({
      ...utils.getChromeSessionOptionsFromConfig(config),
      outputDir: process.argv[5], hydrateReusedSession: false,
    });
    assert.equal(session.reusedExisting, true);
    assert.equal(session.cdpUrl, cdpUrl);
    assert.equal(session.stopPersonaHydration, null);
  };
  try {
    await assertImported('initial');
    await browser.setCookie({domain: 'unrelated.example', name: 'unrelated', value: 'keep', path: '/'});
    const initialHash = fs.readFileSync(process.argv[5] + '/persona_state.json', 'utf8');
    fs.writeFileSync(config.COOKIES_FILE, fs.readFileSync(config.COOKIES_FILE, 'utf8').replace('initial', 'updated'));
    await reimport(config);
    await assertImported('updated');
    assert.notEqual(fs.readFileSync(process.argv[5] + '/persona_state.json', 'utf8'), initialHash);
    assert.equal((await browser.cookies()).find(c => c.name === 'unrelated').value, 'keep');
    // Omission preserves browser state; a file-specific clear must not clear
    // the other artifact or unrelated browser cookies.
    await reimport({...config, COOKIES_FILE: '', AUTH_STORAGE_FILE: ''});
    await assertImported('updated');
    fs.writeFileSync(config.COOKIES_FILE, '');
    await reimport(config);
    assert.equal((await browser.cookies()).find(c => c.name === 'shared').value, 'auth-value');
    assert.equal((await browser.cookies()).find(c => c.name === 'unrelated').value, 'keep');
    for (const cookies of [null, {}, 'invalid']) {
      fs.writeFileSync(config.AUTH_STORAGE_FILE, JSON.stringify({cookies}));
      await assert.rejects(reimport(config), /cookies array/);
      assert.equal((await browser.cookies()).find(c => c.name === 'shared').value, 'auth-value');
    }
    for (const auth of [{}, {origins: []}, {SOURCE: 'archivebox-browser-extension', origins: []}]) {
      fs.writeFileSync(config.AUTH_STORAGE_FILE, JSON.stringify(auth));
      await reimport(config);
      assert.equal((await browser.cookies()).find(c => c.name === 'shared').value, 'auth-value');
      assert.equal((await browser.cookies()).find(c => c.name === 'unrelated').value, 'keep');
    }
  } finally { browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(CHROME_UTILS),
                str(chrome_dir),
                json.dumps(
                    {
                        "AUTH_STORAGE_FILE": str(auth_file),
                        "COOKIES_FILE": str(cookies_file),
                        "CHROME_USER_DATA_DIR": str(
                            tmp_path / "personas" / "Default" / "chrome_profile",
                        ),
                    },
                ),
                str(expires),
                str(Path(env["CRAWL_DIR"]) / "chrome"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
