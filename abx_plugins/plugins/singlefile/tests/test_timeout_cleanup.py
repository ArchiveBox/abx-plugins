"""A real suspended helper must retain diagnostics at the hard deadline."""

import json
import subprocess
import os
import signal
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session
from abx_plugins.plugins.singlefile.tests.test_singlefile import (
    CHROME_UTILS,
    SINGLEFILE_HELPER,
    SNAPSHOT_HOOK,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_singlefile_outer_timeout_keeps_browser_diagnostics(
    tmp_path,
    httpserver,
    singlefile_install_state,
):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><h1>Busy browser capture</h1>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    url = httpserver.url_for("/")
    state = singlefile_install_state
    with chrome_session(
        tmp_path,
        test_url=url,
        navigate=True,
        timeout=30,
        env_overrides={
            "ABXPKG_CHROMEWEBSTORE_ROOT": str(state["extensions_dir"].parent),
            "CHROMEWEBSTORE_EXTENSIONS_DIR": str(state["extensions_dir"]),
        },
    ) as (_, _, chrome, env):
        output = chrome.parent / "singlefile"
        output.mkdir()
        script = f"""
const chromeUtils = require({json.dumps(str(CHROME_UTILS))});
(async () => {{
  const {{ browser, page, extensions }} = await chromeUtils.connectToPage({{
    chromeSessionDir: {json.dumps(str(chrome))}, requireTargetId: true,
    waitForNavigationComplete: true,
  }});
  try {{
    const extension = chromeUtils.findExtensionMetadataByName(extensions, 'singlefile');
    const configPage = await browser.newPage();
    await configPage.goto(`chrome-extension://${{extension.id}}/src/ui/pages/offscreen-document.html`);
    await configPage.evaluate(async (tabId) => {{
      const [result] = await chrome.scripting.executeScript({{
        target: {{tabId}}, func: () => chrome.runtime.sendMessage({{method:'bootstrap.init'}}),
      }});
      await chrome.runtime.sendMessage({{method:'config.updateProfile',
        profileName: result.result.options.profileName, profile:{{delayBeforeProcessing:30}}}});
    }}, (await configPage.evaluate(async (url) => (await chrome.tabs.query({{}})).find(tab=>tab.url===url).id, await page.url())));
    console.log(chromeUtils.resolveChromeLaunchOptions().CHROME_DOWNLOADS_DIR);
    await configPage.close();
  }} finally {{ await browser.disconnect(); }}
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
        busy = subprocess.run(
            [env["NODE_BINARY"], "-e", script],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert busy.returncode == 0, busy.stderr
        lock = Path(busy.stdout.strip()) / ".download.lock"
        with subprocess.Popen(
            [str(SNAPSHOT_HOOK), f"--url={url}"],
            cwd=output,
            env={**env, "SINGLEFILE_TIMEOUT": "10"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as hook:
            deadline = time.monotonic() + 8
            while (
                not lock.exists()
                and hook.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert lock.exists(), (
                "Real SingleFile helper did not acquire its download lock"
            )
            helper_pid = json.loads(lock.read_text())["pid"]
            # Suspend the actual helper only after it has connected to Chrome
            # and acquired its own lock. Its subprocess deadline must kill it.
            os.kill(helper_pid, signal.SIGSTOP)
            stdout, stderr = hook.communicate(timeout=15)
            result = subprocess.CompletedProcess(
                hook.args,
                hook.returncode,
                stdout,
                stderr,
            )
        with pytest.raises(ProcessLookupError):
            os.kill(helper_pid, 0)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Timed out after 10 seconds" in result.stderr
        assert "[singlefile] phase=connect" in result.stderr, result.stderr
        assert "[singlefile] phase=extension" in result.stderr, result.stderr
        assert not (output / "singlefile.html").exists()


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("failure", ["download-deadline", "protocol-timeout"])
def test_singlefile_timeout_cancels_exact_tab_and_keeps_other_capture(
    tmp_path,
    httpserver,
    singlefile_install_state,
    failure,
):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><h1>SingleFile cancellation preserves other tabs</h1>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    url = httpserver.url_for("/")
    state = singlefile_install_state
    with chrome_session(
        tmp_path,
        test_url=url,
        navigate=True,
        timeout=30,
        env_overrides={
            "ABXPKG_CHROMEWEBSTORE_ROOT": str(state["extensions_dir"].parent),
            "CHROMEWEBSTORE_EXTENSIONS_DIR": str(state["extensions_dir"]),
        },
    ) as (_, _, chrome, env):
        output = chrome.parent / "singlefile"
        output.mkdir()
        script = f"""
const cu=require({json.dumps(str(CHROME_UTILS))});
(async()=>{{
 const {{browser,page,extensions}}=await cu.connectToPage({{chromeSessionDir:{json.dumps(str(chrome))},requireTargetId:true,waitForNavigationComplete:true}});
 try {{
 const extension=cu.findExtensionMetadataByName(extensions,'singlefile');
 const helper=await browser.newPage();
 await helper.goto(`chrome-extension://${{extension.id}}/src/ui/pages/offscreen-document.html`);
 const url=await page.url();
 const mode=process.argv[1];
 const info=()=>helper.evaluate(()=>chrome.runtime.sendMessage({{method:'downloads.getInfo'}}));
 if(mode==='configure'||mode==='reset'){{
  await helper.evaluate(async({{url,delay}})=>{{
   const tab=(await chrome.tabs.query({{}})).find(tab=>tab.url===url);
   const [init]=await chrome.scripting.executeScript({{target:{{tabId:tab.id}},func:()=>chrome.runtime.sendMessage({{method:'bootstrap.init'}})}});
   await chrome.runtime.sendMessage({{method:'config.updateProfile',profileName:init.result.options.profileName,profile:{{delayBeforeProcessing:delay}}}});
  }},{{url,delay:mode==='configure'?30:0}});
 }}
 if(mode==='other'){{
  const deadline=Date.now()+5000;
  while(!(await info()).some(task=>task.url===url)){{
   if(Date.now()>deadline)throw new Error('SingleFile did not start the owned capture');
   await new Promise(resolve=>setTimeout(resolve,50));
  }}
  const owned=(await info()).find(task=>task.url===url);
  if(owned.id!==0)throw new Error(`Expected first real task id0, got ${{owned.id}}`);
  const other=await browser.newPage();await other.goto(url+'?other');
  await other.bringToFront();
  await other.triggerExtensionAction((await browser.extensions()).get(extension.id));
  while(!(await info()).some(task=>task.url===url+'?other')){{
   if(Date.now()>deadline)throw new Error('SingleFile did not start the other capture');
   await new Promise(resolve=>setTimeout(resolve,50));
  }}
 }}
 if(mode==='cleanup'){{
  await helper.evaluate(async(url)=>{{
   const tab=(await chrome.tabs.query({{}})).find(tab=>tab.url===url+'?other');
   await chrome.scripting.executeScript({{target:{{tabId:tab.id}},func:()=>chrome.runtime.sendMessage({{method:'downloads.cancel'}})}});
   await chrome.tabs.remove(tab.id);
  }},url);
 }}
 if(mode==='reset')await page.goto(url+'?next');
 console.log(JSON.stringify(await info()));await helper.close();
 }}finally{{await browser.disconnect()}}
}})().catch(e=>{{console.error(e);process.exit(1)}});
"""

        def control(mode):
            result = subprocess.run(
                [env["NODE_BINARY"], "-e", script, mode],
                cwd=output,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        assert control("configure") == []
        capture_command = [str(SNAPSHOT_HOOK), f"--url={url}"]
        if failure == "protocol-timeout":
            # Exercise a real CDP timeout before the extension's own deadline,
            # retaining the native delayed task and exact-tab cancellation.
            capture_script = f"""
const cu = require({json.dumps(str(CHROME_UTILS))});
const {{ saveSinglefileWithExtension }} = require({json.dumps(str(SINGLEFILE_HELPER))});
(async () => {{
 const {{browser,page,extensions}} = await cu.connectToPage({{
   chromeSessionDir:{json.dumps(str(chrome))}, requireTargetId:true,
   waitForNavigationComplete:true, timeoutMs:5000,
 }});
 try {{
   const extension = cu.findExtensionMetadataByName(extensions, 'singlefile');
   extension.version = cu.loadExtensionManifest(extension.unpacked_path).version;
   const target = await cu.waitForExtensionTargetHandle(browser, extension.id, 5000);
   await cu.loadExtensionFromTarget([extension], target);
   await saveSinglefileWithExtension(page, extension, {{timeoutMs:60000}});
 }} finally {{ await browser.disconnect(); }}
}})().catch(error => {{ console.error(error); process.exitCode=1; }});
"""
            capture_command = [env["NODE_BINARY"], "-e", capture_script]
        with subprocess.Popen(
            capture_command,
            cwd=output,
            env={**env, "SINGLEFILE_TIMEOUT": "20"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as hook:
            tasks = control("other")
            assert sorted(task["url"] for task in tasks) == [url, url + "?other"]
            stdout, stderr = hook.communicate(timeout=25)
        assert hook.returncode == 1, stdout + stderr
        expected_error = (
            "did not complete within"
            if failure == "download-deadline"
            else "Runtime.callFunctionOn timed out"
        )
        assert expected_error in stderr, stderr
        remaining = control("observe")
        assert [task["url"] for task in remaining] == [url + "?other"], remaining
        assert remaining[0]["status"] == "processing"
        assert not (output / "singlefile.html").exists()
        assert control("cleanup") == []
        assert control("reset") == []
        result = subprocess.run(
            [str(SNAPSHOT_HOOK), f"--url={url}?next"],
            cwd=output,
            env={**env, "SINGLEFILE_TIMEOUT": "20"},
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        saved = (output / "singlefile.html").read_text()
        assert "SingleFile cancellation preserves other tabs" in saved
        assert f"url: {url}?next" in saved
        assert control("observe") == []
