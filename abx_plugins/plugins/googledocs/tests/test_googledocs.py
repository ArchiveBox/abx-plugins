"""Google export planning and real browser/network integration (no API credentials)."""

import json
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session


PLUGIN_DIR = Path(__file__).resolve().parents[1]
HOOK = PLUGIN_DIR / "on_Snapshot__53_googledocs.js"
CHROME_UTILS = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"


def node(script, env, *args):
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_export_plans(ensure_chrome_test_prereqs):
    import os

    result = node(
        """
        const {parseDocumentUrl, exportUrl} = require(process.argv[1]);
        const assert = require('node:assert/strict');
        const doc = parseDocumentUrl('https://docs.google.com/document/u/2/d/abc_123-XYZ/edit?resourcekey=key#tab=t.123');
        assert.equal(doc.kind, 'document');
        assert.equal(exportUrl(doc, 'docx'), 'https://docs.google.com/document/u/2/d/abc_123-XYZ/export?format=docx&authuser=2&resourcekey=key');
        const sheet = parseDocumentUrl('https://docs.google.com/spreadsheets/d/abc/edit?authuser=2#gid=42');
        assert.equal(exportUrl(sheet, 'csv'), 'https://docs.google.com/spreadsheets/d/abc/export?format=csv&authuser=2&gid=42');
        assert.equal(exportUrl(sheet, 'xlsx'), 'https://docs.google.com/spreadsheets/d/abc/export?format=xlsx&authuser=2');
        const slide = parseDocumentUrl('https://docs.google.com/presentation/d/abc/edit#slide=id.p');
        assert.equal(exportUrl(slide, 'pptx'), 'https://docs.google.com/presentation/d/abc/export/pptx');
        const drawing = parseDocumentUrl('https://docs.google.com/drawings/d/abc/edit');
        assert.equal(exportUrl(drawing, 'svg'), 'https://docs.google.com/drawings/d/abc/export/svg');
        for (const url of ['https://evil.test/document/d/abc/edit', 'https://docs.google.com.evil.test/document/d/abc', 'https://docs.google.com/document/d/e/published/pub', 'https://docs.google.com/forms/d/abc/edit', 'file:///document/d/abc']) {
            assert.equal(parseDocumentUrl(url), null, url);
        }
        console.log(JSON.stringify({ok: true}));
        """,
        os.environ,
        PLUGIN_DIR / "googledocs_utils.js",
    )
    assert result == {"ok": True}


def test_browser_resource_reuses_cookies_and_tab(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
):
    httpserver.expect_request("/").respond_with_data(
        "<title>Signed in</title>",
        content_type="text/html",
        headers={"Set-Cookie": "session=existing; HttpOnly; SameSite=Lax; Path=/"},
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    payload = bytes(range(256)) * 8192
    httpserver.expect_oneshot_request(
        "/export",
        headers={"Cookie": "session=existing"},
    ).respond_with_data(
        payload,
        content_type="application/octet-stream",
        headers={"Cache-Control": "max-age=3600"},
    )
    httpserver.expect_oneshot_request("/denied").respond_with_data(
        "Forbidden",
        status=403,
    )
    with chrome_session(tmp_path, test_url=httpserver.url_for("/")) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result = node(
            """
            const {connectToPage, downloadBrowserResource} = require(process.argv[1]);
            (async () => {
                const {browser, page, cdpSession, targetId} = await connectToPage({chromeSessionDir: process.argv[2], waitForNavigationComplete: true});
                try {
                    const before = (await browser.pages()).length;
                    const response = await downloadBrowserResource({cdpSession, url: process.argv[3], outputPath: process.argv[4], timeoutMs: 10000});
                    await downloadBrowserResource({cdpSession, url: process.argv[3], outputPath: process.argv[4] + '.cached', timeoutMs: 10000});
                    const assert = require('node:assert/strict');
                    await assert.rejects(downloadBrowserResource({cdpSession, url: new URL('/denied', process.argv[3]).href, outputPath: process.argv[4], timeoutMs: 10000}), /HTTP 403/);
                    console.log(JSON.stringify({before, after: (await browser.pages()).length, url: page.url(), targetId, response}));
                } finally { await browser.disconnect(); }
            })().catch(e => { console.error(e); process.exitCode = 1; });
            """,
            env,
            CHROME_UTILS,
            chrome_dir,
            httpserver.url_for("/export"),
            tmp_path / "download.bin",
        )
        assert result["before"] == result["after"]
        assert result["url"] == httpserver.url_for("/")
        assert result["targetId"] == (chrome_dir / "target_id.txt").read_text().strip()
        assert result["response"]["status"] == 200
    assert (tmp_path / "download.bin").read_bytes() == payload
    assert (tmp_path / "download.bin.cached").read_bytes() == payload
    httpserver.check_assertions()


@pytest.mark.parametrize(
    ("url", "config", "status", "code"),
    [
        ("https://example.com", {}, "noresults", 0),
        (
            "https://docs.google.com/document/d/abc/edit",
            {"GOOGLEDOCS_ENABLED": "false"},
            "skipped",
            0,
        ),
        ("https://docs.google.com/document/d/abc/edit", {}, "failed", 1),
    ],
)
def test_hook_without_browser(
    tmp_path,
    ensure_chrome_test_prereqs,
    url,
    config,
    status,
    code,
):
    import os

    result = subprocess.run(
        [str(HOOK), f"--url={url}"],
        cwd=tmp_path,
        env={**os.environ, "SNAP_DIR": str(tmp_path), **config},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == code, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record is not None
    assert record["status"] == status
