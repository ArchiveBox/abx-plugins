"""MHTML reports a loaded FontResource again after its fetch cache entry is collected."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from abx_plugins.plugins.base.testing import start_process_and_wait_for_file
from abx_plugins.plugins.responses.tests.test_responses import (
    CHROME_NAVIGATE_HOOK,
    RESPONSES_HOOK,
    chrome_session,
)

FIXTURES = Path(__file__).parent / "fixtures"
assert RESPONSES_HOOK is not None, "Responses hook is required for real browser tests"
CHROME_UTILS = RESPONSES_HOOK.parent.parent / "chrome/chrome_utils.js"
MHTML_HOOK = (
    RESPONSES_HOOK.parent.parent / "chrome_mhtml/on_Snapshot__54_chrome_mhtml.js"
)
OBSERVER = r"""
const fs = require('fs');
const utils = require(process.argv[1]);
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir: process.argv[2]});
  for (const name of ['Network.requestWillBeSent', 'Network.responseReceived', 'Network.requestServedFromCache']) {
    page.mainFrame().client.on(name, data => fs.appendFileSync(process.argv[3], JSON.stringify({name, data}) + '\n'));
  }
  fs.writeFileSync(process.argv[4], 'ready');
  process.on('SIGTERM', () => { browser.disconnect(); process.exit(0); });
})().catch(error => { console.error(error); process.exit(1); });
"""
EVICT_FETCH = r"""
const utils = require(process.argv[1]);
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir: process.argv[2]});
  await page.evaluate(async () => {
    await document.fonts.ready;
    if (![...document.fonts].some(font => font.family === 'Saved' && font.status === 'loaded')) throw new Error('Original font did not render');
  });
  await page.screenshot({path: process.argv[4]});
  await page.evaluate(async url => { await (await fetch(url, {cache: 'no-store'})).arrayBuffer(); }, process.argv[3]);
  // The temporary Fetch Resource replaces the FontResource in Blink's weak
  // URL map. Collect it deterministically instead of waiting for incidental GC.
  await page.mainFrame().client.send('HeapProfiler.collectGarbage');
  browser.disconnect();
})().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.fixture
def httpserver():
    with HTTPServer(threaded=True) as server:
        yield server


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize(
    "scenario",
    ["same-origin", "changed-origin", "service-worker", "observable-mismatch"],
)
def test_mhtml_cached_font_preserves_original_bytes(tmp_path, httpserver, scenario):
    original = (FIXTURES / "source-sans-pro.woff").read_bytes()
    changed = (FIXTURES / "forkawesome.woff2").read_bytes()
    first = httpserver.url_for("/")
    second = first.replace("localhost", "127.0.0.1")
    assert first != second
    font_url = httpserver.url_for("/font.woff")
    headers = {
        "Cache-Control": "public, max-age=3600",
        "Vary": "Origin, Referer" if scenario == "observable-mismatch" else "Origin",
        "Access-Control-Allow-Origin": "*",
    }
    for origin, body in [(first.rstrip("/"), original), (second.rstrip("/"), changed)]:
        httpserver.expect_request(
            "/font.woff",
            headers={"Origin": origin},
        ).respond_with_data(body, content_type="font/woff", headers=headers)
    # Same-origin fetch() omits Origin; this is a distinct real request from the
    # font loader and must not replace that loader's saved bytes or identity.
    httpserver.expect_request("/font.woff").respond_with_data(
        original,
        content_type="font/woff",
        headers=headers,
    )
    httpserver.expect_request("/").respond_with_data(
        f"<!doctype html><title>Cached font</title><style>@font-face{{font-family:Saved;src:url({font_url})}}body{{font-family:Saved}}</style><body>Original font bytes &#xf000;</body>",
        content_type="text/html",
    )
    httpserver.expect_request("/setup").respond_with_data(
        "<!doctype html><title>Worker setup</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/sw.js").respond_with_data(
        "self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));"
        "self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));"
        "self.addEventListener('fetch', event => event.respondWith(fetch(event.request)));",
        content_type="application/javascript",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    with chrome_session(
        tmp_path,
        test_url=first,
        navigate=False,
        timeout=30,
        env_overrides={"CHROME_MHTML_ENABLED": "true", "CHROME_WAIT_FOR": "load"},
    ) as (_, _, chrome, env):
        output = chrome.parent / "responses"
        output.mkdir()
        index = output / "index.jsonl"
        events = tmp_path / "network.jsonl"
        observer = start_process_and_wait_for_file(
            [
                env["NODE_BINARY"],
                "-e",
                OBSERVER,
                str(CHROME_UTILS),
                str(chrome),
                str(events),
                str(tmp_path / "observer-ready"),
            ],
            tmp_path / "observer-ready",
            cwd=chrome,
            env=env,
        )
        process = None
        stdout = stderr = ""
        url = first
        try:
            process = start_process_and_wait_for_file(
                [str(RESPONSES_HOOK), f"--url={first}"],
                index,
                cwd=output,
                env=env,
            )
            if scenario == "service-worker":
                setup = subprocess.run(
                    [
                        env["NODE_BINARY"],
                        "-e",
                        """
const utils = require(process.argv[1]);
(async () => {
 const {browser,page} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
 await page.goto(process.argv[3]);
 await page.evaluate(async () => {
   await navigator.serviceWorker.register('/sw.js');
   await navigator.serviceWorker.ready;
   if (!navigator.serviceWorker.controller) await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, {once:true}));
 });
 browser.disconnect();
})().catch(error => { console.error(error); process.exit(1); });
""",
                        str(CHROME_UTILS),
                        str(chrome),
                        httpserver.url_for("/setup"),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert setup.returncode == 0, setup.stderr
            for url in [first, second] if scenario == "changed-origin" else [first]:
                nav = subprocess.run(
                    [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                    cwd=chrome,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert nav.returncode == 0, nav.stderr
            fetched = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    EVICT_FETCH,
                    str(CHROME_UTILS),
                    str(chrome),
                    font_url,
                    str(tmp_path / "loaded-font.png"),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert fetched.returncode == 0, fetched.stderr
            mhtml = chrome.parent / "chrome_mhtml"
            mhtml.mkdir()
            saved = subprocess.run(
                [str(MHTML_HOOK), f"--url={url}"],
                cwd=mhtml,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert saved.returncode == 0, (saved.stdout, saved.stderr)
            assert "Original font bytes" in (mhtml / "snapshot.mhtml").read_text()
        finally:
            if process is not None:
                process.terminate()
                stdout, stderr = process.communicate(timeout=30)
                (tmp_path / "responses.stderr.log").write_text(stderr)
            observer.terminate()
            observer.communicate(timeout=15)

        observed = [json.loads(line) for line in events.read_text().splitlines()]
        requests = {
            e["data"]["requestId"]: e["data"]
            for e in observed
            if e["name"] == "Network.requestWillBeSent"
            and e["data"]["request"]["url"] == font_url
        }
        responses = {
            e["data"]["requestId"]: e["data"]["response"]
            for e in observed
            if e["name"] == "Network.responseReceived"
            and e["data"]["requestId"] in requests
        }
        cached_ids = [
            e["data"]["requestId"]
            for e in observed
            if e["name"] == "Network.requestServedFromCache"
            and e["data"]["requestId"] in requests
        ]
        assert len(cached_ids) == 1, observed
        cached_id = cached_ids[0]
        assert requests[cached_id]["request"]["headers"] == {"Referer": ""}
        original_ids = [
            request_id
            for request_id, request in requests.items()
            if request["type"] == "Font" and request_id != cached_id
        ]
        latest = responses[original_ids[-1]]
        cached = responses[cached_id]
        for field in ("responseTime", "connectionId", "timing", "headers"):
            assert cached[field] == latest[field], (field, latest, cached)
        if scenario == "service-worker":
            assert latest["fromServiceWorker"] is True
            assert latest["connectionId"] == 0
        else:
            assert (
                latest["responseTime"] > 0
                and latest["connectionId"] > 0
                and latest["timing"]["requestTime"] > 0
            )
        if scenario == "changed-origin":
            assert responses[original_ids[0]]["responseTime"] != latest["responseTime"]
            assert (
                requests[original_ids[0]]["request"]["headers"]["Origin"]
                != requests[original_ids[-1]]["request"]["headers"]["Origin"]
            )

        rows = [json.loads(line) for line in index.read_text().splitlines()]
        fonts = [r for r in rows if r["url"] == font_url]
        assert process is not None
        if scenario in ("service-worker", "observable-mismatch"):
            assert process.returncode == 0, (stdout, stderr)
            assert "WARN: Cannot read response body:" in stderr
            assert "No data found for resource" in stderr
            expected = [original, original]
        else:
            assert process.returncode == 0, (stdout, stderr)
            expected = (
                [original, changed, changed, changed]
                if scenario == "changed-origin"
                else [original] * 3
            )
        results = [
            json.loads(line) for line in stdout.splitlines() if line.startswith("{")
        ]
        record = next(row for row in results if row.get("type") == "ArchiveResult")
        assert record["status"] == "succeeded", record
        assert [(output / row["path"]).read_bytes() for row in fonts] == expected
        assert [row["responseSha256"] for row in fonts] == [
            hashlib.sha256(body).hexdigest() for body in expected
        ]
        assert sum(request.path == "/font.woff" for request, _ in httpserver.log) == (
            3 if scenario == "changed-origin" else 2
        )
