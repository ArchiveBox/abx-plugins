"""Responses from an isolated cross-site frame keep their own CDP identity."""

import json
import subprocess
from urllib.parse import urlsplit, urlunsplit

import pytest

from abx_plugins.plugins.base.testing import start_process_and_wait_for_file
from abx_plugins.plugins.responses.tests.test_responses import (
    RESPONSES_HOOK,
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


@pytest.mark.parametrize(
    "mode",
    [
        "fetch",
        "sync-xhr",
        "redirect",
        "detached",
        "worker",
        "worker-closed",
        "blob-worker",
        "empty-workers",
        "transient",
    ],
)
def test_cross_site_child_response_preserves_original_bytes(tmp_path, httpserver, mode):
    page_url = httpserver.url_for("/main")
    parsed = urlsplit(page_url)
    child_host = "127.0.0.1" if parsed.hostname == "localhost" else "localhost"
    child_url = urlunsplit(
        (parsed.scheme, f"{child_host}:{parsed.port}", "/child", "", ""),
    )
    payload_url = urlunsplit(
        (parsed.scheme, f"{child_host}:{parsed.port}", "/payload", "", ""),
    )
    payload = bytes(range(256)) * 257
    transient = (
        "".join(
            f'<iframe src="{child_url}?transient={i}" onload="this.remove()"></iframe>'
            for i in range(20)
        )
        if mode == "transient"
        else ""
    )
    httpserver.expect_request("/main").respond_with_data(
        f'<!doctype html><title>Parent</title><script>addEventListener("message",()=>{{document.querySelector("iframe").remove();document.title="Child detached"}})</script><iframe src="{child_url}"></iframe>{transient}',
        content_type="text/html",
    )
    if mode == "sync-xhr":
        script = 'const r=new XMLHttpRequest();r.open("GET","/payload",false);r.send();document.title="Saved "+r.status'
    else:
        route = "/redirect" if mode == "redirect" else "/payload"
        script = f'fetch("{route}").then(r=>r.arrayBuffer()).then(b=>{{document.title="Saved "+b.byteLength}})'
    if mode == "detached":
        script += '.then(()=>parent.postMessage("done","*"))'
    if mode == "empty-workers":
        script += ';for(let i=0;i<20;i++){new Worker(URL.createObjectURL(new Blob(["close();"],{type:"application/javascript"})))}'
    worker_script = None
    if mode in ("worker", "worker-closed", "blob-worker"):
        worker_script = (
            'fetch("/worker-payload").then(r=>r.arrayBuffer()).then(b=>{postMessage(b.byteLength);'
            + ("close();" if mode == "worker-closed" else "")
            + "})"
        )
        if mode == "blob-worker":
            worker_script = worker_script.replace(
                '"/worker-payload"',
                json.dumps(payload_url.replace("/payload", "/worker-payload")),
            )
            worker_url = f'URL.createObjectURL(new Blob([{json.dumps(worker_script)}],{{type:"application/javascript"}}))'
        else:
            worker_url = '"/worker.js"'
        script += f';const worker=new Worker({worker_url});worker.onmessage=()=>{{document.title="Worker done"}}'
        httpserver.expect_request("/worker.js").respond_with_data(
            worker_script,
            content_type="application/javascript",
        )
        httpserver.expect_request("/worker-payload").respond_with_data(
            payload,
            content_type="application/octet-stream",
        )
    httpserver.expect_request("/child").respond_with_data(
        f"<!doctype html><title>Child</title><script>{script}</script>",
        content_type="text/html",
    )
    if mode == "redirect":
        httpserver.expect_request("/redirect").respond_with_data(
            b"",
            status=302,
            headers={"Location": "/payload"},
        )
    httpserver.expect_request("/payload").respond_with_data(
        payload,
        content_type="text/plain",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    with chrome_session(
        tmp_path,
        test_url=page_url,
        navigate=False,
        timeout=30,
        env_overrides={"CHROME_WAIT_FOR": "networkidle0"},
    ) as (_, _, chrome_dir, env):
        output = chrome_dir.parent / "responses"
        output.mkdir()
        index = output / "index.jsonl"
        process = start_process_and_wait_for_file(
            [str(RESPONSES_HOOK), f"--url={page_url}"],
            index,
            cwd=output,
            env=env,
        )
        try:
            nav = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK), f"--url={page_url}"],
                cwd=chrome_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert nav.returncode == 0, nav.stderr
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        if mode == "transient":
            final = [
                json.loads(line)
                for line in stdout.splitlines()
                if line.startswith('{"type":"ArchiveResult"')
            ]
            assert len(final) == 1, (stdout, stderr)
            assert final[0]["status"] in ("succeeded", "failed"), final
            assert process.returncode == (final[0]["status"] == "failed")
            assert (final[0]["status"] == "failed") == ("ERROR: Cannot " in stderr), (
                stdout,
                stderr,
            )
        else:
            assert process.returncode == 0, (stdout, stderr)
        rows = [json.loads(line) for line in index.read_text().splitlines()]
        saved = [row for row in rows if row["url"] == payload_url]
        assert saved, (rows, stderr)
        for row in saved:
            assert (output / row["path"]).read_bytes() == payload
        if mode != "transient":
            assert len(saved) == 1, (rows, stderr)
            assert sum(request.path == "/payload" for request, _ in httpserver.log) == 1
        if mode in ("worker", "worker-closed", "blob-worker"):
            assert worker_script is not None
            scripts = [row for row in rows if row["url"].endswith("/worker.js")]
            if mode == "blob-worker":
                assert not scripts
                assert not any(
                    request.path == "/worker.js" for request, _ in httpserver.log
                )
            else:
                assert len(scripts) == 1, (rows, stderr)
                assert (output / scripts[0]["path"]).read_text() == worker_script
            worker_rows = [
                row for row in rows if row["url"].endswith("/worker-payload")
            ]
            assert len(worker_rows) == 1, (rows, stderr)
            assert (output / worker_rows[0]["path"]).read_bytes() == payload
            assert (
                sum(request.path == "/worker-payload" for request, _ in httpserver.log)
                == 1
            )
