"""Remove one exact host through Chrome's declared profile command."""

import json
from contextlib import ExitStack
import subprocess
import sys

import pytest
from pytest_httpserver import HTTPServer

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.fixture
def profile_site_url():
    # Multiple hostnames create parallel browser preconnects. A single-threaded
    # HTTP server blocks later hosts behind the first idle connection.
    with HTTPServer(host="127.0.0.1", threaded=True) as server:
        server.expect_request("/").respond_with_data(
            "<!doctype html><title>Profile storage</title>",
            content_type="text/html",
        )
        server.expect_request("/favicon.ico").respond_with_data("", status=404)
        yield server.url_for("/")
        server.check_assertions()


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("live", [True, False])
def test_remove_profile_domain_preserves_other_hosts(tmp_path, profile_site_url, live):
    selected = profile_site_url.replace("127.0.0.1", "localhost")
    storage_only = profile_site_url.replace("127.0.0.1", "storage-only.localhost")
    session_only = profile_site_url.replace("127.0.0.1", "session-only.localhost")
    with ExitStack() as sessions:
        _, _, chrome_dir, env = sessions.enter_context(
            chrome_session(tmp_path, test_url=profile_site_url, navigate=False),
        )
        setup = r"""
const fs = require('fs');
const utils = require(process.argv[1]);
(async () => {
 const browser = await utils.resolvePuppeteerModule().connect({browserWSEndpoint: fs.readFileSync(process.argv[2], 'utf8').trim()});
 try {
  for (const domain of ['localhost', '.localhost', 'sub.localhost', '127.0.0.1']) {
    await browser.setCookie({domain, name: domain === '.localhost' ? 'dot-login' : 'login', value: 'keep-or-remove', path: '/', secure: false, expires: Math.floor(Date.now()/1000) + 3600});
  }
  for (const url of process.argv.slice(3)) {
    const page = await browser.newPage();
    await page.goto(url);
    await page.evaluate(async () => {
      if (location.hostname === 'session-only.localhost') { sessionStorage.setItem('login', 'saved'); return; }
      localStorage.setItem('login', 'saved'); sessionStorage.setItem('login', 'saved');
      if (location.hostname === 'storage-only.localhost') return;
      await new Promise((resolve, reject) => {
        const request = indexedDB.open('login-db', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('tokens');
        request.onerror = () => reject(request.error);
        request.onsuccess = () => { request.result.close(); resolve(); };
      });
      const cache = await caches.open('login-cache');
      await cache.put('/saved', new Response('saved'));
    });
    // Native inventory must discover storage even after its tab closes.
    if (!['127.0.0.1', 'session-only.localhost'].includes(new URL(url).hostname)) await page.close();
  }
 } finally { browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                setup,
                str(CHROME_UTILS),
                str(chrome_dir / "cdp_url.txt"),
                selected,
                profile_site_url,
                storage_only,
                session_only,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        request = {
            "action": "remove-domain",
            "domain": "localhost",
            "user_data_dir": str(tmp_path / "personas" / "Default" / "chrome_profile"),
            "cdp_url": (chrome_dir / "cdp_url.txt").read_text().strip(),
        }
        command = json.loads((CHROME_UTILS.parent / "config.json").read_text())[
            "commands"
        ]["browser-profile"]
        if not live:
            sessions.close()
            request.pop("cdp_url")
        result = subprocess.run(
            [sys.executable, str(CHROME_UTILS.parent / command[0]), *command[1:]],
            input=json.dumps({**request, "action": "inspect-domains"}),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        inventory = {
            item["domain"]: item for item in json.loads(result.stdout)["domains"]
        }
        assert inventory["localhost"]["cookies"] == 2
        assert inventory["storage-only.localhost"]["cookies"] == 0
        assert (
            storage_only.rstrip("/") in inventory["storage-only.localhost"]["origins"]
        )
        assert inventory["storage-only.localhost"]["storage_bytes"] > 0
        if live:
            assert inventory["session-only.localhost"]["cookies"] == 0
            assert inventory["session-only.localhost"]["session_entries"] == 1
        assert "keep-or-remove" not in result.stdout
        result = subprocess.run(
            [sys.executable, str(CHROME_UTILS.parent / command[0]), *command[1:]],
            input=json.dumps(request),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        removed = json.loads(result.stdout)
        assert "localhost" not in {entry["domain"] for entry in removed["domains"]}
        assert removed["domain"] == "localhost"
        assert removed["cookies_removed"] == 2
        assert selected.rstrip("/") in removed["origins_removed"]
        verify = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const utils = require(process.argv[1]);
(async () => {
 const {browser, cleanup} = await require(process.argv[1].replace('chrome_utils.js', 'export_browser_state.js')).openBrowser({reuseProfileSession:true});
 try {
   const cookies = await browser.cookies();
   assert.equal(cookies.some(cookie => cookie.domain.replace(/^\./, '') === 'localhost'), false);
   assert.equal(cookies.find(cookie => cookie.domain === 'sub.localhost').value, 'keep-or-remove');
   assert.equal(cookies.find(cookie => cookie.domain === '127.0.0.1').value, 'keep-or-remove');
   for (const url of process.argv.slice(3)) {
     const page = await browser.newPage(); await page.goto(url);
     const state = await page.evaluate(async () => ({local: localStorage.getItem('login'), dbs: (await indexedDB.databases()).map(db => db.name), caches: await caches.keys()}));
     if (new URL(url).hostname === 'localhost') assert.deepEqual(state, {local:null, dbs:[], caches:[]});
     else if (new URL(url).hostname === 'storage-only.localhost') assert.deepEqual(state, {local:'saved', dbs:[], caches:[]});
     else assert.deepEqual(state, {local:'saved', dbs:['login-db'], caches:['login-cache']});
     await page.close();
   }
 } finally { await cleanup(); }
})().catch(error => { console.error(error); process.exit(1); });
"""
        verify_env = {
            **env,
            "CHROME_USER_DATA_DIR": request["user_data_dir"],
            "CHROME_CDP_URL": request.get("cdp_url", ""),
        }
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                verify,
                str(CHROME_UTILS),
                str(chrome_dir / "cdp_url.txt"),
                selected,
                profile_site_url,
                storage_only,
            ],
            env=verify_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("live", [True, False])
def test_import_profile_state_merges_cookies_and_storage(
    tmp_path,
    profile_site_url,
    live,
):
    auth = tmp_path / "pasted-auth.json"
    jar = tmp_path / "pasted-cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2145916800\tjar\tjar-value\n",
    )
    with ExitStack() as sessions:
        _, _, chrome_dir, env = sessions.enter_context(
            chrome_session(tmp_path, test_url=profile_site_url, navigate=False),
        )
        setup = r"""
const fs = require('fs');
const utils = require(process.argv[1]);
(async () => {
 const browser = await utils.resolvePuppeteerModule().connect({browserWSEndpoint: fs.readFileSync(process.argv[2], 'utf8').trim()});
 try {
   const page = await browser.newPage(); await page.goto(process.argv[3]);
   await browser.setCookie({domain:'127.0.0.1', name:'unrelated', value:'keep', path:'/', expires: Math.floor(Date.now()/1000)+3600});
   await page.evaluate(async () => {
     localStorage.setItem('login', 'saved'); sessionStorage.setItem('login', 'saved');
     const db = await new Promise((resolve, reject) => {
       const request = indexedDB.open('login-db', 1);
       request.onupgradeneeded = () => request.result.createObjectStore('tokens');
       request.onerror = () => reject(request.error); request.onsuccess = () => resolve(request.result);
     });
     await new Promise((resolve, reject) => {
       const tx = db.transaction('tokens', 'readwrite'); tx.objectStore('tokens').put('saved', 'login');
       tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
     }); db.close();
   });
   const state = await require(process.argv[1].replace('chrome_utils.js','persona_storage.js')).captureOriginStorage(page);
   fs.writeFileSync(process.argv[4], JSON.stringify({SOURCE:'archivebox-browser-extension', cookies:[{domain:'127.0.0.1',path:'/',name:'imported',value:'new',sameSite:'strict',expirationDate: Math.floor(Date.now()/1000)+3600}], origins:[state], tabs:[{url:page.url(),sessionStorage:state.sessionStorage}]}));
   await page.evaluate(async () => {
     localStorage.setItem('login', 'changed'); localStorage.setItem('unrelated', 'keep'); sessionStorage.setItem('login', 'changed');
     const db = await new Promise(resolve => { const request = indexedDB.open('login-db'); request.onsuccess = () => resolve(request.result); });
     await new Promise((resolve, reject) => {
       const tx = db.transaction('tokens', 'readwrite'); const store = tx.objectStore('tokens');
       store.put('changed', 'login'); store.put('keep', 'unrelated');
       tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
     }); db.close();
   });
 } finally { browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                setup,
                str(CHROME_UTILS),
                str(chrome_dir / "cdp_url.txt"),
                profile_site_url,
                str(auth),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        request = {
            "action": "import-state",
            "user_data_dir": str(tmp_path / "personas" / "Default" / "chrome_profile"),
            "cookies_file": str(jar),
            "auth_file": str(auth),
            "cdp_url": (chrome_dir / "cdp_url.txt").read_text().strip(),
        }
        if not live:
            sessions.close()
            request.pop("cdp_url")
        command = json.loads((CHROME_UTILS.parent / "config.json").read_text())[
            "commands"
        ]["browser-profile"]
        result = subprocess.run(
            [sys.executable, str(CHROME_UTILS.parent / command[0]), *command[1:]],
            input=json.dumps(request),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        imported = json.loads(result.stdout)
        assert imported["cookies_imported"] == 2
        assert imported["origins_imported"] == 1
        assert imported["session_tabs_imported"] == (1 if live else 0)
        assert (
            next(
                entry for entry in imported["domains"] if entry["domain"] == "127.0.0.1"
            )["cookies"]
            == 3
        )
        verify = r"""
const assert = require('node:assert/strict');
(async () => {
 const {browser, cleanup} = await require(process.argv[1].replace('chrome_utils.js','export_browser_state.js')).openBrowser({reuseProfileSession:true});
 try {
   const cookies = await browser.cookies();
   assert.equal(cookies.find(c => c.name === 'unrelated').value, 'keep');
   assert.equal(cookies.find(c => c.name === 'jar').value, 'jar-value');
   assert.equal(cookies.find(c => c.name === 'imported').sameSite, 'Strict');
   let page = (await browser.pages()).find(page => page.url() === process.argv[2]);
   if (!page) { page = await browser.newPage(); await page.goto(process.argv[2]); }
   const state = await page.evaluate(async () => {
     const db = await new Promise(resolve => { const request = indexedDB.open('login-db'); request.onsuccess = () => resolve(request.result); });
     const tokens = await new Promise((resolve, reject) => {
       const request = db.transaction('tokens').objectStore('tokens').getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
     }); db.close();
     return {login:localStorage.getItem('login'), unrelated:localStorage.getItem('unrelated'), session:sessionStorage.getItem('login'), tokens};
   });
   assert.equal(state.login, 'saved'); assert.equal(state.unrelated, 'keep');
   assert.deepEqual(state.tokens, ['saved', 'keep']);
   if (process.argv[3] === 'True') assert.equal(state.session, 'saved');
 } finally { await cleanup(); }
})().catch(error => { console.error(error); process.exit(1); });
"""
        verify_env = {
            **env,
            "CHROME_USER_DATA_DIR": request["user_data_dir"],
            "CHROME_CDP_URL": request.get("cdp_url", ""),
        }
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                verify,
                str(CHROME_UTILS),
                profile_site_url,
                str(live),
            ],
            env=verify_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_new_tabs_use_updated_persona_session_storage(tmp_path, profile_site_url):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": profile_site_url.rstrip("/"),
                        "sessionStorage": [{"name": "login", "value": "initial"}],
                    },
                ],
            },
        ),
    )
    with chrome_session(
        tmp_path,
        test_url=profile_site_url,
        navigate=False,
        env_overrides={"AUTH_STORAGE_FILE": str(auth)},
    ) as (_, _, chrome_dir, env):
        script = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const utils = require(process.argv[1]);
(async () => {
 const browser = await utils.resolvePuppeteerModule().connect({browserWSEndpoint: fs.readFileSync(process.argv[2], 'utf8').trim()});
 try {
   const existing = await browser.newPage(); await existing.goto(process.argv[3]);
   assert.equal(await existing.evaluate(() => sessionStorage.getItem('login')), 'initial');
   await existing.evaluate(() => sessionStorage.setItem('login', 'current-tab'));
   const save = value => fs.writeFileSync(process.argv[4], JSON.stringify({cookies:[], origins:value === null ? [] : [{origin:new URL(process.argv[3]).origin,sessionStorage:[{name:'login',value}]}]}));
   for (const value of ['updated', null, 'restored']) {
     save(value);
     const page = await browser.newPage(); await page.goto(process.argv[3]);
     assert.equal(await page.evaluate(() => sessionStorage.getItem('login')), value, 'New tabs must use current auth.json session state');
     await page.close();
     assert.equal(await existing.evaluate(() => sessionStorage.getItem('login')), 'current-tab');
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
                str(chrome_dir / "cdp_url.txt"),
                profile_site_url,
                str(auth),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("live", [True, False])
def test_import_profile_creates_native_playwright_database(
    tmp_path,
    profile_site_url,
    live,
):
    from playwright.sync_api import sync_playwright

    auth = tmp_path / "playwright-auth.json"
    with ExitStack() as sessions:
        _, _, chrome_dir, env = sessions.enter_context(
            chrome_session(tmp_path, test_url=profile_site_url, navigate=False),
        )
        playwright = sessions.enter_context(sync_playwright())
        browser = playwright.chromium.connect_over_cdp(
            (chrome_dir / "cdp_url.txt").read_text().strip(),
        )
        context = browser.contexts[0]
        page = context.new_page()
        page.goto(profile_site_url)
        page.evaluate("""async () => {
            const request = indexedDB.open('Notes', 1);
            request.onupgradeneeded = () => request.result.createObjectStore('entries', {keyPath:'id'});
            const db = await new Promise((resolve,reject) => {request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
            await new Promise((resolve,reject) => {const tx=db.transaction('entries','readwrite');tx.objectStore('entries').put({id:1,note:'saved'});tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error);});
            db.close();
        }""")
        auth.write_text(json.dumps(context.storage_state(indexed_db=True)))
        page.evaluate("""async () => {
            await new Promise((resolve,reject) => {const request=indexedDB.deleteDatabase('Notes');request.onsuccess=resolve;request.onerror=()=>reject(request.error);});
        }""")
        request = {
            "action": "import-state",
            "user_data_dir": str(tmp_path / "personas" / "Default" / "chrome_profile"),
            "auth_file": str(auth),
            "cdp_url": (chrome_dir / "cdp_url.txt").read_text().strip(),
        }
        if not live:
            sessions.close()
            request.pop("cdp_url")
        command = json.loads((CHROME_UTILS.parent / "config.json").read_text())[
            "commands"
        ]["browser-profile"]
        result = subprocess.run(
            [sys.executable, str(CHROME_UTILS.parent / command[0]), *command[1:]],
            input=json.dumps(request),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["origins_imported"] == 1
        verify = r"""
const assert = require('node:assert/strict');
(async () => {
 const {browser, cleanup} = await require(process.argv[1].replace('chrome_utils.js','export_browser_state.js')).openBrowser({reuseProfileSession:true});
 try {
   const page = await browser.newPage(); await page.goto(process.argv[2]);
   const state = await page.evaluate(async () => {
     const db = await new Promise((resolve,reject) => { const request=indexedDB.open('Notes');request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error); });
     const records = await new Promise((resolve,reject) => {const request=db.transaction('entries').objectStore('entries').getAll();request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
     db.close(); return records;
   });
   assert.deepEqual(state, [{id:1,note:'saved'}]); await page.close();
 } finally { await cleanup(); }
})().catch(error => {console.error(error);process.exit(1);});
"""
        verify_env = {
            **env,
            "CHROME_USER_DATA_DIR": request["user_data_dir"],
            "CHROME_CDP_URL": request.get("cdp_url", ""),
        }
        result = subprocess.run(
            [env["NODE_BINARY"], "-e", verify, str(CHROME_UTILS), profile_site_url],
            env=verify_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
