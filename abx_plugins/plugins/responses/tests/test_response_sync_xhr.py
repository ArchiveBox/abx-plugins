"""Synchronous page requests must not deadlock browser response capture."""

import json
import subprocess
import time

import pytest

from abx_plugins.plugins.base.testing import (
    start_process_and_wait_for_file,
    wait_for_file,
)
from abx_plugins.plugins.responses.tests.test_responses import (
    CHROME_NAVIGATE_HOOK,
    RESPONSES_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"Synchronous response preserved byte for byte.", "text/plain"),
        (bytes(range(256)) * 4096, "text/plain; charset=windows-1252"),
    ],
    ids=["text", "binary-windows-1252"],
)
def test_synchronous_xhr_preserves_response_without_blocking_navigation(
    tmp_path,
    httpserver,
    body,
    content_type,
):
    httpserver.expect_request("/payload").respond_with_data(
        body,
        content_type=content_type,
        headers={"Cache-Control": "max-age=3600"},
    )
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><title>Sync request</title><script>"
        "for(let i=0;i<2;i++){const xhr = new XMLHttpRequest();"
        "xhr.open('GET','/payload',false);xhr.send();"
        "document.title='Completed '+xhr.responseText.length;}</script><body>Completed</body>",
        content_type="text/html",
    )
    url = httpserver.url_for("/")
    payload_url = httpserver.url_for("/payload")
    with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        output = chrome_dir.parent / "responses"
        output.mkdir()
        index = output / "index.jsonl"
        process = start_process_and_wait_for_file(
            [str(RESPONSES_HOOK), f"--url={url}"],
            index,
            cwd=output,
            env=env,
        )
        started = time.monotonic()
        try:
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
                ready=lambda p: p.read_text().count(payload_url) == 2,
            )
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
        assert time.monotonic() - started < 10
        records = [json.loads(line) for line in index.read_text().splitlines()]
        payloads = [r for r in records if r["url"] == payload_url]
        assert len(payloads) == 2
        for payload in payloads:
            assert (output / payload["path"]).read_bytes() == body
        assert sum(request.path == "/payload" for request, _ in httpserver.log) == 1
