"""Real cookie-authenticated downloads, tab isolation, and archive preservation."""

import json
import subprocess
import zipfile

import pytest
from pathlib import Path

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins/plugins"


def test_cookie_download_ignores_other_tab(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
):
    payload_path = tmp_path / "source.zip"
    with zipfile.ZipFile(
        payload_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("nested/hello.txt", "Authenticated archive contents\n")
    payload = payload_path.read_bytes()
    httpserver.expect_request("/").respond_with_data(
        '<a href="/archive.zip">Download</a>',
        content_type="text/html",
        headers={"Set-Cookie": "session=existing; HttpOnly; SameSite=Lax; Path=/"},
    )
    httpserver.expect_request("/other").respond_with_data(
        '<a href="/unrelated.zip">Other download</a>',
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    httpserver.expect_oneshot_request(
        "/archive.zip",
        headers={"Cookie": "session=existing"},
    ).respond_with_data(
        payload,
        content_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="folder.zip"'},
    )
    httpserver.expect_oneshot_request("/unrelated.zip").respond_with_data(
        b"Other tab bytes",
        content_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="folder.zip"'},
    )
    with chrome_session(tmp_path / "session", test_url=httpserver.url_for("/")) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        script = """
const fs=require('fs'), path=require('path');
const {connectToPage,captureBrowserDownloads}=require(process.argv[1]);
const {saveDownloads}=require(process.argv[2]);
(async()=>{
 const {browser,page}=await connectToPage({chromeSessionDir:process.argv[3],waitForNavigationComplete:true});
 const output=process.argv[4];
 const other=await browser.newPage();
 try {
  await other.goto(process.argv[5]);
  const before=await page.evaluate(()=>performance.timeOrigin);
  const files=await captureBrowserDownloads({browser,page,downloadPath:path.join(output,'browser'),timeoutMs:10000,
   trigger:async()=>{await other.bringToFront();await other.click('a');await page.bringToFront();await page.click('a');}});
  const manifest=await saveDownloads(path.join(output,'saved'),'Folder',files,{requireZip:true});
  const previous=fs.readFileSync(path.join(output,'saved/download-1.zip'));
  const originalManifest=fs.readFileSync(path.join(output,'saved/downloads.json'));
  const broken=path.join(output,'truncated.zip');fs.writeFileSync(broken,previous.subarray(0,previous.length-10));
  let rejected=false;
  try {await saveDownloads(path.join(output,'saved'),'Bad',[{filePath:broken,suggestedFilename:'folder.zip'}],{requireZip:true});}
  catch(e){rejected=/truncated ZIP/.test(e.message);}
  console.log(JSON.stringify({manifest,rejected,preserved:previous.equals(fs.readFileSync(path.join(output,'saved/download-1.zip')))&&originalManifest.equals(fs.readFileSync(path.join(output,'saved/downloads.json'))),sameDocument:before===await page.evaluate(()=>performance.timeOrigin)}));
 } finally {await other.close();await browser.disconnect();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
"""
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                script,
                str(PLUGINS / "chrome/chrome_utils.js"),
                str(PLUGINS / "base/downloads.js"),
                str(chrome_dir),
                str(tmp_path / "output"),
                httpserver.url_for("/other"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["sameDocument"] and report["preserved"] and report["rejected"]
        assert len(report["manifest"]["downloads"]) == 1
        assert (tmp_path / "output/saved/download-1.zip").read_bytes() == payload
        assert list((tmp_path / "output/saved").glob(".*.tmp")) == []
    httpserver.check_assertions()


@pytest.mark.parametrize(
    "user_agent",
    [
        "Mozilla/5.0 (compatible; ArchiveBox/1.0)",
        "Mozilla/5.0 (compatible; abx-dl/1.0; +https://github.com/ArchiveBox/abx-dl)",
    ],
)
def test_generic_http_user_agent_keeps_native_chrome(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    user_agent,
):
    httpserver.expect_request("/").respond_with_data(
        "Browser identity test",
        content_type="text/html",
    )
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/"),
        env_overrides={"CHROME_USER_AGENT": user_agent},
    ) as (_, _, chrome_dir, env):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                "const u=require(process.argv[1]);(async()=>{const {browser,page}=await u.connectToPage({chromeSessionDir:process.argv[2]});try{console.log(JSON.stringify(await page.evaluate(()=>navigator.userAgent)));}finally{await browser.disconnect();}})().catch(e=>{console.error(e);process.exitCode=1;});",
                str(PLUGINS / "chrome/chrome_utils.js"),
                str(chrome_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        actual = json.loads(result.stdout)
        assert "Chrome/" in actual
        assert "ArchiveBox/" not in actual and "abx-dl/" not in actual
