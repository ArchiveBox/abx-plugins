"""Preserve real inline PDF bytes before Chromium substitutes its viewer."""

import json
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

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
            code, stdout, stderr, nav, result, responses_dir, _, _ = (
                run_staticfile_capture(
                    staticfile_dir,
                    chrome_dir,
                    env,
                    url,
                    "test-inline-pdf",
                    start_responses=responses_enabled,
                    hook_timeout=45,
                )
            )
            assert nav.returncode == 0, nav.stderr
            assert code == 0, stderr
            assert result is not None, stdout
            assert result["status"] == "succeeded", result
            assert (chrome_dir.parent / result["output_str"]).read_bytes() == expected
            if responses_enabled:
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
