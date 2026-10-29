"""Cached image responses must remain available across real renderer sessions."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from abx_plugins.plugins.base.testing import start_process_and_wait_for_file
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import CHROME_UTILS
from abx_plugins.plugins.responses.tests.test_responses import (
    CHROME_NAVIGATE_HOOK,
    RESPONSES_HOOK,
    chrome_session,
)

IMAGE = Path(__file__).parents[4] / "docs/assets/plugins/singlefile.png"
BODY_A = IMAGE.read_bytes()
BODY_B = (Path(__file__).parents[4] / "docs/assets/icon.png").read_bytes()

IMAGE_EVENTS = r"""
const fs = require('fs');
const crypto = require('crypto');
const utils = require(process.argv[1]);
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const normalized = headers => Object.fromEntries(Object.entries(headers || {})
  .map(([name, value]) => [name.toLowerCase(), String(value)])
  .sort(([a], [b]) => a.localeCompare(b)));
const hashObject = value => digest(JSON.stringify(value));
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir: process.argv[2]});
  const output = process.argv[3];
  const clients = new WeakMap();
  const watched = new WeakSet();
  const types = new Map();
  const requests = new Map();
  let clientNumber = 0;
  const record = (client, eventName, event, extra = {}) => {
    const clientId = clients.get(client);
    const key = `${clientId}:${event.requestId}`;
    const request = requests.get(key) || {};
    const url = event.request?.url || event.response?.url || request.url || '';
    const resourceType = event.type || types.get(key);
    if (resourceType !== 'Image') return;
    const row = {
      at: Date.now(), client: clientId, event: eventName,
      requestId: event.requestId, frameId: event.frameId || request.frameId || null,
      urlHash: digest(url), resourceType,
      initiator: event.initiator?.type || request.initiator || null,
      ...extra,
    };
    if (event.request) {
      const requestHeaders = normalized(event.request.headers);
      requests.set(key, {
        ...request,
        url,
        frameId: event.frameId || null,
        initiator: event.initiator?.type || null,
        requestHeaders,
      });
      row.requestHeaderNames = Object.keys(requestHeaders);
      row.requestHeaderHash = hashObject(requestHeaders);
    }
    if (event.response) {
      const responseHeaders = normalized(event.response.headers);
      const requestHeaders = requests.get(key)?.requestHeaders || {};
      row.requestHeaderNames = Object.keys(requestHeaders);
      row.requestHeaderHash = hashObject(requestHeaders);
      const varyNames = (responseHeaders.vary || '').toLowerCase()
        .split(',').map(name => name.trim()).filter(Boolean);
      const varyInputs = varyNames.map(name => [name, requestHeaders[name] ?? null]);
      const timingHash = hashObject(event.response.timing || null);
      row.status = event.response.status;
      row.responseHeaderNames = Object.keys(responseHeaders);
      row.responseHeaderHash = hashObject(responseHeaders);
      row.varyNames = varyNames;
      row.varyInputsPresent = varyNames.every(name => Object.hasOwn(requestHeaders, name));
      row.varyInputsHash = hashObject(varyInputs);
      row.responseTime = event.response.responseTime || null;
      row.connectionId = event.response.connectionId || null;
      row.timingHash = timingHash;
      row.transportIdentityHash = hashObject([
        url, event.request?.method || requests.get(key)?.method || 'GET',
        event.response.status, responseHeaders,
        event.response.responseTime, event.response.connectionId, event.response.timing,
      ]);
      requests.set(key, { ...requests.get(key), method: event.request?.method || 'GET' });
    }
    fs.appendFileSync(output, JSON.stringify(row) + String.fromCharCode(10));
  };
  const attach = client => {
    if (!client || watched.has(client)) return;
    watched.add(client);
    clients.set(client, ++clientNumber);
    client.on('Target.attachedToTarget', ({sessionId, targetInfo}) => {
      if (targetInfo.type !== 'iframe') return;
      const child = client.connection()?.session(sessionId);
      if (child) attach(child);
    });
    client.on('Network.requestWillBeSent', event => {
      types.set(`${clients.get(client)}:${event.requestId}`, event.type);
      record(client, 'request', event);
    });
    client.on('Network.responseReceived', event => record(client, 'response', event));
    client.on('Network.requestServedFromCache', event => record(client, 'cache', event));
  };
  attach(page.mainFrame().client);
  fs.writeFileSync(process.argv[4], 'ready');
  process.on('SIGTERM', () => {
    browser.disconnect();
    process.exit(0);
  });
})().catch(error => {
  console.error(error.name);
  process.exit(1);
});
"""

LOAD_SECOND_FRAME = r"""
 (async () => {
const {browser, page} = await (async () => {
  const utils = require(process.argv[1]);
  return utils.connectToPage({chromeSessionDir: process.argv[2]});
})();
try {
  // A generic Puppeteer timeout does not identify whether frame attachment or
  // image completion stalled. Keep the real phase/URLs in failed CI output;
  // this must not turn a missing frame into a retry or a weaker body assertion.
  console.error('cached-image: attached frames', JSON.stringify(page.frames().map(frame => frame.url())));
  const first = await page.waitForFrame(frame => frame.url().endsWith('#first'), {timeout: 15000});
  console.error('cached-image: first frame');
  const second = await page.waitForFrame(frame => frame.url().includes('#second-'), {timeout: 15000});
  console.error('cached-image: second frame');
  await first.waitForFunction(() => document.documentElement.dataset.imageReady === 'true', {timeout: 15000});
  console.error('cached-image: first image');
  await page.evaluate(() => document.querySelector('#second').contentWindow.postMessage('load-image', '*'));
  await second.waitForFunction(() => document.documentElement.dataset.imageReady === 'true', {timeout: 15000});
  console.error('cached-image: second image');
  console.log(JSON.stringify({firstFrame: true, secondFrame: true}));
} catch (error) {
  console.error('cached-image: frames at failure', JSON.stringify(page.frames().map(frame => frame.url())));
  throw error;
} finally { browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.fixture
def image_server():
    with HTTPServer(threaded=True) as server:
        yield server


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("variant", ["same", "changed"])
def test_cached_image_body_identity_across_oopif_sessions(
    tmp_path,
    image_server,
    variant,
):
    # Separate renderer sessions expose Chrome's missing cached-body buffer.
    # The changed-Origin case must fetch and save different bytes: matching a
    # URL alone would make the cached case pass while corrupting Vary responses.
    base = image_server.url_for("/")
    first_origin = base.replace("localhost", "oopif-child-a.test", 1).rstrip("/")
    second_origin = base.replace("localhost", "oopif-child-b.test", 1).rstrip("/")
    image_url = first_origin + "/image.png"
    first_child = first_origin + "/child"
    second_child = (second_origin if variant == "changed" else first_origin) + "/child"
    headers = {
        "Cache-Control": "public, max-age=3600, immutable",
        "Vary": "Origin",
        "Access-Control-Allow-Origin": "*",
    }
    image_server.expect_request("/", method="GET").respond_with_data(
        f'<iframe id="first" src="{first_child}#first"></iframe>'
        f'<iframe id="second" src="{second_child}#second-{variant}"></iframe>',
        content_type="text/html",
    )
    image_server.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    child_html = """<!doctype html><meta charset=utf-8><body><script>
      function loadImage() {
        const image = new Image();
        image.crossOrigin = 'anonymous';
        image.onload = () => { document.documentElement.dataset.imageReady = 'true'; };
        image.src = '__IMAGE_URL__';
        document.body.append(image);
      }
      if (location.hash === '#first') loadImage();
      else addEventListener('message', event => {
        if (event.data === 'load-image') loadImage();
      });
    </script></body>""".replace("__IMAGE_URL__", image_url)
    image_server.expect_request("/child").respond_with_data(
        child_html,
        content_type="text/html",
    )
    image_server.expect_request(
        "/image.png",
        headers={"Origin": first_origin},
    ).respond_with_data(
        BODY_A,
        content_type="image/png",
        headers={**headers, "ETag": '"origin"'},
    )
    if variant == "changed":
        image_server.expect_request(
            "/image.png",
            headers={"Origin": second_origin},
        ).respond_with_data(
            BODY_B,
            content_type="image/png",
            headers={**headers, "ETag": '"path"'},
        )

    page_url = base
    chrome_options = json.dumps(
        [
            "--site-per-process",
            "--host-resolver-rules=MAP oopif-child-a.test 127.0.0.1,MAP oopif-child-b.test 127.0.0.1",
        ],
    )
    with chrome_session(
        tmp_path,
        test_url=page_url,
        navigate=False,
        timeout=30,
        env_overrides={"CHROME_ARGS_EXTRA": chrome_options},
    ) as (_, _, chrome, env):
        output = chrome.parent / "responses"
        output.mkdir()
        index = output / "index.jsonl"
        trace = tmp_path / "image-events.jsonl"
        observer = start_process_and_wait_for_file(
            [
                env["NODE_BINARY"],
                "-e",
                IMAGE_EVENTS,
                str(CHROME_UTILS),
                str(chrome),
                str(trace),
                str(tmp_path / "observer-ready"),
            ],
            tmp_path / "observer-ready",
            cwd=chrome,
            env=env,
        )
        recorder = start_process_and_wait_for_file(
            [str(RESPONSES_HOOK), f"--url={page_url}"],
            index,
            cwd=output,
            env=env,
        )
        try:
            navigation = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK), f"--url={page_url}"],
                cwd=chrome,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert navigation.returncode == 0, navigation.stderr
            loaded = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    LOAD_SECOND_FRAME,
                    str(CHROME_UTILS),
                    str(chrome),
                ],
                cwd=chrome,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert loaded.returncode == 0, loaded.stderr
        finally:
            recorder.terminate()
            stdout, stderr = recorder.communicate(timeout=30)
            observer.terminate()
            observer.communicate(timeout=15)

        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        image_key = hashlib.sha256(image_url.encode()).hexdigest()
        image_events = [row for row in rows if row.get("urlHash") == image_key]
        requests = [row for row in image_events if row["event"] == "request"]
        responses = [row for row in image_events if row["event"] == "response"]
        cache_events = [row for row in image_events if row["event"] == "cache"]
        records = [json.loads(line) for line in index.read_text().splitlines()]
        saved = [row for row in records if row.get("urlSha256") == image_key]

        def assert_saved_bodies(expected):
            assert len(saved) == len(expected)
            for record, body in zip(saved, expected, strict=True):
                saved_body = (output / record["path"]).read_bytes()
                assert saved_body == body
                assert record["responseSha256"] == hashlib.sha256(body).hexdigest()

        summary = {
            "variant": variant,
            "requestCount": len(requests),
            "frameIds": [row["frameId"] for row in requests],
            "clientIds": [row["client"] for row in requests],
            "initiators": [row["initiator"] for row in requests],
            "cacheServedCount": len(cache_events),
            "responseIdentity": [
                {
                    "status": row.get("status"),
                    "responseHeaderHash": row.get("responseHeaderHash"),
                    "transportIdentityHash": row.get("transportIdentityHash"),
                    "requestHeaderNames": row.get("requestHeaderNames"),
                    "requestHeaderHash": row.get("requestHeaderHash"),
                    "varyNames": row.get("varyNames"),
                    "varyInputsPresent": row.get("varyInputsPresent"),
                    "varyInputsHash": row.get("varyInputsHash"),
                    "responseTime": row.get("responseTime"),
                    "connectionId": row.get("connectionId"),
                    "timingHash": row.get("timingHash"),
                }
                for row in responses
            ],
            "savedResponseHashes": [row.get("responseSha256") for row in saved],
            "savedResponseCount": len(saved),
            "responseExit": recorder.returncode,
            "bodyUnavailable": "No resource with given identifier found" in stderr,
        }
        print(json.dumps(summary))
        assert len(requests) == 2, summary
        assert requests[0]["frameId"] != requests[1]["frameId"], summary
        if variant == "same":
            assert requests[1]["initiator"] == "script", summary
        assert len({row["client"] for row in requests}) == 2, summary

        if variant == "same":
            assert len(cache_events) == 1, summary
            assert (
                responses[0]["responseHeaderHash"] == responses[1]["responseHeaderHash"]
            ), summary
            assert (
                responses[0]["transportIdentityHash"]
                == responses[1]["transportIdentityHash"]
            ), summary
            assert (
                responses[0]["varyInputsPresent"] and responses[1]["varyInputsPresent"]
            ), summary
            assert responses[0]["varyInputsHash"] == responses[1]["varyInputsHash"], (
                summary
            )
            assert recorder.returncode == 0, stderr
            assert_saved_bodies([BODY_A, BODY_A])
            assert (
                image_server.log
                and sum(req.path == "/image.png" for req, _ in image_server.log) == 1
            )
        else:
            assert not cache_events, summary
            assert responses[0]["varyInputsHash"] != responses[1]["varyInputsHash"], (
                summary
            )
            assert (
                image_server.log
                and sum(req.path == "/image.png" for req, _ in image_server.log) == 2
            )
            assert_saved_bodies([BODY_A, BODY_B])
            assert recorder.returncode == 0, stderr
