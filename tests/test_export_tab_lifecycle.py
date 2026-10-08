"""Owned export tabs follow real provider and snapshot process lifetimes."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    CHROME_TAB_HOOK,
    CHROME_UTILS,
    chrome_session,
    launch_snapshot_tab,
)

URL = "https://app.diagrams.net/#Uhttps%3A%2F%2Fraw.githubusercontent.com%2Fjgraph%2Fdrawio-diagrams%2Fmaster%2Fdiagrams%2Fschema.xml"
HOOK = (
    Path(__file__).resolve().parents[1]
    / "abx_plugins/plugins/drawio/on_Snapshot__53_drawio.js"
)


@pytest.mark.parametrize(
    "cleanup",
    [
        "provider-killed",
        "provider-terminated",
        "deadline",
        "snapshot-stopped",
        "snapshot-killed",
    ],
)
def test_provider_export_tab_lifecycle(tmp_path, ensure_chrome_test_prereqs, cleanup):
    with chrome_session(tmp_path, test_url="https://example.com", timeout=90) as (
        _,
        _,
        first_chrome,
        env,
    ):
        second_snapshot = tmp_path / "snap" / "second-snapshot"
        second_chrome = second_snapshot / "chrome"
        second_chrome.mkdir(parents=True)
        second_env = dict(
            env,
            SNAP_DIR=str(second_snapshot),
            DRAWIO_TIMEOUT="7200" if cleanup == "provider-killed" else "20",
        )
        tab = launch_snapshot_tab(
            snapshot_chrome_dir=second_chrome,
            tab_env=second_env,
            test_url=URL,
            snapshot_id="second-snapshot",
            crawl_id="test-crawl",
            timeout=90,
        )
        provider = None
        provider_log = None
        lease = None
        try:
            navigation = subprocess.run(
                [
                    str(CHROME_NAVIGATE_HOOK),
                    f"--url={URL}",
                    "--snapshot-id=second-snapshot",
                ],
                cwd=second_chrome,
                env=second_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert navigation.returncode == 0, navigation.stderr
            first_id = (first_chrome / "target_id.txt").read_text().strip()
            second_id = (second_chrome / "target_id.txt").read_text().strip()
            assert first_id != second_id
            assert (first_chrome / "cdp_url.txt").read_bytes() == (
                second_chrome / "cdp_url.txt"
            ).read_bytes()
            inspect = f"""
const {{connectToPage,getTargetIdFromTarget}} = require({json.dumps(str(CHROME_UTILS))});
(async () => {{
  const {{browser,page}} = await connectToPage({{chromeSessionDir:process.argv[1],timeoutMs:30000,waitForNavigationComplete:true}});
  try {{
    if (process.argv[2] === 'start') {{
      const unrelated = await browser.newPage();
      await unrelated.goto('https://example.com', {{waitUntil:'domcontentloaded',timeout:30000}});
    }}
    const targets = browser.targets().filter(target => target.type() === 'page').map(target => ({{id:getTargetIdFromTarget(target),url:target.url()}}));
    console.log(JSON.stringify({{url:page.url(),text:await page.evaluate(() => document.body.innerText),targets}}));
  }} finally {{ await browser.disconnect(); }}
}})().catch(error => {{console.error(error);process.exitCode=1}});
"""

            def inspect_browser(action):
                result = subprocess.run(
                    [env["NODE_BINARY"], "-e", inspect, str(first_chrome), action],
                    env=env,
                    cwd=first_chrome.parent,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert result.returncode == 0, result.stderr
                return json.loads(result.stdout)

            before = inspect_browser("start")
            assert "This domain is for use in documentation examples" in before["text"]
            assert (
                len(
                    [
                        target
                        for target in before["targets"]
                        if target["url"] == "https://example.com/"
                    ],
                )
                >= 2
            )
            provider_log = (tmp_path / "provider.log").open("w")
            provider = subprocess.Popen(
                [str(HOOK), f"--url={URL}"],
                cwd=second_snapshot,
                env=second_env,
                stdout=provider_log,
                stderr=provider_log,
                text=True,
            )
            observed_at = time.monotonic()
            lease_path = None
            lease = None
            while time.monotonic() - observed_at < 15:
                for candidate in (second_chrome / "export-tabs").glob("*.json"):
                    current = json.loads(candidate.read_text())
                    if current.get("targetId"):
                        lease_path, lease = candidate, current
                        break
                if lease is not None:
                    break
                assert provider.poll() is None, (tmp_path / "provider.log").read_text()
                time.sleep(0.02)
            assert lease is not None, (tmp_path / "provider.log").read_text()
            assert lease_path is not None
            owner_pid = lease["pid"]
            owner_command = subprocess.check_output(
                ["ps", "-p", str(owner_pid), "-o", "command="],
                text=True,
            )
            assert str(HOOK) in owner_command
            assert lease["ownerTargetId"] == second_id
            assert lease["targetId"] not in {first_id, second_id}
            lifetime = lease["expiresAt"] - lease["createdAt"]
            if cleanup == "provider-killed":
                # A real two-hour hook budget is capped at one hour. Kill the
                # owner immediately; this does not pretend an hour elapsed.
                assert lifetime == 60 * 60 * 1000
            else:
                assert 0 < lifetime <= 20_000
            # Suspend a genuine exporter to exercise a live but hung owner;
            # never replace its hooks, timers, or browser behavior.
            if cleanup in {"provider-killed", "provider-terminated"}:
                os.kill(
                    owner_pid,
                    signal.SIGKILL if cleanup == "provider-killed" else signal.SIGTERM,
                )
                provider.wait(timeout=5)
            else:
                os.kill(owner_pid, signal.SIGSTOP)
            if cleanup == "snapshot-stopped":
                tab.terminate()
                tab.wait(timeout=15)
            if cleanup == "snapshot-killed":
                # Kill the actual Node daemon, including when abxpkg supervises
                # it as a child. Killing only the wrapper would leave it alive.
                processes = {}
                for line in subprocess.check_output(
                    ["ps", "-axo", "pid=,ppid=,command="],
                    text=True,
                ).splitlines():
                    pid, parent, command = line.strip().split(None, 2)
                    processes[int(pid)] = (int(parent), command)
                owned = []
                for pid, (parent, command) in processes.items():
                    if str(CHROME_TAB_HOOK) not in command or not Path(
                        command.split()[0],
                    ).name.startswith("node"):
                        continue
                    ancestor = pid
                    while ancestor in processes and ancestor != tab.pid:
                        ancestor = processes[ancestor][0]
                    if ancestor == tab.pid:
                        owned.append(pid)
                assert len(owned) == 1, "Expected the actual owned snapshot Node daemon"
                os.kill(owned[0], signal.SIGKILL)
                tab.wait(timeout=15)
            deadline = time.monotonic() + (
                30 if cleanup in {"deadline", "snapshot-killed"} else 10
            )
            while lease_path.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not lease_path.exists(), (
                "Snapshot daemon did not remove the owned export lease"
            )
            if cleanup in {"deadline", "snapshot-killed"}:
                assert provider.poll() is None, (
                    "Deadline must clean up a still-live provider"
                )
                if cleanup == "deadline":
                    assert time.time() * 1000 >= lease["expiresAt"]
            after = inspect_browser("finish")
            ids = {target["id"] for target in after["targets"]}
            assert lease["targetId"] not in ids
            expected = {target["id"] for target in before["targets"]}
            if cleanup == "snapshot-stopped":
                expected.remove(second_id)
                assert second_id not in ids
            assert expected <= ids, (
                "Cleanup closed an unrelated or other snapshot's tab"
            )
            assert after["url"] == before["url"]
            assert after["text"] == before["text"]
        finally:
            if provider is not None and provider.poll() is None:
                if lease is not None:
                    try:
                        os.kill(lease["pid"], signal.SIGCONT)
                        os.kill(lease["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                provider.wait(timeout=5)
            if provider_log is not None:
                provider_log.close()
            if tab.poll() is None:
                tab.terminate()
                tab.wait(timeout=15)
            for attr in ("_stdout_handle", "_stderr_handle"):
                getattr(tab, attr).close()
