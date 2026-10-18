"""Native filesystem backpressure must not stall intercepted browser traffic."""

import functools
import json
import os
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from abx_plugins.plugins.base.testing import (
    start_process_and_wait_for_file,
    wait_for_file,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    chrome_session,
)
from abx_plugins.plugins.responses.tests.test_responses import RESPONSES_HOOK


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_pending_index_write_does_not_block_browser_responses(tmp_path):
    webroot = tmp_path / "web"
    webroot.mkdir()
    bodies = {
        name: f"<!doctype html><title>{name}</title><p>{name}</p>".encode()
        for name in ("first", "second")
    }
    for name, body in bodies.items():
        (webroot / f"{name}.html").write_bytes(body)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(SimpleHTTPRequestHandler, directory=str(webroot)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with chrome_session(
            tmp_path,
            test_url=f"{base_url}/first.html",
            navigate=False,
            timeout=30,
        ) as (_, _, chrome_dir, env):
            output = chrome_dir.parent / "responses"
            output.mkdir()
            index = output / "index.jsonl"
            process = start_process_and_wait_for_file(
                [str(RESPONSES_HOOK), f"--url={base_url}/first.html"],
                index,
                cwd=output,
                env=env,
            )
            index.unlink()
            os.mkfifo(index)
            script = """
const u = require(process.argv[1]);
(async () => {
  const { browser, page } = await u.connectToPage({chromeSessionDir: process.argv[2], requireTargetId: true});
  try {
    const response = await page.goto(process.argv[3], {waitUntil: 'load', timeout: 5000});
    console.log(JSON.stringify({status: response.status(), title: await page.title()}));
  } finally { await browser.disconnect(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
            try:
                first = subprocess.run(
                    [
                        env["NODE_BINARY"],
                        "-e",
                        script,
                        str(CHROME_UTILS),
                        str(chrome_dir),
                        f"{base_url}/first.html",
                    ],
                    cwd=output,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert first.returncode == 0, first.stderr
                # The body and both aliases precede the real FIFO append. Its
                # writer cannot open until the reader below exists.
                wait_for_file(output / "127.0.0.1/first.html", process=process)
                second = subprocess.run(
                    [
                        env["NODE_BINARY"],
                        "-e",
                        script,
                        str(CHROME_UTILS),
                        str(chrome_dir),
                        f"{base_url}/second.html",
                    ],
                    cwd=output,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert second.returncode == 0, second.stderr
                assert json.loads(second.stdout) == {"status": 200, "title": "second"}
            finally:
                # Release actual kernel backpressure before draining shutdown.
                reader = os.open(index, os.O_RDWR | os.O_NONBLOCK)
                try:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=30)
                    records = [
                        json.loads(line) for line in os.read(reader, 65536).splitlines()
                    ]
                finally:
                    os.close(reader)
            assert process.returncode == 0, (stdout, stderr)
            for name, body in bodies.items():
                record = next(
                    row for row in records if row["url"] == f"{base_url}/{name}.html"
                )
                assert (output / record["path"]).read_bytes() == body
                assert (output / f"127.0.0.1/{name}.html").read_bytes() == body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
