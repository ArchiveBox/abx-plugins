"""Real Chrome integration coverage for TLSNotary request-header capture."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CHROME_UTILS = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"
PREPARE_HOOK = PLUGIN_DIR / "on_CrawlSetup__80_tlsnotary_prepare.py"
SNAPSHOT_HOOK = PLUGIN_DIR / "on_Snapshot__92_tlsnotary.js"
CHECK_CAPTURE = PLUGIN_DIR / "tests" / "check_capture.mjs"
TARGET_URL = "https://docs.sweeting.me/s/cookie-dilemma"
TRUSTED_PUBLIC_KEY = "MCowBQYDK2VwAyEA0H35h4fS0zKwPykdHg5ST/w/Byeek4VGQBSsmKBsr+E="
pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def _install_and_prepare(env: dict[str, str], _chrome_dir: Path) -> None:
    install_required_binary_from_config(PLUGIN_DIR, "tlsnotary", env=env)
    prepared = subprocess.run(
        [str(PREPARE_HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr

    extension = json.loads(
        (Path(env["CRAWL_DIR"]) / "tlsnotary" / "extension.json").read_text(),
    )
    script = r"""
const chrome = require(process.argv[1]);
const fs = require('node:fs');
const extension = JSON.parse(process.argv[3]);
(async () => {
  const puppeteer = chrome.resolvePuppeteerModule();
  const browser = await chrome.connectToBrowserEndpoint(puppeteer, process.argv[2], {defaultViewport: null});
  try {
    await chrome.loadUnpackedExtensionsIntoBrowser(browser, [extension], 30000);
    fs.writeFileSync(process.argv[4], JSON.stringify(extension));
  } finally {
    await browser.disconnect();
  }
})().catch(error => { console.error(error); process.exit(1); });
"""
    loaded = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            (Path(env["CRAWL_DIR"]) / "chrome" / "cdp_url.txt").read_text().strip(),
            json.dumps(extension),
            str(Path(env["CRAWL_DIR"]) / "tlsnotary" / "loaded-extension.json"),
        ],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr


def _inspect_browser(env: dict[str, str], cdp_url: str, extension_id: str) -> dict:
    script = r"""
const chrome = require(process.argv[1]);
(async () => {
  const browser = await chrome.connectToBrowserEndpoint(
    chrome.resolvePuppeteerModule(), process.argv[2], {defaultViewport: null},
  );
  try {
    const extensions = chrome.getExtensionTargets(browser)
      .filter(target => target.extensionId === process.argv[3]);
    const pageUrls = (await browser.pages()).map(page => page.url());
    process.stdout.write(JSON.stringify({extensionTargets: extensions.length, pageUrls}));
  } finally {
    await browser.disconnect();
  }
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, str(CHROME_UTILS), cdp_url, extension_id],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_extension_reports_real_main_request_headers_before_proof(tmp_path):
    """Exercise the pinned extension's user-facing API and actual managed window."""

    with chrome_session(
        tmp_path,
        test_url=TARGET_URL,
        navigate=True,
        timeout=45,
        env_overrides={"TLSNOTARY_ENABLED": "true"},
        crawl_setup=_install_and_prepare,
    ) as (_process, _pid, snapshot_chrome_dir, env):
        cdp_url = (snapshot_chrome_dir / "cdp_url.txt").read_text().strip()
        script = r"""
const http = require('node:http');
const chrome = require(process.argv[1]);
const targetUrl = process.argv[3];
const host = new URL(targetUrl).hostname;
const pathname = new URL(targetUrl).pathname;
let server;
let caller;
let browser;
async function approve(browser, extensionId, requestId) {
  const offscreen = await browser.waitForTarget(
    target => target.url() === `chrome-extension://${extensionId}/offscreen.html`,
    {timeout: 10000},
  );
  const session = await offscreen.createCDPSession();
  try {
    const result = await session.send('Runtime.evaluate', {
      expression: `chrome.runtime.sendMessage(${JSON.stringify({type: 'PLUGIN_CONFIRM_RESPONSE', requestId, mode: 'all-session'})})`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) throw new Error('Extension approval RPC failed');
  } finally {
    await session.detach();
  }
}
(async () => {
  const puppeteer = chrome.resolvePuppeteerModule();
  browser = await chrome.connectToBrowserEndpoint(puppeteer, process.argv[2], {defaultViewport: null});
  server = http.createServer((_request, response) => {
    response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
    response.end('<!doctype html><title>TLSNotary API caller</title>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  caller = await browser.newPage();
  await caller.goto(`http://127.0.0.1:${server.address().port}/`);
  await caller.waitForFunction(() => !!window.tlsn, {timeout: 10000});

  const extension = JSON.parse(process.argv[4]);
  const extensionId = extension.id;
  if (!extensionId) throw new Error('Prepared extension did not expose its runtime ID');
  const confirmation = browser.waitForTarget(
    target => target.url().startsWith(`chrome-extension://${extensionId}/`) && target.url().includes('confirm'),
    {timeout: 20000},
  );
  const code = `const config={name:'Header capture integration',description:'Capture request metadata for a real page',requests:[{method:'GET',host:${JSON.stringify(host)},pathname:${JSON.stringify(pathname)}}],urls:[${JSON.stringify(new URL(targetUrl).origin + '/*')}],timeout:60000};
const url=${JSON.stringify(targetUrl)};
function main(){
 const [request]=useHeaders(items=>items.filter(item=>item.url===url&&item.method==='GET').slice(-1));
 const started=useState('started',false);
 useEffect(()=>{if(!started){setState('started',true);openWindow(url,{width:1024,height:768}).catch(error=>done(JSON.stringify({error:String(error)})));}},[started]);
 useEffect(()=>{if(request){done(JSON.stringify({url:request.url,method:request.method,headerNames:request.requestHeaders.map(header=>header.name.toLowerCase())}));}},[!!request]);
 return div({},['Waiting for the real document request']);
}
export default {config,main};`;
  const resultPromise = caller.evaluate((pluginCode) =>
    window.tlsn.execCode(pluginCode, {sessionData: {mode: 'Mpc'}}), code,
  );
  resultPromise.catch(() => {});
  const confirmationTarget = await confirmation;
  const requestId = new URL(confirmationTarget.url()).searchParams.get('requestId');
  if (!requestId) throw new Error('Extension confirmation omitted request ID');
  await approve(browser, extensionId, requestId);
  const result = await resultPromise;
  const parsed = typeof result === 'string' ? JSON.parse(result) : result;
  process.stdout.write(JSON.stringify(parsed));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (caller) await caller.close().catch(() => {});
  if (browser) await browser.disconnect().catch(() => {});
  if (server) await new Promise(resolve => server.close(resolve));
});
"""
        completed = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(CHROME_UTILS),
                cdp_url,
                TARGET_URL,
                json.dumps(
                    json.loads(
                        (
                            Path(env["CRAWL_DIR"])
                            / "tlsnotary"
                            / "loaded-extension.json"
                        ).read_text(),
                    ),
                ),
            ],
            cwd=Path(env["SNAP_DIR"]),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["url"] == TARGET_URL
        assert result["method"] == "GET"
        assert {"accept", "user-agent"}.issubset(set(result["headerNames"]))


def test_real_hook_terminal_result_and_cleanup(tmp_path):
    """Verify terminal output and owned cleanup around a real live response."""

    with chrome_session(
        tmp_path,
        test_url=TARGET_URL,
        navigate=True,
        timeout=45,
        env_overrides={
            "TLSNOTARY_ENABLED": "true",
            "TLSNOTARY_TIMEOUT": "5",
        },
        crawl_setup=_install_and_prepare,
    ) as (_process, _pid, _snapshot_chrome_dir, env):
        hook_output_dir = Path(env["SNAP_DIR"]) / "tlsnotary"
        hook_output_dir.mkdir(parents=True)
        started = time.monotonic()
        result = subprocess.run(
            [str(SNAPSHOT_HOOK), f"--url={TARGET_URL}", "--snapshot-id=test-snapshot"],
            cwd=hook_output_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        elapsed = time.monotonic() - started
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{") and json.loads(line).get("type") == "ArchiveResult"
        ]

        assert elapsed < 15
        assert len(records) == 1, result.stdout
        if records[0]["status"] == "succeeded":
            # A faster public proof is valid only with its real signed receipt.
            assert result.returncode == 0, (result.stdout, result.stderr)
            verification = subprocess.run(
                [
                    env["NODE_BINARY"],
                    str(CHECK_CAPTURE),
                    str(hook_output_dir),
                    TRUSTED_PUBLIC_KEY,
                ],
                cwd=PLUGIN_DIR.parent.parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert verification.returncode == 0, verification.stderr
        else:
            assert result.returncode == 1, (result.stdout, result.stderr)
            assert records[0]["status"] == "failed"
            assert records[0]["output_str"] == "TLSNotary capture deadline exceeded"
        assert not (Path(env["CRAWL_DIR"]) / "tlsnotary" / "capture.lock").exists()
        extension = json.loads(
            (
                Path(env["CRAWL_DIR"]) / "tlsnotary" / "loaded-extension.json"
            ).read_text(),
        )
        browser_state = _inspect_browser(
            env,
            (Path(env["CRAWL_DIR"]) / "chrome" / "cdp_url.txt").read_text().strip(),
            extension["id"],
        )
        assert browser_state["extensionTargets"] == 0, browser_state
        assert TARGET_URL in browser_state["pageUrls"], browser_state
