"""MHTML reports a loaded FontResource again after its fetch cache entry is collected."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from email import policy
from email.parser import BytesParser
from time import perf_counter

import pytest
from jinja2 import Environment
from playwright.sync_api import sync_playwright
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
  const started = Date.now();
  const phase = name => console.error(`MHTML font setup phase=${name} elapsed_ms=${Date.now() - started}`);
  phase('connect');
  const {browser, page} = await utils.connectToPage({chromeSessionDir: process.argv[2]});
  phase('font readiness');
  const rendered = await page.evaluate(async () => {
    await document.fonts.ready;
    if (![...document.fonts].some(font => font.family === 'Saved' && font.status === 'loaded')) throw new Error('Original font did not render');
    return {visibility: document.visibilityState, focused: document.hasFocus(), width: innerWidth, height: innerHeight};
  });
  console.error(`MHTML font viewport=${JSON.stringify(rendered)}`);
  // Match the screenshot hook: focus emulation reports a visible document but
  // does not activate the headless compositor after navigation. Activate the
  // target before requesting pixels so font setup cannot wait on an idle frame.
  await page.bringToFront();
  phase('screenshot');
  // Python kills this process at 30s. Before that loses the CDP connection,
  // distinguish a compositor command awaiting Chrome from a local Puppeteer
  // wait. These are public diagnostic errors, not protocol bodies/cookies.
  const pending = setTimeout(() => {
    console.error('MHTML font pending protocol:', browser.debugInfo.pendingProtocolErrors.map(error => error.message));
  }, 25000);
  pending.unref();
  try {
    await page.screenshot({path: process.argv[4]});
  } finally {
    clearTimeout(pending);
  }
  phase('no-store fetch');
  await page.evaluate(async url => { await (await fetch(url, {cache: 'no-store'})).arrayBuffer(); }, process.argv[3]);
  // The temporary Fetch Resource replaces the FontResource in Blink's weak
  // URL map. Collect it deterministically instead of waiting for incidental GC.
  phase('garbage collection');
  await page.mainFrame().client.send('HeapProfiler.collectGarbage');
  phase('disconnect');
  browser.disconnect();
  phase('complete');
})().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.fixture
def httpserver():
    with HTTPServer(threaded=True) as server:
        yield server


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize(
    "scenario",
    [
        "same-origin",
        "same-origin-icon",
        "changed-origin",
        "service-worker",
        "observable-mismatch",
        "unobservable-vary",
    ],
)
def test_mhtml_cached_font_preserves_original_bytes(tmp_path, httpserver, scenario):
    original = (
        FIXTURES
        / (
            "forkawesome.woff2"
            if scenario == "same-origin-icon"
            else "source-sans-pro.woff"
        )
    ).read_bytes()
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
    if scenario == "unobservable-vary":
        headers["Vary"] = "X-Original-Host, X-Geo-Country, accept-encoding"
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
            try:
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
            except subprocess.TimeoutExpired as error:
                # TimeoutExpired omits captured stderr, including the last
                # real browser operation reached before the setup stalled.
                diagnostics = error.stderr or b""
                if isinstance(diagnostics, bytes):
                    diagnostics = diagnostics.decode(errors="replace")
                error.add_note(diagnostics)
                raise
            print(fetched.stderr)
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
        if scenario == "unobservable-vary":
            assert latest["headers"]["Vary"] == headers["Vary"]
            original_headers = {
                name.lower()
                for name in requests[original_ids[-1]]["request"]["headers"]
            }
            assert not {"x-original-host", "x-geo-country"} & original_headers
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
            assert process.returncode == 1, (stdout, stderr)
            assert "No data found for resource" in stderr
            expected = [original, original]
        else:
            assert process.returncode == 0, (stdout, stderr)
            expected = (
                [original, changed, changed, changed]
                if scenario == "changed-origin"
                else [original] * 3
            )
        assert [(output / row["path"]).read_bytes() for row in fonts] == expected
        assert [row["responseSha256"] for row in fonts] == [
            hashlib.sha256(body).hexdigest() for body in expected
        ]
        assert sum(request.path == "/font.woff" for request, _ in httpserver.log) == (
            3 if scenario == "changed-origin" else 2
        )

    # Replay the actual native MHTML after the original site has gone away.
    # The independently saved font must work in both card and full views.
    embedded_font = base64.b64encode(expected[-1]).decode()
    httpserver.expect_request("/embedded-font").respond_with_data(
        '<!doctype html><meta charset="utf-8"><style>@font-face{font-family:Saved;'
        f"src:url(data:font/woff;base64,{embedded_font})}}body{{font-family:Saved}}"
        "</style><body>Original font bytes &#xf000;</body>",
        content_type="text/html",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=env["CHROME_BINARY"])
        page = browser.new_page()
        page.goto(httpserver.url_for("/embedded-font"))
        page.evaluate("async () => { await document.fonts.ready; }")
        embedded_mhtml = (
            page.context.new_cdp_session(page)
            .send("Page.captureSnapshot", {"format": "mhtml"})["data"]
            .encode()
        )
        browser.close()
    embedded_parts = BytesParser(policy=policy.default).parsebytes(embedded_mhtml)
    assert any(
        isinstance(body := part.get_payload(decode=True), bytes)
        and embedded_font.encode() in body
        for part in embedded_parts.walk()
    )
    httpserver.stop()
    raw_mhtml = (mhtml / "snapshot.mhtml").read_bytes()
    templates = MHTML_HOOK.parent / "templates"
    with HTTPServer(threaded=True) as replay, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=env["CHROME_BINARY"])
        for manifest in ("saved", "embedded", "absent", "malformed", "unsafe"):
            prefix = f"/{manifest}"
            output_path = f"{prefix}/chrome_mhtml/snapshot.mhtml"
            replay.expect_request(output_path, query_string="").respond_with_data(
                embedded_mhtml if manifest == "embedded" else raw_mhtml,
                content_type="multipart/related",
            )
            viewer = (templates / "full.html").read_text()
            viewer = viewer.replace("{{ output_path }}", output_path).replace(
                '{{ output_path_raw|default:"snapshot.mhtml" }}',
                "snapshot.mhtml",
            )
            replay.expect_request(
                output_path,
                query_string="preview=1",
            ).respond_with_data(viewer, content_type="text/html")
            card = (
                Environment(autoescape=True)
                .from_string((templates / "card.html").read_text())
                .render(output_path=output_path)
            )
            replay.expect_request(f"{prefix}/card.html").respond_with_data(
                card,
                content_type="text/html",
            )
            manifest_bytes = index.read_bytes()
            if manifest == "unsafe":
                manifest_bytes = "\n".join(
                    json.dumps({**row, "path": "../outside.woff"}) for row in rows
                ).encode()
            elif manifest not in ("saved", "embedded"):
                manifest_bytes = b"invalid JSONL"
            replay.expect_request(f"{prefix}/responses/index.jsonl").respond_with_data(
                manifest_bytes,
                content_type="application/x-ndjson",
                status=404 if manifest == "absent" else 200,
            )
            for row in fonts:
                replay.expect_request(
                    f"{prefix}/responses/{row['path'].removeprefix('./')}",
                ).respond_with_data(
                    (output / row["path"]).read_bytes(),
                    content_type=row["mimeType"],
                )
            for surface in ("full", "card"):
                context = browser.new_context()
                page = context.new_page()
                requested = []
                page.on("request", lambda request: requested.append(request.url))
                started = perf_counter()
                page.goto(
                    replay.url_for(output_path) + "?preview=1"
                    if surface == "full"
                    else replay.url_for(f"{prefix}/card.html"),
                )
                rendered = page.frame_locator("iframe")
                if surface == "card":
                    rendered = rendered.frame_locator("iframe")
                assert (
                    rendered.locator("body").inner_text()
                    == "Original font bytes \uf000"
                )
                loaded = rendered.locator("body").evaluate(
                    """async () => {
                        await document.fonts.ready;
                        return [...document.fonts].filter(font => font.family === 'Saved')
                            .map(font => font.status);
                    }""",
                )
                elapsed_ms = (perf_counter() - started) * 1000
                timing = page.evaluate("""() => {
                    const navigation = performance.getEntriesByType('navigation')[0];
                    return {ttfb_ms:navigation.responseStart-navigation.requestStart,
                        index:performance.getEntriesByType('resource')
                            .filter(entry=>entry.name.includes('/responses/index.jsonl'))
                            .map(entry=>({duration_ms:entry.duration,bytes:entry.decodedBodySize}))};
                }""")
                print(
                    f"MHTML replay scenario={scenario} manifest={manifest} surface={surface} "
                    f"elapsed_ms={elapsed_ms:.1f} manifest_bytes={len(manifest_bytes)} timing={timing}",
                )
                assert not any("outside.woff" in request for request in requested)
                if manifest == "embedded" or (
                    manifest == "saved" and scenario != "changed-origin"
                ):
                    assert loaded == ["loaded"], (scenario, surface, requested)
                    assert font_url not in requested
                else:
                    # Distinct native FontResources can share a URL. Do not
                    # silently choose another origin's bytes for that URL.
                    assert loaded == ["error"], (scenario, manifest, surface)
                page.screenshot(path=str(tmp_path / f"{manifest}-{surface}.png"))
                context.close()
        browser.close()
    assert (mhtml / "snapshot.mhtml").read_bytes() == raw_mhtml
