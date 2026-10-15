"""Timed-out attachments must release connections that finish after recovery."""

import json
import os
import signal
import subprocess
import threading

import psutil
import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    PLUGINS_ROOT,
    chrome_session,
    fetch_devtools_targets,
)


@pytest.mark.parametrize("operation", ["calendar", "probe", "inspect"])
def test_late_browser_connection_is_released(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    operation,
):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><h1>ArchiveBox page without calendar links</h1>",
        content_type="text/html",
    )
    url = httpserver.url_for("/")
    with chrome_session(tmp_path, test_url=url, timeout=45) as (_, pid, chrome, env):
        endpoint = (chrome / "cdp_url.txt").read_text().strip()
        before = fetch_devtools_targets(endpoint)
        targets = {t["id"]: t["url"] for t in before if t["type"] == "page"}
        if operation == "calendar":
            command = [
                str(PLUGINS_ROOT / "calendar/on_Snapshot__53_calendar.js"),
                f"--url={url}",
            ]
        else:
            command = [
                env["NODE_BINARY"],
                "-e",
                """
const utils = require(process.argv[1]);
const operation = process.argv[2];
const target = process.argv[3];
(async () => {
  const result = await utils.inspectChromeSessionArtifacts(target, {
    requireTargetId: operation === 'inspect', probeTimeoutMs: 2000,
    processIsLocal: false,
  });
  console.log(JSON.stringify(result));
})().catch(error => { console.error(error); process.exitCode = 1; });
""",
                str(CHROME_UTILS),
                operation,
                str(chrome),
            ]

        browser = psutil.Process(pid)
        # Pause only this test's real browser past the attachment deadline, then
        # let it recover. Late successful connections used to keep Node alive.
        browser.send_signal(signal.SIGSTOP)
        resume = threading.Timer(4, lambda: browser.send_signal(signal.SIGCONT))
        resume.start()
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=chrome.parent,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stdout + stderr
            if operation == "calendar":
                result = parse_jsonl_output(stdout)
                assert result is not None, stdout
                assert result["status"] == "noresults", stdout
                assert result["output_str"] == "No calendar feed or ICS invitation"
                assert not (chrome.parent / "calendar").exists()
            else:
                result = json.loads(stdout)
                assert result["stale"] is True, result
                expected = (
                    "cdp unreachable"
                    if operation == "probe"
                    else "Timed out connecting to browser"
                )
                assert expected in result["reason"]
            assert browser.is_running()
            assert {
                t["id"]: t["url"]
                for t in fetch_devtools_targets(endpoint)
                if t["type"] == "page"
            } == targets
        finally:
            browser.send_signal(signal.SIGCONT)
            resume.cancel()
            resume.join()
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
