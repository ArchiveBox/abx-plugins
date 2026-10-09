"""Use the actual agent clients against a Chrome hook's live capture tab."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import pytest
from abxpkg import BinProvider

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGIN = Path(__file__).parents[1]


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_harness_and_stagehand_share_live_capture(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<title>Live persona</title>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/"),
        env_overrides={
            "BROWSER_LANGUAGE": "fr-FR",
            "BROWSER_TIMEZONE": "Europe/Paris",
        },
    ) as (_, _, chrome_dir, env):
        harness = install_required_binary_from_config(
            PLUGIN,
            "browser-harness",
            env=env,
        )
        stagehand = install_required_binary_from_config(PLUGIN, "stagehand", env=env)
        env = BinProvider.build_exec_env(
            providers=[harness.loaded_binprovider, stagehand.loaded_binprovider],
            base_env=env,
        )
        probe = subprocess.run(
            [
                env["NODE_BINARY"],
                str(PLUGIN / "browser.js"),
                str(chrome_dir),
                "snapshot",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert probe.returncode == 0, probe.stderr
        session = json.loads(probe.stdout)
        assert (
            session["target_id"] == (chrome_dir / "target_id.txt").read_text().strip()
        )
        env.update(
            {
                "BU_CDP_WS": session["cdp_url"],
                "BU_CDP_URL": session["cdp_url"],
                "BU_NAME": "abx-test-"
                + hashlib.sha256(session["cdp_url"].encode()).hexdigest()[:16],
                "BH_TAB_MARKER": "0",
                "BH_RUNTIME_DIR": tempfile.mkdtemp(prefix="abx-bh-"),
                "ARCHIVEBOX_BROWSER_TARGET_ID": session["target_id"],
                "ARCHIVEBOX_STAGEHAND_MODULE": str(stagehand.loaded_abspath),
            },
        )

        def run_harness(code):
            result = subprocess.run(
                [str(harness.loaded_abspath)],
                input=code,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        try:
            output = run_harness("""import os, json
switch_tab(os.environ['ARCHIVEBOX_BROWSER_TARGET_ID'])
print(json.dumps(js("window.agentHeap = 'still-live'; sessionStorage.setItem('agent','session'); localStorage.setItem('agent','local'); document.cookie = 'agent=session-cookie; SameSite=Lax'; ({language:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})")))
""")
            assert "fr-FR" in output and "Europe/Paris" in output
            task = tmp_path / "stagehand.mts"
            task.write_text("""const {StagehandClient} = await import(process.env.ARCHIVEBOX_STAGEHAND_MODULE);
const client = new StagehandClient({cdp_url: process.env.BU_CDP_WS, keep_alive:true, logging:{sentry_enabled:false}});
await client.connect();
try {
  const pages = await client.browser.pages({targetId:process.env.ARCHIVEBOX_BROWSER_TARGET_ID});
  if (pages.length !== 1) throw new Error('Expected the existing capture tab');
  const state = await pages[0].evaluate({expression: "({heap:window.agentHeap, session:sessionStorage.getItem('agent'), local:localStorage.getItem('agent'), cookie:document.cookie, language:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})"});
  console.log('STATE=' + JSON.stringify(state));
} finally { await client.close(); }
""")
            result = subprocess.run(
                ["tsx", str(task)],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            state = json.loads(
                next(
                    line.removeprefix("STATE=")
                    for line in result.stdout.splitlines()
                    if line.startswith("STATE=")
                ),
            )
            assert state == {
                "heap": "still-live",
                "session": "session",
                "local": "local",
                "cookie": "agent=session-cookie",
                "language": "fr-FR",
                "timezone": "Europe/Paris",
            }
            assert "still-live" in run_harness("print(js('window.agentHeap'))")
            # The official daemon shutdown command must also preserve this external browser.
            result = subprocess.run(
                [str(harness.loaded_abspath), "--reload"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr
            assert "still-live" in run_harness(
                "import os; switch_tab(os.environ['ARCHIVEBOX_BROWSER_TARGET_ID']); print(js('window.agentHeap'))",
            )
        finally:
            subprocess.run(
                [str(harness.loaded_abspath), "--reload"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
