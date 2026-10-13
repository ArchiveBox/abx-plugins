"""Persona state must be present before scripts run in any CDP client's tabs."""

import json
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("storage_source", ["origin", "tab"])
def test_persona_settings_and_storage_on_external_cdp_tabs(
    tmp_path,
    httpserver,
    storage_source,
):
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
                        "indexedDB": {},
                        "localStorage": [{"name": "login", "value": "local-value"}],
                        **(
                            {
                                "sessionStorage": [
                                    {"name": "tab", "value": "session-value"},
                                ],
                            }
                            if storage_source == "origin"
                            else {}
                        ),
                    },
                ],
                "tabs": [
                    {
                        "url": origin + "/previous/page?before=restart",
                        "sessionStorage": [{"name": "tab", "value": "session-value"}],
                    },
                ]
                if storage_source == "tab"
                else [],
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
@pytest.mark.parametrize(
    ("existing_schema", "account_keypath"),
    [(False, "type"), (False, ["type"]), (True, ["type"])],
)
def test_indexeddb_and_tab_storage_roundtrip(
    tmp_path,
    httpserver,
    existing_schema,
    account_keypath,
):
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
    await page.evaluate(async accountKeyPath => {
      localStorage.setItem('token','local-token'); sessionStorage.setItem('token','tab-token');
      await new Promise((resolve,reject) => {
        const request = indexedDB.open('login-state', 3);
        request.onupgradeneeded = () => {
          const store = request.result.createObjectStore('sessions', {keyPath:'id', autoIncrement:true});
          store.createIndex('byAccount','account',{unique:true});
          request.result.createObjectStore('accountData', {keyPath:accountKeyPath});
        };
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result; const tx = db.transaction(['sessions','accountData'],'readwrite');
          tx.objectStore('accountData').put({type:'settings',theme:'dark'});
          tx.objectStore('sessions').put({id:7,account:'alice',token:'idb-token',
            created:new Date('2026-01-01T00:00:00Z'),bytes:new Uint8Array([0,127,255]),blob:new Blob(['saved-session'])});
          tx.oncomplete = () => {db.close();resolve();}; tx.onerror = () => reject(tx.error);
        };
      });
    }, JSON.parse(process.argv[4]));
    const state = await captureOriginStorage(page);
    fs.writeFileSync(process.argv[3],JSON.stringify({cookies:[],origins:[{origin:state.origin,
      localStorage:state.localStorage,indexedDB:state.indexedDB}],tabs:[{url:state.url,sessionStorage:state.sessionStorage}]}));
  } finally {await browser.disconnect();}
})().catch(e=>{console.error(e);process.exit(1)});
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                str(auth),
                json.dumps(account_keypath),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
    # A synced site database that IndexedDB rejects must only skip that
    # database; it must never abort Chrome launch or the other databases.
    saved = json.loads(auth.read_text())
    rejected = dict(saved["origins"][0]["indexedDB"][0], name="rejected-db")
    saved["origins"][0]["indexedDB"][0:0] = [None, {"name": "malformed-db"}, rejected]
    auth.write_text(json.dumps(saved))
    profile = tmp_path / "destination-profile"
    if existing_schema:
        with chrome_session(
            tmp_path / "previous-crawl",
            test_url=url,
            env_overrides={"PERSONAS_DIR": str(profile)},
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
    await page.evaluate(() => new Promise((resolve,reject) => {
      const request = indexedDB.open('login-state', 3);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore('sessions', {keyPath:'id', autoIncrement:true});
        store.createIndex('byAccount','account',{unique:true});
        request.result.createObjectStore('accountData', {keyPath:'type'});
      };
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result; const tx = db.transaction('sessions','readwrite');
        tx.objectStore('sessions').put({id:99,account:'alice',token:'previous-token'});
        tx.oncomplete = () => {db.close();resolve();}; tx.onerror = () => reject(tx.error);
      };
    }));
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
    for attempt, malformed in enumerate((False, isinstance(account_keypath, str))):
        if malformed:
            # Corrupt an incoming key, then run a new crawl with the same profile.
            # Its failed import must roll back the clear and preserve good rows.
            payload = json.loads(saved["origins"][0]["indexedDB"][-1]["data"])
            account_data = next(
                table
                for table in payload["data"]["data"]
                if table["tableName"] == "accountData"
            )
            del account_data["rows"][0]["type"]
            saved["origins"][0]["indexedDB"][-1]["data"] = json.dumps(payload)
            auth.write_text(json.dumps(saved))
        with chrome_session(
            tmp_path / f"restore-{attempt}",
            test_url=url,
            env_overrides={
                "AUTH_STORAGE_FILE": str(auth),
                "PERSONAS_DIR": str(profile),
            },
        ) as (chrome_process, _, chrome_dir, env):
            diagnostics = chrome_process._stderr_log.read_text()
            assert "Skipped saved IndexedDB" in diagnostics
            assert (
                "malformed-db: Error IndexedDB state must contain Dexie JSON exports"
                in diagnostics
            )
            assert (
                "<unnamed>: Error IndexedDB state must contain Dexie JSON exports"
                in diagnostics
            )
            if malformed:
                assert "DataError" in diagnostics
            else:
                assert "login-state:" not in diagnostics
            result = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    r"""
    const utils = require(process.argv[1]);
    (async () => {
      const {browser,page} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
      try {
        console.log(JSON.stringify(await page.evaluate(async accountKeyPath => {
          const db = await new Promise((resolve,reject) => {const r=indexedDB.open('login-state');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
          const store = db.transaction('sessions').objectStore('sessions');
          const row = await new Promise((resolve,reject) => {const r=store.get(7);r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
          const unique=store.index('byAccount').unique;
          const result = {version:db.version,keyPath:store.keyPath,autoIncrement:store.autoIncrement,
            accountKeyPath:db.transaction('accountData').objectStore('accountData').keyPath,
            accountData:await new Promise((resolve,reject)=>{const r=db.transaction('accountData').objectStore('accountData').get(Array.isArray(accountKeyPath) ? ['settings'] : 'settings');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);}),
            unique,account:row.account,token:row.token,date:row.created.toISOString(),
            bytes:Array.from(row.bytes),blob:await row.blob.text(),local:localStorage.getItem('token'),session:sessionStorage.getItem('token'),
            databases:(await indexedDB.databases()).map(d=>d.name).sort()};
          db.close();return result;
        }, JSON.parse(process.argv[3]))));
      } finally {await browser.disconnect();}
    })().catch(e=>{console.error(e);process.exit(1)});
    """,
                    str(CHROME_UTILS),
                    str(chrome_dir),
                    json.dumps(account_keypath),
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
                "accountKeyPath": account_keypath,
                "accountData": {"type": "settings", "theme": "dark"},
                "account": "alice",
                "token": "idb-token",
                "date": "2026-01-01T00:00:00.000Z",
                "bytes": [0, 127, 255],
                "blob": "saved-session",
                "local": "local-token",
                "session": "tab-token",
                "databases": ["login-state"],
            }


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_concurrent_new_tabs_survive_session_storage_hydration(tmp_path, httpserver):
    # Snapshot tabs are opened by other CDP clients while the crawl browser's
    # persona hydration attaches to them. A tab navigating while it is being
    # hydrated must neither fail hydration nor be closed, and must keep its state.
    httpserver.expect_request("/").respond_with_data(
        # The page replaces itself once on load, so hydration's evaluate in each
        # tab regularly overlaps a same-origin navigation, as it does in production.
        "<title>Persona tab</title><script>if (!location.search) location.replace('/?settled')</script>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    url = httpserver.url_for("/")
    origin = url.rstrip("/")
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": origin,
                        # Real synced personas carry megabytes of tab state, which
                        # keeps each tab's seeding evaluate in flight long enough
                        # to overlap the page replacing itself.
                        "sessionStorage": [
                            {"name": "token", "value": "tab-token"},
                            *(
                                {"name": f"cache-{i}", "value": "x" * 4096}
                                for i in range(1000)
                            ),
                        ],
                    },
                ],
                "tabs": [],
            },
        ),
    )
    with chrome_session(
        tmp_path,
        test_url=url,
        env_overrides={"AUTH_STORAGE_FILE": str(auth)},
    ) as (_, _, chrome_dir, env):
        navigation = json.loads((chrome_dir / "navigation.json").read_text())
        assert navigation["finalUrl"] == url + "?settled"
        assert navigation["status"] == 200
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                r"""
const fs = require('fs');
const path = require('path');
const utils = require(process.argv[1]);
(async () => {
  // Open tabs like the snapshot tab hook (Target.createTarget over a
  // short-lived connection), already pointed at the page so each tab's first
  // navigation starts as soon as any attached client resumes it, as the
  // extensions and observers attached to production tabs do.
  const cdpUrl = fs.readFileSync(path.join(process.argv[2], 'cdp_url.txt'), 'utf8').trim();
  const targetIds = [];
  for (let i = 0; i < 16; i++) targetIds.push((await utils.openTabInChromeSession({cdpUrl, puppeteer: utils.resolvePuppeteerModule(), initialUrl: process.argv[3]})).targetId);
  const {browser} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
  try {
    const targets = browser.targets().filter(target => targetIds.includes(target._targetId));
    const pages = await Promise.all(targets.map(target => target.page()));
    await Promise.all(pages.map(page => page.waitForFunction(() => location.search === '?settled' && document.readyState === 'complete')));
    const tokens = await Promise.all(pages.map(page => page.evaluate(() => sessionStorage.getItem('token'))));
    console.log(JSON.stringify({open: pages.length, tokens}));
  } finally {await browser.disconnect();}
})().catch(e=>{console.error(e);process.exit(1)});
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                url,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"open": 16, "tokens": ["tab-token"] * 16}
