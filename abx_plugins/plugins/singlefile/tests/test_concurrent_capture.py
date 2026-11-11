"""Native SingleFile tasks must not hold the profile lock while capturing."""

import json
import subprocess
from contextlib import contextmanager

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    launch_snapshot_tab,
    CHROME_NAVIGATE_HOOK,
)
from abx_plugins.plugins.singlefile.tests.test_singlefile import (
    CHROME_UTILS,
    SNAPSHOT_HOOK,
)


@contextmanager
def second_snapshot(tmp_path, env, url):
    chrome = tmp_path / "snap" / "second" / "chrome"
    chrome.mkdir(parents=True)
    tab_env = {**env, "SNAP_DIR": str(chrome.parent)}
    process = launch_snapshot_tab(
        snapshot_chrome_dir=chrome,
        tab_env=tab_env,
        test_url=url,
        snapshot_id="second",
        crawl_id="test-crawl",
        timeout=30,
    )
    try:
        result = subprocess.run(
            [str(CHROME_NAVIGATE_HOOK), f"--url={url}", "--snapshot-id=second"],
            cwd=chrome,
            env=tab_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        yield chrome
    finally:
        process.terminate()
        process.wait(timeout=10)
        for handle in (process._stdout_handle, process._stderr_handle):
            handle.close()


def test_delayed_captures_overlap_and_restore_profile(
    tmp_path,
    httpserver,
    singlefile_install_state,
    ensure_chrome_test_prereqs,
):
    for name in ("first", "second"):
        httpserver.expect_request(f"/{name}").respond_with_data(
            f"<!doctype html><title>{name}</title><h1>{name} capture</h1>",
            content_type="text/html",
        )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    state = singlefile_install_state
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/first"),
        timeout=30,
        env_overrides={
            "ABXPKG_CHROMEWEBSTORE_ROOT": str(state["extensions_dir"].parent),
            "CHROMEWEBSTORE_EXTENSIONS_DIR": str(state["extensions_dir"]),
        },
    ) as (_, _, chrome, env):
        output = chrome.parent / "singlefile"
        output.mkdir()
        script = f"""
const assert = require('node:assert/strict');
const cu = require({json.dumps(str(CHROME_UTILS))});
const {{spawn}} = require("node:child_process");
(async () => {{
 const {{browser,page,extensions}} = await cu.connectToPage({{
  chromeSessionDir:{json.dumps(str(chrome))}, requireTargetId:true,
  waitForNavigationComplete:true,
 }});
 try {{
  const extension = cu.findExtensionMetadataByName(extensions,'singlefile');
  extension.version = cu.loadExtensionManifest(extension.unpacked_path).version;
  const target = await cu.waitForExtensionTargetHandle(browser,extension.id,5000);
  await cu.loadExtensionFromTarget([extension],target);
  const observer = await browser.newPage();
  await observer.goto(`chrome-extension://${{extension.id}}/src/ui/pages/offscreen-document.html`);
  const original = await observer.evaluate(async url => {{
   const tab = (await chrome.tabs.query({{}})).find(tab=>tab.url===url);
   const [init] = await chrome.scripting.executeScript({{target:{{tabId:tab.id}},func:()=>chrome.runtime.sendMessage({{method:'bootstrap.init'}})}});
   const options = init.result.options;
   // SingleFile's real configurable delay holds both captures in progress.
   await chrome.runtime.sendMessage({{method:'config.updateProfile',profileName:options.profileName,profile:{{delayBeforeProcessing:8}}}});
   return {{profileName:options.profileName,profile:Object.fromEntries(
    ['removeHiddenElements','removeUnusedStyles','removeAlternativeMedias','removeAlternativeImages','removeUnusedFonts'].map(key=>[key,options[key]])
   )}};
  }},await page.url());
  let maxTasks = 0;
  let overlappingProfile = null;
  let finished = false;
  const observations = (async () => {{
   while (!finished) {{
    const state = await observer.evaluate(async url => {{
     const tasks = await chrome.runtime.sendMessage({{method:'downloads.getInfo'}});
     const tab = (await chrome.tabs.query({{}})).find(tab=>tab.url===url);
     const [init] = await chrome.scripting.executeScript({{target:{{tabId:tab.id}},func:()=>chrome.runtime.sendMessage({{method:'bootstrap.init'}})}});
     return {{tasks,options:init.result.options}};
    }},await page.url());
    maxTasks = Math.max(maxTasks,state.tasks.length);
    if(state.tasks.length===2) overlappingProfile=state.options;
    await new Promise(resolve=>setTimeout(resolve,50));
   }}
  }})();
  const capture = (snapshot,url) => new Promise((resolve,reject) => {{
   const child = spawn({json.dumps(str(SNAPSHOT_HOOK))}, ['--url='+url], {{
    cwd:snapshot,env:{{...process.env,SNAP_DIR:snapshot,SINGLEFILE_TIMEOUT:'40'}},
   }});
   let stdout='',stderr='';
   child.stdout.on('data',data=>{{stdout+=data;}});
   child.stderr.on('data',data=>{{stderr+=data;}});
   child.on('error',reject);
   child.on('exit',code=>{{
    if(code!==0) return reject(new Error(stdout+'\\n'+stderr));
    const record=stdout.split('\\n').filter(line=>line.startsWith('{{')).map(line=>JSON.parse(line)).find(record=>record.type==='ArchiveResult');
    if(record?.status!=='succeeded') return reject(new Error(stdout+'\\n'+stderr));
    resolve();
   }});
  }});
  try {{
   const results = await Promise.allSettled([
    capture({json.dumps(str(chrome.parent))},{json.dumps(httpserver.url_for("/first"))}),
    capture({json.dumps(str(tmp_path / "snap" / "second"))},{json.dumps(httpserver.url_for("/second"))}),
   ]);
   for(const result of results) assert.equal(result.status,'fulfilled',String(result.reason));
  }} finally {{ finished=true; await observations; }}
  assert.equal(maxTasks,2,'Both real native captures must overlap');
  for(const [key,value] of Object.entries(original.profile))
   assert.equal(overlappingProfile[key],value,`Persona preference ${{key}} must already be restored while captures run`);
  for(const name of ['first','second']) {{
   const saved = await browser.newPage();
   const snapshot = name==='first' ? {json.dumps(str(chrome.parent))} : {json.dumps(str(tmp_path / "snap" / "second"))};
   await saved.goto('file://'+require('path').join(snapshot,'singlefile','singlefile.html'));
   assert.equal(await saved.title(),name);
   assert.equal(await saved.$eval('h1',el=>el.textContent),name+' capture');
   await saved.close();
  }}
 }} finally {{await browser.disconnect();}}
}})().catch(error=>{{console.error(error);process.exitCode=1;}});
"""
        with second_snapshot(tmp_path, env, httpserver.url_for("/second")):
            result = subprocess.run(
                [env["NODE_BINARY"], "-e", script],
                cwd=output,
                env=env,
                capture_output=True,
                text=True,
                timeout=70,
            )
        assert result.returncode == 0, result.stdout + result.stderr
        for name, snapshot in (
            ("first", chrome.parent),
            ("second", tmp_path / "snap" / "second"),
        ):
            saved = (snapshot / "singlefile" / "singlefile.html").read_text()
            assert f"{name} capture" in saved
            assert "Page saved with SingleFile" in saved
