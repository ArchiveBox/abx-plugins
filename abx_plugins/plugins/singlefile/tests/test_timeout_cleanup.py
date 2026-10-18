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
