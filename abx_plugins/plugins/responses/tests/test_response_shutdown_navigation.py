"""Response shutdown must not depend on a page's retiring JavaScript realm."""

import hashlib
import json
import subprocess

import pytest

from abx_plugins.plugins.base.testing import wait_for_file
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import CHROME_UTILS
from abx_plugins.plugins.responses.tests.test_responses import (
    CHROME_NAVIGATE_HOOK,
    RESPONSES_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def test_response_shutdown_during_navigation_preserves_completed_bytes(
    tmp_path,
    httpserver,
):
    body = bytes(range(256)) * 4096
    httpserver.expect_request("/payload").respond_with_data(
        body,
        content_type="application/octet-stream",
    )
    httpserver.expect_request("/").respond_with_data(
        '<!doctype html><title>Before navigation</title><script>fetch("/payload").then(r=>r.arrayBuffer())</script>',
        content_type="text/html",
    )
    httpserver.expect_request("/next").respond_with_data(
        "<!doctype html><title>After navigation</title>",
        content_type="text/html",
    )
    url = httpserver.url_for("/")
    with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        output = chrome_dir.parent / "responses"
        output.mkdir()
        index = output / "index.jsonl"
        stdout_path = tmp_path / "responses.stdout.log"
        stderr_path = tmp_path / "responses.stderr.log"
        paused = tmp_path / "paused"
        script = f"""
const cu = require({json.dumps(str(CHROME_UTILS))});
(async () => {{
 const {{browser,page}} = await cu.connectToPage({{chromeSessionDir:{json.dumps(str(chrome_dir))},requireTargetId:true}});
 const session = await page.createCDPSession();
 try {{
  await session.send('Runtime.enable');
  await session.send('Debugger.enable');
  const paused = new Promise(resolve => session.once('Debugger.paused',resolve));
  const evaluation = page.evaluate(() => {{debugger;}});
  // Navigation intentionally abandons this paused evaluation. Its expected
  // rejection is independent of the real recorder result asserted below.
  evaluation.catch(() => {{}});
  await paused;
  require('node:fs').writeFileSync({json.dumps(str(paused))},'paused');
  await new Promise(resolve => process.stdin.once('data',resolve));
  const cleared = new Promise(resolve => session.once('Runtime.executionContextsCleared',resolve));
  await session.send('Page.navigate', {{url:{json.dumps(httpserver.url_for("/next"))}}});
  await cleared;
  await session.send('Debugger.disable');
 }} finally {{ await browser.disconnect(); }}
}})().catch(error => {{console.error(error);process.exitCode=1;}});
"""
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            process = subprocess.Popen(
                [str(RESPONSES_HOOK), f"--url={url}"],
                cwd=output,
                env={**env, "NODE_DEBUG": "puppeteer:protocol:SEND*"},
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            control = None
            try:
                wait_for_file(index, process=process)
                navigation = subprocess.run(
                    [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                    cwd=chrome_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert navigation.returncode == 0, navigation.stderr
                wait_for_file(
                    index,
                    process=process,
                    ready=lambda p: httpserver.url_for("/payload") in p.read_text(),
                )
                control = subprocess.Popen(
                    [env["NODE_BINARY"], "-e", script],
                    cwd=output,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                wait_for_file(paused, process=control)
                process.terminate()
                # Observe the real shutdown round trip before destroying the
                # paused realm. Protocol output is streamed to disk, not a pipe.
                wait_for_file(
                    stderr_path,
                    process=process,
                    ready=lambda p: (
                        any(
                            f'"method":"{method}"'
                            in p.read_text().split("Received SIGTERM", 1)[-1]
                            for method in (
                                "Runtime.callFunctionOn",
                                "Performance.getMetrics",
                            )
                        )
                        and "Received SIGTERM" in p.read_text()
                    ),
                )
                control_stdout, control_stderr = control.communicate(
                    "navigate\n",
                    timeout=15,
                )
                assert control.returncode == 0, (control_stdout, control_stderr)
                process.wait(timeout=15)
            finally:
                if control is not None and control.poll() is None:
                    control.kill()
                    control.communicate(timeout=10)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        stdout, stderr = stdout_path.read_text(), stderr_path.read_text()
        assert process.returncode == 0, (stdout, stderr[-12000:])
        results = [
            json.loads(line) for line in stdout.splitlines() if line.startswith("{")
        ]
        results = [
            result for result in results if result.get("type") == "ArchiveResult"
        ]
        assert len(results) == 1, stdout
        assert results[0]["status"] == "succeeded", stdout
        assert "Response event flush failed" not in stderr
        records = [json.loads(line) for line in index.read_text().splitlines()]
        payload = next(
            record
            for record in records
            if record["url"] == httpserver.url_for("/payload")
        )
        assert (output / payload["path"]).read_bytes() == body
        assert payload["responseSha256"] == hashlib.sha256(body).hexdigest()
