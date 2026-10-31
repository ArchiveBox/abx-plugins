"""Real cookie-authenticated downloads, tab isolation, and archive preservation."""

import json
import subprocess
import zipfile

import pytest
from pytest_httpserver import HTTPServer
from pathlib import Path

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins/plugins"


@pytest.mark.parametrize("concurrent_captures", [False, True])
def test_cookie_download_ignores_other_tab(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    concurrent_captures,
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
    with chrome_session(
        tmp_path / "session",
        test_url=httpserver.url_for("/"),
    ) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        script = """
const fs=require('fs'), path=require('path');
const {connectToPage,captureBrowserDownloads,acquireSessionLock,resolveChromeLaunchOptions}=require(process.argv[1]);
const {saveDownloads}=require(process.argv[2]);
(async()=>{
 const {browser,page}=await connectToPage({chromeSessionDir:process.argv[3],waitForNavigationComplete:true});
 const output=process.argv[4];
 const downloadPath=resolveChromeLaunchOptions().CHROME_DOWNLOADS_DIR;
 const other=await browser.newPage();
 try {
  await other.goto(process.argv[5]);
  const before=await page.evaluate(()=>performance.timeOrigin);
  const lock = path.join(downloadPath,'.download.lock');
  const release = await acquireSessionLock(lock,1000);
  // The old global policy lock must not serialize independent page observers.
  let files;
  if(process.argv[6] === 'true') {
   const batches = await Promise.all([
   captureBrowserDownloads({browser,page,downloadPath,timeoutMs:10000,
    trigger:async()=>{await page.click('a');}}),
   captureBrowserDownloads({browser,page:other,downloadPath,timeoutMs:10000,
    trigger:async()=>{await other.click('a');}}),
  ]);
   files = batches[0];
   if(batches[1].length !== 1 || fs.readFileSync(batches[1][0].filePath,'utf8') !== 'Other tab bytes') throw new Error('Wrong tab download');
  } else {
   files=await captureBrowserDownloads({browser,page,downloadPath,timeoutMs:10000,
    trigger:async()=>{await other.bringToFront();await other.click('a');await page.bringToFront();await page.click('a');}});
  }
  if(!fs.existsSync(lock)) throw new Error('Unrelated lock was modified');
  release();
  const manifest=await saveDownloads(path.join(output,'saved'),'Folder',files,{requireZip:true});
  const previous=fs.readFileSync(path.join(output,'saved/files/nested/hello.txt'));
  const originalManifest=fs.readFileSync(path.join(output,'saved/downloads.json'));
  const broken=path.join(output,'truncated.zip');fs.writeFileSync(broken,Buffer.from('PK incomplete archive'));
  let rejected=false;
  try {await saveDownloads(path.join(output,'saved'),'Bad',[{filePath:broken,suggestedFilename:'folder.zip'}],{requireZip:true});}
  catch(e){rejected=/extraction failed/.test(e.message);}
  console.log(JSON.stringify({manifest,rejected,preserved:previous.equals(fs.readFileSync(path.join(output,'saved/files/nested/hello.txt')))&&originalManifest.equals(fs.readFileSync(path.join(output,'saved/downloads.json'))),sameDocument:before===await page.evaluate(()=>performance.timeOrigin)}));
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
                str(concurrent_captures).lower(),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["sameDocument"] and report["preserved"] and report["rejected"]
        assert len(report["manifest"]["files"]) == 1
        assert (
            tmp_path / "output/saved/files/nested/hello.txt"
        ).read_text() == "Authenticated archive contents\n"
        assert not list((tmp_path / "output/saved").glob("*.zip"))
        assert list((tmp_path / "output/saved").glob(".unpack-*")) == []
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


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("late_frame", [False, True])
def test_provider_download_from_cross_origin_child_frame(
    tmp_path,
    ensure_chrome_test_prereqs,
    isolated,
    late_frame,
):
    """Observe a real out-of-process child without changing shared routing.

    WHY: export links may live in cross-origin provider frames. Page-domain
    events belong to the frame's CDP target, so observing only the top-level
    session can silently miss the actual download after removing global events.
    """
    # Chrome opens parallel connections for different sites and contexts.
    # A single-threaded server can block the child behind an idle keepalive.
    with HTTPServer(host="127.0.0.1", threaded=True) as httpserver:
        parent_url = httpserver.url_for("/").replace("localhost", "127.0.0.1")
        child_url = httpserver.url_for("/child").replace("127.0.0.1", "localhost")
        httpserver.expect_request("/").respond_with_data(
            "<!doctype html><title>Export</title>"
            + ("" if late_frame else f'<iframe src="{child_url}"></iframe>'),
            content_type="text/html",
        )
        httpserver.expect_request("/child").respond_with_data(
            "<script>window.downloadInput = [];"
            "for (const type of ['pointerdown', 'pointerup', 'click']) "
            "document.addEventListener(type, event => {"
            "if (event.isTrusted && event.target.tagName === 'A') "
            "window.downloadInput.push(type);}, true);</script>"
            '<a href="/download">Export child document</a>',
            content_type="text/html",
        )
        payload = b"Exact cross-origin child download\n" * 1000
        httpserver.expect_request("/download").respond_with_data(
            payload,
            content_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="child.txt"'},
        )
        httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
        with chrome_session(
            tmp_path / "session",
            test_url=parent_url,
            env_overrides={
                "CHROME_ARGS_EXTRA": json.dumps(["--site-per-process"]),
            },
        ) as (_, _, chrome_dir, env):
            script = r"""
    const fs = require('fs');
    const utils = require(process.argv[1]);
    (async () => {
      const {browser, page: ownerPage} = await utils.connectToPage({chromeSessionDir: process.argv[2], waitForNavigationComplete: true});
      let exportPage = null;
      try {
        if (process.argv[3] === 'true') {
          exportPage = await utils.openExportPage({page: ownerPage, chromeSessionDir: process.argv[2], isolateStorage: true});
          await exportPage.goto(await ownerPage.url());
        }
        const page = exportPage || ownerPage;
        const lateFrame = process.argv[5] === 'true';
        async function findChild() {
          const deadline = Date.now() + 10000;
          const child = await page.waitForFrame(frame => frame.url() === process.argv[4], {timeout: 10000});
          // Frame navigation precedes both usable child UI and the browser's
          // separate target inventory update. Check the ready child's own CDP
          // session, not an immediate global snapshot or another same-URL frame.
          const remaining = deadline - Date.now();
          if (remaining <= 0) throw new Error('Child frame readiness deadline exceeded');
          await child.waitForSelector('a', {visible: true, timeout: remaining});
          const {targetInfo} = await child.client.send('Target.getTargetInfo');
          if (targetInfo.type !== 'iframe' || targetInfo.url !== process.argv[4]) {
            throw new Error('Test did not create an out-of-process iframe');
          }
          return child;
        }
        const existingChild = lateFrame ? null : await findChild();
        const downloadDeadline = Date.now() + 10000;
        const files = await utils.captureBrowserDownloads({browser, page,
          downloadPath: utils.resolveChromeLaunchOptions().CHROME_DOWNLOADS_DIR, timeoutMs: 10000,
          trigger: async () => {
            if (lateFrame) await page.evaluate(url => {
              const iframe = document.createElement('iframe'); iframe.src = url;
              document.body.append(iframe);
            }, process.argv[4]);
            console.error('child-created');
            const child = existingChild || await findChild();
            console.error('child-target-ready');
            // A visible child DOM can precede compositor/input routing: a raw
            // click pressed the parent iframe and released the child anchor.
            // Use native locator readiness before the one trusted interaction.
            const remaining = downloadDeadline - Date.now();
            if (remaining <= 0) throw new Error('Child download deadline exceeded');
            await child.locator('a').setTimeout(remaining).click();
            const input = await child.evaluate(() => window.downloadInput);
            if (JSON.stringify(input) !== JSON.stringify(['pointerdown', 'pointerup', 'click'])) {
              throw new Error('Expected one trusted child click: ' + JSON.stringify(input));
            }
            console.error('child-click-completed');
          },
        });
        if (exportPage) {
          await utils.closeExportPage({page: exportPage, chromeSessionDir: process.argv[2]});
          exportPage = null;
          // Disposing the isolated context AND its policy-owning connection
          // must leave the crawl owner's default-context routing intact.
          await browser.disconnect();
          const next = await utils.connectToPage({chromeSessionDir: process.argv[2]});
          try {
            const after = await utils.captureBrowserDownloads({browser: next.browser, page: next.page,
              downloadPath: utils.resolveChromeLaunchOptions().CHROME_DOWNLOADS_DIR, timeoutMs: 10000,
              trigger: async () => next.page.evaluate(() => {
                const link = document.createElement('a');
                link.href = URL.createObjectURL(new Blob(['after isolated context']));
                link.download = 'after.txt'; document.body.append(link); link.click();
              }),
            });
            if (after.length !== 1 || fs.readFileSync(after[0].filePath, 'utf8') !== 'after isolated context') {
              throw new Error('Isolated context cleanup changed default download routing');
            }
          } finally { await next.browser.disconnect(); }
        }
        console.log(JSON.stringify(files.map(file => ({
          filename: file.suggestedFilename, path: file.filePath,
          bytes: fs.statSync(file.filePath).size,
        }))));
      } finally {
        if (exportPage) await utils.closeExportPage({page: exportPage, chromeSessionDir: process.argv[2]});
        await browser.disconnect();
      }
    })().catch(error => { console.error(error); process.exitCode = 1; });
    """
            result = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    script,
                    str(PLUGINS / "chrome/chrome_utils.js"),
                    str(chrome_dir),
                    str(isolated).lower(),
                    child_url,
                    str(late_frame).lower(),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            files = json.loads(result.stdout)
            assert len(files) == 1, files
            assert files[0]["filename"] == "child.txt"
            assert files[0]["bytes"] == len(payload)
            assert Path(files[0]["path"]).read_bytes() == payload
        assert (
            sum(request.path == "/download" for request, _response in httpserver.log)
            == 1
        )
