"""CacheStorage bodies must survive beside the durable network collector."""

import base64
import json
import subprocess
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from abx_plugins.plugins.base.testing import start_process_and_wait_for_file
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import CHROME_UTILS
from abx_plugins.plugins.responses.tests.test_responses import (
    CHROME_NAVIGATE_HOOK,
    RESPONSES_HOOK,
    chrome_session,
)


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize(
    "cross_site_frame,mislabeled_binary,streamed_worker",
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, False),
        (False, True, True),
        (True, True, True),
    ],
)
def test_service_worker_cached_bodies_preserve_bytes_without_refetching(
    tmp_path,
    mislabeled_binary,
    cross_site_frame,
    streamed_worker,
):
    image = (
        Path(__file__).parents[4] / "docs/assets/plugins/singlefile.png"
    ).read_bytes()
    assets = {
        "/asset.css": (b"body{color:rgb(1,2,3)}\n", "text/css"),
        "/image.png": (image, "image/png"),
    }
    if mislabeled_binary:
        assets["/binary.txt"] = (image, "text/plain")
    with HTTPServer(threaded=True) as server:
        origin = server.url_for("/")
        child_origin = origin.replace("localhost", "127.0.0.1")
        assert origin != child_origin
        document = "<!doctype html><title>Service worker response capture</title>"
        server.expect_request("/").respond_with_data(
            document
            + (
                f'<iframe src="{child_origin}child"></iframe>'
                if cross_site_frame
                else ""
            ),
            content_type="text/html",
        )
        server.expect_request("/child").respond_with_data(
            document,
            content_type="text/html",
        )
        server.expect_request("/favicon.ico").respond_with_data(b"", status=204)
        for url, (body, mime) in assets.items():
            server.expect_request(url).respond_with_data(body, content_type=mime)
        server.expect_request("/sw.js").respond_with_data(
            "self.addEventListener('install',event=>event.waitUntil("
            "caches.open('assets').then(cache=>cache.addAll("
            + json.dumps(list(assets))
            + ")).then(()=>self.skipWaiting())));"
            "self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));"
            "self.addEventListener('fetch',event=>event.respondWith("
            "caches.match(event.request).then(async cached=>{"
            "if(!cached)return fetch(event.request);"
            + (
                # A real worker can stream cached content instead of returning
                # its Response directly. Exercise original-byte delivery even
                # when a text MIME type would corrupt getResponseBody fallback.
                "if(new URL(event.request.url).pathname==='/binary.txt'){"
                "const bytes=new Uint8Array(await cached.arrayBuffer());"
                "return new Response(new ReadableStream({async start(controller){"
                "controller.enqueue(bytes.slice(0,1));"
                "await new Promise(resolve=>setTimeout(resolve,100));"
                "controller.enqueue(bytes.slice(1));controller.close();"
                "}}),{headers:cached.headers});}"
                if streamed_worker
                else ""
            )
            + "return cached;})));",
            content_type="application/javascript",
        )
        url = server.url_for("/")
        with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
            _,
            _,
            chrome,
            env,
        ):
            output = chrome.parent / "responses"
            output.mkdir()
            recorder = start_process_and_wait_for_file(
                [str(RESPONSES_HOOK), f"--url={url}"],
                output / "index.jsonl",
                cwd=output,
                env=env,
            )
            try:
                navigation = subprocess.run(
                    [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                    cwd=chrome,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert navigation.returncode == 0, navigation.stderr
                # These are actual application CacheStorage responses, not a
                # second network fetch. Disabling HTTP cache cannot replace
                # them, and bypassing the worker would change the page's data.
                fetched = subprocess.run(
                    [
                        env["NODE_BINARY"],
                        "-e",
                        """
const {connectToPage}=require(process.argv[1]);
(async()=>{
 const {browser,page}=await connectToPage({chromeSessionDir:process.argv[2]});
 try {
  const target=process.argv[4]==='true'?page.frames().find(frame=>frame.url().endsWith('/child')):page;
  if(!target)throw new Error('Child frame did not load');
  await target.evaluate(async()=>{
   await navigator.serviceWorker.register('/sw.js');
   await navigator.serviceWorker.ready;
   if(!navigator.serviceWorker.controller) await new Promise(resolve=>navigator.serviceWorker.addEventListener('controllerchange',resolve,{once:true}));
  });
  const bodies=await target.evaluate(async urls=>{
   const output={};
   for(const url of urls){
    const bytes=new Uint8Array(await(await fetch(url)).arrayBuffer());
    output[url]=btoa(String.fromCharCode(...bytes));
   }
   return output;
  },JSON.parse(process.argv[3]));
  console.log(JSON.stringify(bodies));
 } finally {browser.disconnect();}
})().catch(error=>{console.error(error);process.exitCode=1});
""",
                        str(CHROME_UTILS),
                        str(chrome),
                        json.dumps(list(assets)),
                        str(cross_site_frame).lower(),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert fetched.returncode == 0, fetched.stderr
                assert {
                    key: base64.b64decode(value)
                    for key, value in json.loads(fetched.stdout).items()
                } == {key: value[0] for key, value in assets.items()}
            finally:
                recorder.terminate()
                stdout, stderr = recorder.communicate(timeout=30)
                (tmp_path / "responses.stderr.log").write_text(stderr)
            rows = [
                json.loads(line)
                for line in (output / "index.jsonl").read_text().splitlines()
            ]
            binary_url = (child_origin if cross_site_frame else origin) + "binary.txt"
            binary_saved = [row for row in rows if row["url"] == binary_url]
            # Chrome may expose original streamed bytes even for a text MIME
            # type; only its decoded fallback loses binary fidelity. Require
            # exact saved bytes plus success, or the specific integrity failure
            # plus no corrupt artifact, rather than requiring a backend to fail.
            unavailable_binary = mislabeled_binary and not binary_saved
            if streamed_worker:
                assert binary_saved, (stdout, stderr)
            results = [
                json.loads(line) for line in stdout.splitlines() if line.startswith("{")
            ]
            assert len(results) == 1, results
            assert results[0]["type"] == "ArchiveResult"
            if unavailable_binary:
                assert recorder.returncode == 1, (stdout, stderr)
                assert "Original response bytes unavailable" in stderr
                assert "1 response capture error(s)" in stdout
                assert results[0]["status"] == "failed"
            else:
                assert recorder.returncode == 0, (stdout, stderr)
                assert results[0]["status"] == "succeeded"
                assert "response capture error" not in stdout
                assert "Original response bytes unavailable" not in stderr
            for asset, (expected, _) in assets.items():
                expected_url = (
                    child_origin if cross_site_frame else origin
                ) + asset.lstrip("/")
                saved = [row for row in rows if row["url"] == expected_url]
                if asset == "/binary.txt" and unavailable_binary:
                    assert not saved
                    assert sum(request.path == asset for request, _ in server.log) == 1
                    continue
                assert len(saved) == 1, (asset, rows)
                assert (output / saved[0]["path"]).read_bytes() == expected
                assert sum(request.path == asset for request, _ in server.log) == 1
