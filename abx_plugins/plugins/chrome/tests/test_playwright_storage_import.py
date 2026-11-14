import json
import subprocess

import pytest
from playwright.sync_api import sync_playwright

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("mixed_formats", [False, True])
def test_native_playwright_indexeddb_merge_preserves_existing_records(
    tmp_path,
    httpserver,
    mixed_formats,
):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><title>Storage</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    origin = httpserver.url_for("/").rstrip("/")
    with chrome_session(tmp_path, test_url=origin, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        endpoint = (chrome_dir / "cdp_url.txt").read_text().strip()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            page = context.new_page()
            page.goto(origin)
            page.evaluate("""async () => {
                localStorage.setItem('login', 'imported');
                const request = indexedDB.open('native-playwright', 1);
                request.onupgradeneeded = () => {
                    const store = request.result.createObjectStore('records', {keyPath: 'id'});
                    store.createIndex('by_name', 'name', {unique: true});
                };
                const db = await new Promise((resolve,reject) => {request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
                await new Promise((resolve,reject) => {
                    const tx = db.transaction('records','readwrite');
                    tx.objectStore('records').put({id:1, name:'incoming', date:new Date('2024-01-01'), bytes:new Uint8Array([1,2,3]), nested:{ok:true}});
                    tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error);
                });db.close();
            }""")
            auth = tmp_path / "playwright-state.json"
            context.storage_state(path=auth, indexed_db=True)
            state = json.loads(auth.read_text())
            assert state["origins"][0]["indexedDB"][0]["stores"][0]["records"][0][
                "valueEncoded"
            ]
            page.evaluate("""async () => {
                localStorage.setItem('keep', 'existing');
                const request=indexedDB.open('native-playwright');
                const db=await new Promise(resolve=>request.onsuccess=()=>resolve(request.result));
                await new Promise((resolve,reject)=>{const tx=db.transaction('records','readwrite');
                    tx.objectStore('records').put({id:1,name:'old'});
                    tx.objectStore('records').put({id:2,name:'keep'});
                    tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error);});db.close();
            }""")
            script = """
const assert=require('node:assert/strict');
const utils=require(process.argv[1]);
const storage=require(require('path').join(require('path').dirname(process.argv[1]),'persona_storage.js'));
(async()=>{const browser=await utils.resolvePuppeteerModule().connect({browserWSEndpoint:process.argv[2]});
try {
const {origins}=storage.readStorageState(process.argv[3]);
if(process.argv[4]==='true') {
 const page=(await browser.pages()).find(page=>page.url().startsWith(origins[0].origin));
 const previous=await storage.captureOriginStorage(page);
 origins[0].indexedDB.unshift(...previous.indexedDB);
}
assert.deepEqual(await storage.restoreOriginStorage(browser,origins,{merge:true}),[]);}
finally{browser.disconnect();}})().catch(error=>{console.error(error);process.exit(1);});
"""
            result = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    script,
                    str(CHROME_UTILS),
                    endpoint,
                    str(auth),
                    json.dumps(mixed_formats),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr
            actual = page.evaluate("""async () => {
                const request=indexedDB.open('native-playwright');
                const db=await new Promise(resolve=>request.onsuccess=()=>resolve(request.result));
                const records=await new Promise(resolve=>db.transaction('records').objectStore('records').getAll().onsuccess=e=>resolve(e.target.result));db.close();
                return {records:records.map(record=>({...record,date:record.date?.toISOString(),bytes:record.bytes?Array.from(record.bytes):undefined})),keep:localStorage.getItem('keep'),login:localStorage.getItem('login')};
            }""")
            assert actual["records"] == [
                {
                    "id": 1,
                    "name": "incoming",
                    "date": "2024-01-01T00:00:00.000Z",
                    "bytes": [1, 2, 3],
                    "nested": {"ok": True},
                },
                {"id": 2, "name": "keep", "date": None, "bytes": None},
            ]
            assert actual["keep"] == "existing"
            assert actual["login"] == "imported"
            # An incompatible pasted schema must not delete the existing DB.
            state["origins"][0]["indexedDB"][0]["version"] = 2
            auth.write_text(json.dumps(state))
            rejected = subprocess.run(
                result.args,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert rejected.returncode != 0
            assert "Playwright IndexedDB import failed" in rejected.stderr
            preserved = page.evaluate("""async () => {
                const request=indexedDB.open('native-playwright');
                const db=await new Promise(resolve=>request.onsuccess=()=>resolve(request.result));
                const records=await new Promise(resolve=>db.transaction('records').objectStore('records').getAllKeys().onsuccess=e=>resolve(e.target.result));
                const version=db.version;db.close();return {records,version};
            }""")
            assert preserved == {"records": [1, 2], "version": 1}
            page.close()
            browser.close()
