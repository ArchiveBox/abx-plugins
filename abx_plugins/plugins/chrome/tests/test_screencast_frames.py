"""Native screencast delivery must retain the final painted frame."""

import json
import re
import subprocess

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    CHROME_UTILS,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_screencast_limits_native_frame_acquisition(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><title>Screencast acquisition</title>"
        "<style>@keyframes move {to {transform:translateX(100px)}}"
        "div{width:600px;height:400px;background:repeating-linear-gradient(45deg,red 0 20px,blue 20px 40px);"
        "animation:move .5s linear infinite alternate}</style><div></div>",
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
const fs=require('fs'),path=require('path');
const utils=require(process.argv[1]);
const {startPageScreencast}=require(path.join(path.dirname(process.argv[1]),'../chrome_screencast/screencast.js'));
(async()=>{
  const {browser,page}=await utils.connectToPage({chromeSessionDir:process.argv[2]});
  let stop,frames=0,bytes=0;
  try {
    stop=await startPageScreencast(page,jpeg=>{frames++;bytes+=jpeg.length;fs.writeFileSync(process.argv[3],jpeg);},{fps:1,bringToFront:true});
    await new Promise(resolve=>setTimeout(resolve,6000));
    await stop();stop=null;
    console.log(JSON.stringify({frames,bytes}));
  } finally {if(stop)await stop();await browser.disconnect();}
})().catch(error=>{console.error(error);process.exit(1);});
""",
                str(CHROME_UTILS),
                str(chrome_dir),
                str(tmp_path / "last-frame.jpg"),
            ],
            env=env | {"NODE_DEBUG": "puppeteer:protocol:RECV*"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        (tmp_path / "screencast-protocol.log").write_text(result.stderr)
        assert result.returncode == 0, result.stderr[-2000:]
        observed = json.loads(result.stdout)
        # Count real incoming CDP events, not callbacks already throttled by JS.
        acquired = len(
            re.findall(r'"method"\s*:\s*"Page\.screencastFrame"', result.stderr),
        )
        assert 4 <= observed["frames"] <= 7, observed
        assert observed["bytes"] > 1000
        assert (tmp_path / "last-frame.jpg").read_bytes().startswith(b"\xff\xd8\xff")
        assert observed["frames"] <= acquired <= 9, {"acquired": acquired, **observed}


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
  const navigationListeners = page.listenerCount('domcontentloaded');
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
      listenersCleaned:page.listenerCount('domcontentloaded')===navigationListeners}));
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


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_concurrent_snapshot_screencasts_keep_their_own_frames(tmp_path, httpserver):
    """Two real tabs in one crawl cannot replace or delete each other's frames."""
    import hashlib
    import signal
    import time
    from pathlib import Path

    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import launch_snapshot_tab

    for name, color in (("red", "red"), ("blue", "blue")):
        httpserver.expect_request(f"/{name}").respond_with_data(
            f"<!doctype html><html style='background:{color}'><title>{name}</title>"
            "<style>@keyframes move {to {transform:translateY(100px)}}"
            "div{position:absolute;right:0;width:10px;height:10px;background:white;"
            "animation:move 0.3s infinite alternate}</style><div></div></html>",
            content_type="text/html",
        )
    httpserver.expect_request("/favicon.ico").respond_with_data(
        "",
        content_type="image/x-icon",
    )
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/red"),
        env_overrides={
            "TMP_DIR": str(tmp_path / "runtime"),
            "CHROME_SCREENCAST_KEEP": "1",
        },
    ) as (_, _, chrome_dir, env):
        second_dir = tmp_path / "snap" / "second"
        (second_dir / "chrome").mkdir(parents=True)
        second_env = env | {"SNAP_DIR": str(second_dir)}
        tab = launch_snapshot_tab(
            snapshot_chrome_dir=second_dir / "chrome",
            tab_env=second_env,
            test_url=httpserver.url_for("/blue"),
            snapshot_id="second",
            crawl_id="test-crawl",
        )
        casts = []
        try:
            navigation = subprocess.run(
                [
                    str(CHROME_NAVIGATE_HOOK),
                    f"--url={httpserver.url_for('/blue')}",
                    "--snapshot-id=second",
                ],
                cwd=second_dir / "chrome",
                env=second_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert navigation.returncode == 0, navigation.stderr
            assert (chrome_dir / "cdp_url.txt").read_text() == (
                second_dir / "chrome" / "cdp_url.txt"
            ).read_text()
            assert (chrome_dir / "target_id.txt").read_text() != (
                second_dir / "chrome" / "target_id.txt"
            ).read_text()
            hook = (
                CHROME_UTILS.parent.parent
                / "chrome_screencast"
                / "on_Snapshot__02_chrome_screencast.daemon.bg.js"
            )
            for cast_env, name in ((env, "red"), (second_env, "blue")):
                cast = subprocess.Popen(
                    [str(hook), f"--url={httpserver.url_for('/' + name)}"],
                    cwd=cast_env["SNAP_DIR"],
                    env=cast_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                casts.append(cast)
                assert cast.stdout is not None
                assert cast.stdout.readline().strip() == "chrome screencast attached"
            frames = [
                Path(env["TMP_DIR"])
                / "chrome_screencast"
                / hashlib.sha256(cast_env["SNAP_DIR"].encode()).hexdigest()
                / "latest.jpg"
                for cast_env in (env, second_env)
            ]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not all(
                frame.is_file() for frame in frames
            ):
                time.sleep(0.05)
            assert all(frame.is_file() for frame in frames), (
                f"Missing isolated frames: {frames}"
            )
            pixels = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    r"""
const fs = require('fs');
const utils = require(process.argv[1]);
(async () => {
  const {browser, page} = await utils.connectToPage({chromeSessionDir: process.argv[2]});
  const samples = [];
  try {
    for(let sample=0; sample<5; sample++) {
      const pair=[];
      for(const file of process.argv.slice(3)) {
        pair.push(await page.evaluate(async data => {
          const bitmap=await createImageBitmap(new Blob([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],{type:'image/jpeg'}));
          const context=new OffscreenCanvas(1,1).getContext('2d');
          context.drawImage(bitmap,10,10,1,1,0,0,1,1); bitmap.close();
          return Array.from(context.getImageData(0,0,1,1).data);
        },fs.readFileSync(file).toString('base64')));
      }
      samples.push(pair);
      await new Promise(resolve=>setTimeout(resolve,600));
    }
    console.log(JSON.stringify(samples));
  } finally { browser.disconnect(); }
})().catch(error=>{console.error(error);process.exit(1);});
""",
                    str(CHROME_UTILS),
                    str(chrome_dir),
                    *(str(frame) for frame in frames),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert pixels.returncode == 0, pixels.stderr
            for red, blue in json.loads(pixels.stdout):
                assert red[0] > 240 and red[1] < 15 and red[2] < 15, red
                assert blue[2] > 240 and blue[0] < 15 and blue[1] < 15, blue
            before = frames[1].stat().st_mtime_ns
            casts[0].send_signal(signal.SIGTERM)
            stdout, stderr = casts[0].communicate(timeout=10)
            assert casts[0].returncode == 0, stdout + stderr
            assert not frames[0].exists()
            assert frames[1].is_file(), (
                "Stopping one capture deleted the other page's live frame"
            )
            deadline = time.monotonic() + 5
            while (
                time.monotonic() < deadline and frames[1].stat().st_mtime_ns == before
            ):
                time.sleep(0.05)
            assert frames[1].stat().st_mtime_ns > before
        finally:
            for cast in casts:
                if cast.poll() is None:
                    cast.send_signal(signal.SIGTERM)
                    cast.communicate(timeout=10)
            tab.send_signal(signal.SIGTERM)
            tab.wait(timeout=10)
        for cast_env in (env, second_env):
            kept = list(
                (Path(cast_env["SNAP_DIR"]) / "chrome_screencast").glob("frame-*.jpg"),
            )
            assert len(kept) == 1
            assert kept[0].read_bytes().startswith(b"\xff\xd8\xff")
