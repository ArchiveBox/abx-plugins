"""Archive network bytes before Chrome applies MIME-driven text decoding."""

import hashlib
import json
import subprocess
import threading
import time

import psutil

import pytest

from abx_plugins.plugins.base.testing import (
    start_process_and_wait_for_file,
    wait_for_file,
)
from abx_plugins.plugins.responses.tests.test_responses import (
    RESPONSES_HOOK,
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


@pytest.mark.parametrize(
    "content_type,count,megabytes",
    [
        ("text/plain", 1, 1),
        ("application/octet-stream", 1, 1),
        ("text/plain; charset=windows-1252", 1, 1),
        ("text/plain", 8, 4),
    ],
)
def test_response_bytes_survive_mime_decoding(
    tmp_path,
    httpserver,
    content_type,
    count,
    megabytes,
):
    # Every possible octet, including bytes that cannot round-trip through UTF-8
    # or Windows-1252. An opaque download need not have a filename extension.
    body = bytes(range(256)) * (4096 * megabytes)
    paths = [f"/payload/{i}" for i in range(count)]
    for path in paths:
        httpserver.expect_request(path).respond_with_data(
            body,
            content_type=content_type,
            headers={"Cache-Control": "max-age=3600"},
        )
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><title>Response bytes</title><script>"
        f"Promise.all({json.dumps(paths)}.map(p=>fetch(p).then(r=>r.arrayBuffer())))"
        ".then(bs=>document.body.dataset.bytes=bs.reduce((n,b)=>n+b.byteLength,0))"
        "</script><body>Original bytes</body>",
        content_type="text/html",
    )
    url = httpserver.url_for("/")
    payload_urls = [httpserver.url_for(path) for path in paths]
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
        stop = threading.Event()
        rss = []

        def sample_memory():
            while not stop.wait(0.02):
                try:
                    rss.append(psutil.Process(process.pid).memory_info().rss)
                except psutil.NoSuchProcess:
                    return

        sampler = threading.Thread(target=sample_memory)
        sampler.start()
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
                ready=lambda p: all(url in p.read_text() for url in payload_urls),
            )
        finally:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=30)
            finally:
                stop.set()
                sampler.join()
        elapsed = time.monotonic() - started
        print(
            f"{count * megabytes} MiB, capture {elapsed:.3f}s, peak hook RSS {max(rss) / 1024**2:.1f} MiB",
        )
        if count > 1:
            assert elapsed < 10
            assert max(rss) < 256 * 1024**2
        assert process.returncode == 0, (stdout, stderr)
        records = [json.loads(line) for line in index.read_text().splitlines()]
        for payload_url in payload_urls:
            payload = next(r for r in records if r["url"] == payload_url)
            assert (output / payload["path"]).read_bytes() == body
            assert payload["responseSha256"] == hashlib.sha256(body).hexdigest()


def test_open_stream_does_not_block_completed_responses_or_shutdown(tmp_path):
    from pytest_httpserver import HTTPServer
    from werkzeug.wrappers import Response

    started = threading.Event()
    release = threading.Event()

    def body_chunks():
        yield b"Unfinished response\n" * 4096
        started.set()
        release.wait(15)
        yield b"End of response\n"

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with HTTPServer(threaded=True) as server:
        server.expect_request("/slow").respond_with_response(
            Response(body_chunks(), content_type="text/plain"),
        )
        server.expect_request("/fast").respond_with_data(
            b"Completed response",
            content_type="text/plain",
        )
        server.expect_request("/").respond_with_data(
            "<!doctype html><title>Concurrent responses</title>"
            '<script>fetch("/slow");fetch("/fast");</script>',
            content_type="text/html",
        )
        url = server.url_for("/")
        try:
            with chrome_session(
                tmp_path,
                test_url=url,
                navigate=False,
                timeout=30,
                env_overrides={"TMP_DIR": str(scratch)},
            ) as (_, _, chrome_dir, env):
                output = chrome_dir.parent / "responses"
                output.mkdir()
                index = output / "index.jsonl"
                process = start_process_and_wait_for_file(
                    [str(RESPONSES_HOOK), f"--url={url}"],
                    index,
                    cwd=output,
                    env=env,
                )
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
                        ready=lambda p: server.url_for("/fast") in p.read_text(),
                    )
                    assert started.wait(2)
                    assert not release.is_set()
                    assert any(
                        p.stat().st_size > 0
                        for d in scratch.glob("*-responses-*")
                        for p in d.iterdir()
                    )
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                assert process.returncode == 0, (stdout, stderr)
                records = [json.loads(line) for line in index.read_text().splitlines()]
                fast = next(r for r in records if r["url"] == server.url_for("/fast"))
                assert (output / fast["path"]).read_bytes() == b"Completed response"
                assert not any(r["url"] == server.url_for("/slow") for r in records)
                assert not list(scratch.glob("*-responses-*"))
                assert not list(scratch.glob("response-bodies-*"))
        finally:
            release.set()
