"""Preserve real inline PDF bytes before Chromium substitutes its viewer."""

import json
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.staticfile.tests.test_staticfile import (
    PLUGIN_DIR,
    chrome_session,
    run_staticfile_capture,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def test_pdf_response_capture_closes_after_tab_closed(tmp_path):
    with chrome_session(
        tmp_path,
        test_url="https://archivebox.io/tests/",
        navigate=False,
        timeout=45,
    ) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                """
const chrome = require(process.argv[1]);
(async () => {
  const {browser, page} = await chrome.connectToPage({chromeSessionDir: process.argv[2]});
  try {
    const capture = await chrome.captureOriginalResponseBodies(page);
    await page.close();
    await capture.close();
  } finally { await browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
""",
                str(PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"),
                str(chrome_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("responses_enabled", [True, False])
def test_inline_pdf_preserves_original_bytes(tmp_path, responses_enabled):
    web = tmp_path / "www"
    web.mkdir()
    (web / "index.html").write_text(
        "<!doctype html><title>Original PDF</title>"
        "<h1>ArchiveBox original PDF response</h1>"
        "<p>The archived document must contain these bytes, not a viewer.</p>",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=str(web)),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    url = f"{origin}/document.pdf"
    try:
        with chrome_session(
            tmp_path,
            test_url=url,
            navigate=False,
            timeout=45,
            env_overrides={"RESPONSES_ENABLED": str(responses_enabled)},
        ) as (_, _, chrome_dir, env):
            # Generate a real PDF through Chromium, then serve it as an ordinary
            # inline file. The capture itself uses the production hook sequence.
            generated = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    """
const chrome = require(process.argv[1]);
(async () => {
  const {browser} = await chrome.connectToPage({chromeSessionDir: process.argv[2]});
  try {
    const page = await browser.newPage();
    await page.goto(process.argv[3]);
    await page.pdf({path: process.argv[4]});
    await page.close();
  } finally { await browser.disconnect(); }
})().catch(error => { console.error(error); process.exit(1); });
""",
                    str(PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"),
                    str(chrome_dir),
                    f"{origin}/index.html",
                    str(web / "document.pdf"),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert generated.returncode == 0, generated.stderr
            expected = (web / "document.pdf").read_bytes()
            assert expected.startswith(b"%PDF-")
            staticfile_dir = chrome_dir.parent / "staticfile"
            staticfile_dir.mkdir()
            (
                code,
                stdout,
                stderr,
                nav,
                result,
                responses_dir,
                responses_stdout,
                responses_stderr,
            ) = run_staticfile_capture(
                staticfile_dir,
                chrome_dir,
                env,
                url,
                "test-inline-pdf",
                start_responses=responses_enabled,
                hook_timeout=45,
            )
            assert nav.returncode == 0, nav.stderr
            assert code == 0, stderr
            assert result is not None, stdout
            assert result["status"] == "succeeded", result
            assert (chrome_dir.parent / result["output_str"]).read_bytes() == expected
            if responses_enabled:
                # Saving the PDF is insufficient if the response daemon cannot
                # finish its own result or loses other captured response bodies.
                response_result = parse_jsonl_output(responses_stdout)
                assert response_result is not None, (responses_stdout, responses_stderr)
                assert response_result["status"] == "succeeded", (
                    response_result,
                    responses_stderr,
                )
                entries = [
                    json.loads(line)
                    for line in (responses_dir / "index.jsonl").read_text().splitlines()
                ]
                documents = [entry for entry in entries if entry.get("url") == url]
                assert len(documents) == 1, documents
                assert documents[0]["mimeType"] == "application/pdf"
                assert (responses_dir / documents[0]["path"]).read_bytes() == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pdf_response_collector_closes_with_live_viewer_frames(tmp_path):
    web = tmp_path / "www"
    web.mkdir()
    (web / "index.html").write_text(
        "<!doctype html><h1>Original PDF with live viewer frames</h1>",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=str(web)),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with chrome_session(
            tmp_path,
            test_url=f"{origin}/document.pdf",
            navigate=False,
            timeout=45,
        ) as (_, _, chrome_dir, env):
            result = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    """
const chrome = require(process.argv[1]);
const fs = require('fs');
(async () => {
  const {browser} = await chrome.connectToPage({chromeSessionDir: process.argv[2]});
  try {
    const source = await browser.newPage();
    await source.goto(process.argv[3] + '/index.html');
    await source.pdf({path: process.argv[4]});
    await source.close();
    const expected = fs.readFileSync(process.argv[4]);
    if (!expected.subarray(0, 5).equals(Buffer.from('%PDF-'))) throw Error('Invalid source PDF');
    // A bounded sequence exercises the real child-attachment race; every
    // capture must preserve its PDF and close, with no retry after a failure.
    for (let iteration = 0; iteration < 10; iteration++) {
      const page = await browser.newPage();
      const capture = await chrome.captureOriginalResponseBodies(page, {resourceTypes: []});
      let queue = Promise.resolve();
      let capturedPdf = false;
      const errors = [];
      const listener = request => {
        const response = request.response();
        if (!response || response.status() >= 400) return;
        queue = queue.then(async () => {
          try {
            const body = await capture.read(response);
            if (response.url() === process.argv[3] + '/document.pdf' && body) {
              if (!body.equals(expected)) throw Error('Original PDF bytes changed');
              capturedPdf = true;
            }
          } catch (error) { errors.push(error); }
        });
      };
      page.on('requestfinished', listener);
      await page.goto(process.argv[3] + '/document.pdf', {waitUntil: 'load', timeout: 20000});
      const viewer = await page.waitForFrame(frame => frame.url().startsWith('chrome-extension://'), {timeout: 10000});
      await viewer.waitForSelector('pdf-viewer', {timeout: 10000});
      await page.waitForFrame(frame => frame.parentFrame() === viewer, {timeout: 10000});
      page.off('requestfinished', listener);
      await queue;
      // Closing the tab first hides orphaned collector sessions by destroying
      // its children. A recorder must finish while the PDF viewer stays open.
      await capture.close();
      if (!capturedPdf) throw Error('Original PDF was not captured');
      if (errors.length) throw errors[0];
      if (!await viewer.$eval('pdf-viewer', node => node.isConnected)) throw Error('Closing collector damaged live PDF viewer');
      if (iteration === 9) {
        await page.bringToFront();
        const pageNumber = await viewer.waitForSelector('::-p-aria(Page number)', {timeout: 10000});
        if (await pageNumber.evaluate(input => input.value) !== '1') throw Error('PDF viewer did not render its first page');
        await page.screenshot({path: process.argv[5]});
      }
      await page.close();
    }
    console.log(JSON.stringify({completed: 10}));
  } finally { await browser.disconnect(); }
// Puppeteer retains the originating protocol command in error.cause; printing
// only .stack hides the call site when a target closes before its response.
})().catch(error => { console.error(error); process.exit(1); });
""",
                    str(PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"),
                    str(chrome_dir),
                    origin,
                    str(web / "document.pdf"),
                    str(tmp_path / "live-pdf-viewer.png"),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {"completed": 10}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
