"""Persona state must be present before scripts run in any CDP client's tabs."""

import json
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_persona_settings_and_storage_on_external_cdp_tabs(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<script>window.atLoad = {language:navigator.language, "
        "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone, "
        "width:innerWidth, scale:devicePixelRatio, "
        "theme:matchMedia('(prefers-color-scheme: dark)').matches, "
        "local:localStorage.getItem('login'), session:sessionStorage.getItem('tab')};</script>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    origin = httpserver.url_for("/").rstrip("/")
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "login",
                        "value": "cookie-value",
                        "url": origin,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    },
                ],
                "origins": [
                    {
                        "origin": origin,
                        "localStorage": [{"name": "login", "value": "local-value"}],
                        "sessionStorage": [{"name": "tab", "value": "session-value"}],
                    },
                ],
            },
        ),
    )
    with chrome_session(
        tmp_path,
        test_url=origin,
        navigate=False,
        env_overrides={
            "AUTH_STORAGE_FILE": str(auth),
            "BROWSER_LANGUAGE": "fr-FR",
            "BROWSER_TIMEZONE": "Europe/Paris",
            "CHROME_RESOLUTION": "1100,750",
            "BROWSER_DEVICE_SCALE_FACTOR": "2",
            "BROWSER_COLOR_SCHEME": "dark",
            "BROWSER_GEOLOCATION": json.dumps(
                {"latitude": 48.85, "longitude": 2.35, "accuracy": 10},
            ),
        },
    ) as (_, _, chrome_dir, env):
        script = r"""
const fs = require('fs');
const utils = require(process.argv[1]);
(async () => {
  const browser = await utils.connectToBrowserEndpoint(utils.resolvePuppeteerModule(),
    fs.readFileSync(process.argv[2] + '/cdp_url.txt', 'utf8').trim(), {defaultViewport:null});
  try {
    const page = await browser.newPage();
    await page.goto(process.argv[3]);
    const initial = await page.evaluate(() => window.atLoad);
    await page.evaluate(() => { localStorage.setItem('login', 'changed'); sessionStorage.removeItem('tab'); });
    await page.reload();
    const afterReload = await page.evaluate(() => window.atLoad);
    const cookies = await browser.cookies();
    console.log(JSON.stringify({initial, afterReload, cookies}));
    await page.close();
  } finally { await browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(CHROME_UTILS),
                str(chrome_dir),
                origin,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert observed["initial"] == {
            "language": "fr-FR",
            "timezone": "Europe/Paris",
            "width": 1100,
            "scale": 2,
            "theme": True,
            "local": "local-value",
            "session": "session-value",
        }
        assert observed["afterReload"] == {
            **observed["initial"],
            "local": "changed",
            "session": None,
        }
        cookie = next(
            cookie for cookie in observed["cookies"] if cookie["name"] == "login"
        )
        assert cookie["value"] == "cookie-value"
        assert cookie["httpOnly"] is True
        assert cookie["sameSite"] == "Lax"
        assert cookie["session"] is True


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_indexeddb_and_tab_storage_roundtrip(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<title>Persona database</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    url = httpserver.url_for("/")
    auth = tmp_path / "portable-auth.json"
    with chrome_session(tmp_path / "source", test_url=url) as (_, _, chrome_dir, env):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                r"""
const fs = require('fs');
const utils = require(process.argv[1]);
const {captureOriginStorage} = require(require('path').join(require('path').dirname(process.argv[1]), 'persona_storage.js'));
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
  try {
    await page.evaluate(async () => {
      localStorage.setItem('token','local-token'); sessionStorage.setItem('token','tab-token');
      await new Promise((resolve,reject) => {
        const request = indexedDB.open('login-state', 3);
        request.onupgradeneeded = () => {
          const store = request.result.createObjectStore('sessions', {keyPath:'id', autoIncrement:true});
          store.createIndex('byAccount','account',{unique:true});
        };
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result; const tx = db.transaction('sessions','readwrite');
          tx.objectStore('sessions').put({id:7,account:'alice',token:'idb-token',
            created:new Date('2026-01-01T00:00:00Z'),bytes:new Uint8Array([0,127,255]),blob:new Blob(['saved-session'])});
          tx.oncomplete = () => {db.close();resolve();}; tx.onerror = () => reject(tx.error);
        };
      });
    });
    const state = await captureOriginStorage(page);
    fs.writeFileSync(process.argv[3],JSON.stringify({cookies:[],origins:[{origin:state.origin,
      localStorage:state.localStorage,indexedDB:state.indexedDB}],tabs:[{url:state.url,sessionStorage:state.sessionStorage}]}));
  } finally {await browser.disconnect();}
})().catch(e=>{console.error(e);process.exit(1)});
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                str(auth),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
    with chrome_session(
        tmp_path / "destination",
        test_url=url,
        env_overrides={"AUTH_STORAGE_FILE": str(auth)},
    ) as (_, _, chrome_dir, env):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                r"""
const utils = require(process.argv[1]);
(async () => {
  const {browser,page} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
  try {
    console.log(JSON.stringify(await page.evaluate(async () => {
      const db = await new Promise((resolve,reject) => {const r=indexedDB.open('login-state');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
      const store = db.transaction('sessions').objectStore('sessions');
      const row = await new Promise((resolve,reject) => {const r=store.get(7);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
      const result = {version:db.version,keyPath:store.keyPath,autoIncrement:store.autoIncrement,
        unique:store.index('byAccount').unique,account:row.account,token:row.token,date:row.created.toISOString(),
        bytes:Array.from(row.bytes),blob:await row.blob.text(),local:localStorage.getItem('token'),session:sessionStorage.getItem('token')};
      db.close();return result;
    })));
  } finally {await browser.disconnect();}
})().catch(e=>{console.error(e);process.exit(1)});
""",
                str(CHROME_UTILS),
                str(chrome_dir),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "version": 3,
            "keyPath": "id",
            "autoIncrement": True,
            "unique": True,
            "account": "alice",
            "token": "idb-token",
            "date": "2026-01-01T00:00:00.000Z",
            "bytes": [0, 127, 255],
            "blob": "saved-session",
            "local": "local-token",
            "session": "tab-token",
        }
