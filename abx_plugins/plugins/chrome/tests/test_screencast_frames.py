"""Native screencast delivery must retain the final painted frame."""

import json
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_screencast_delivers_static_frame_after_paint_burst(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><html style='background:white'><title>Screencast paints</title></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    with chrome_session(tmp_path, test_url=httpserver.url_for("/")) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                r"""
const fs = require('fs');
const path = require('path');
const utils = require(process.argv[1]);
const {startPageScreencast} = require(path.join(path.dirname(process.argv[1]), '../chrome_screencast/screencast.js'));
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
  let stop, latest, delivered = 0;
  try {
    let firstFrame;
    const ready = new Promise(resolve => { firstFrame = resolve; });
    stop = await startPageScreencast(page, jpeg => {
      latest = jpeg; delivered++; firstFrame();
    }, {fps:1, bringToFront:true});
    await ready;
    // A finite real paint burst fills Chrome's frame window, then leaves a
    // static page. No later animation or screenshot may rescue a dropped frame.
    await page.evaluate(async () => {
      for (let frame = 0; frame < 12; frame++) {
        document.documentElement.style.background = frame % 2 ? 'red' : 'blue';
        await new Promise(requestAnimationFrame);
      }
      document.documentElement.style.background = 'rgb(0,255,0)';
    });
    const deadline = Date.now() + 10000;
    let pixel;
    while (Date.now() < deadline) {
      pixel = await page.evaluate(async data => {
        const bytes = Uint8Array.from(atob(data), char => char.charCodeAt(0));
        const bitmap = await createImageBitmap(new Blob([bytes], {type:'image/jpeg'}));
        const canvas = new OffscreenCanvas(1, 1);
        const context = canvas.getContext('2d');
        context.drawImage(bitmap, 0, 0, 1, 1);
        bitmap.close();
        return Array.from(context.getImageData(0, 0, 1, 1).data);
      }, latest.toString('base64'));
      if (pixel[1] > 240 && pixel[0] < 15 && pixel[2] < 15) break;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    fs.writeFileSync(process.argv[3], latest);
    console.log(JSON.stringify({pixel, delivered}));
  } finally { if (stop) await stop(); await browser.disconnect(); }
})().catch(error => {console.error(error); process.exit(1);});
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                str(tmp_path / "last-frame.jpg"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        red, green, blue, alpha = observed["pixel"]
        assert green > 240 and red < 15 and blue < 15 and alpha == 255, observed
        assert (tmp_path / "last-frame.jpg").read_bytes().startswith(b"\xff\xd8\xff")


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_screencast_follows_navigation_from_another_connection(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><html style='background:lime'><title>Navigation frame</title></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    with chrome_session(tmp_path, test_url=httpserver.url_for("/"), navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                r"""
const {spawn} = require('child_process');
const path = require('path');
const utils = require(process.argv[1]);
const {startPageScreencast} = require(path.join(path.dirname(process.argv[1]), '../chrome_screencast/screencast.js'));
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir:process.argv[2]});
  const events = [];
  const diagnostic = await page.createCDPSession();
  await diagnostic.send('Page.enable');
  diagnostic.on('Page.frameNavigated', ({frame}) => events.push({navigation:frame.url}));
  page.on('error', error => events.push({error:error.message}));
  const navigationListeners = page.listenerCount('framenavigated');
  let latest, stop;
  try {
    let firstFrame;
    const ready = new Promise(resolve => { firstFrame = resolve; });
    stop = await startPageScreencast(page, jpeg => {
      latest = jpeg;
      events.push({frame:Date.now(),bytes:jpeg.length});
      firstFrame();
    }, {fps:1, bringToFront:true});
    await ready;
    const initial = latest;
    const child = spawn(process.argv[3], ['--url='+process.argv[4]], {cwd:process.argv[2],env:process.env});
    let output = '';
    child.stdout.on('data', chunk => {output += chunk;});
    child.stderr.on('data', chunk => {output += chunk;});
    const code = await new Promise(resolve => child.once('exit', resolve));
    const deadline = Date.now()+10000;
    let pixel;
    while (Date.now()<deadline) {
      pixel = await page.evaluate(async data => {
        const bytes = Uint8Array.from(atob(data), char => char.charCodeAt(0));
        const bitmap = await createImageBitmap(new Blob([bytes], {type:'image/jpeg'}));
        const context = new OffscreenCanvas(1, 1).getContext('2d');
        context.drawImage(bitmap, 0, 0, 1, 1);
        bitmap.close();
        return Array.from(context.getImageData(0, 0, 1, 1).data);
      }, latest.toString('base64'));
      if (pixel[1]>240 && pixel[0]<15 && pixel[2]<15) break;
      await new Promise(resolve => setTimeout(resolve,50));
    }
    await stop(); stop = null;
    console.log(JSON.stringify({code,output,changed:!initial.equals(latest),pixel,events,url:page.url(),
      listenersCleaned:page.listenerCount('framenavigated')===navigationListeners}));
  } finally {if(stop) await stop(); await diagnostic.detach(); browser.disconnect();}
})().catch(error => {console.error(error);process.exit(1);});
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                str(CHROME_NAVIGATE_HOOK),
                httpserver.url_for("/"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert observed["code"] == 0, observed
        assert observed["changed"], observed
        red, green, blue, alpha = observed["pixel"]
        assert green > 240 and red < 15 and blue < 15 and alpha == 255, observed
        assert observed["listenersCleaned"], observed
