"""Exercise source map capture through real Chrome and plugin hooks."""

import base64
import json
import subprocess
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    start_process_and_wait_for_file,
    wait_for_file,
    parse_jsonl_output,
)
from abx_plugins.plugins.responses.tests.test_responses import (
    RESPONSES_HOOK,
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__92_sourcemaps.py"


@pytest.mark.parametrize("slow", [False, True])
def test_captures_external_and_inline_source_maps(tmp_path, httpserver, slow):
    external = json.dumps(
        {"version": 3, "sources": ["src/hello.ts"], "names": [], "mappings": "AAAA"},
    ).encode()
    inline = json.dumps(
        {"version": 3, "sources": ["src/inline.ts"], "names": [], "mappings": "AAAA"},
    ).encode()
    data = base64.b64encode(inline).decode()
    httpserver.expect_request("/").respond_with_data(
        '<!doctype html><title>Source maps</title><script src="/bundle.js"></script><script src="/inline.js"></script><script src="/header.js"></script><script src="/legacy.js"></script>'
        + ('<script src="/slow.js"></script>' if slow else ""),
        content_type="text/html",
    )
    httpserver.expect_request("/bundle.js").respond_with_data(
        'console.log("external");\n//# sourceMappingURL=bundle.js.map',
        content_type="application/javascript",
    )
    httpserver.expect_request("/inline.js").respond_with_data(
        f'console.log("inline");\n//# sourceMappingURL=data:application/json;base64,{data}',
        content_type="application/javascript",
    )
    httpserver.expect_request("/header.js").respond_with_data(
        'console.log("header");',
        content_type="application/javascript",
        headers={"SourceMap": "/header.map"},
    )
    httpserver.expect_request("/header.map").respond_with_data(
        external,
        content_type="application/json",
    )
    httpserver.expect_request("/legacy.js").respond_with_data(
        'console.log("legacy");',
        content_type="application/javascript",
        headers={"X-SourceMap": "/legacy.map"},
    )
    httpserver.expect_request("/legacy.map").respond_with_data(
        inline,
        content_type="application/json",
    )
    httpserver.expect_request("/bundle.js.map").respond_with_data(
        external,
        content_type="application/json",
    )
    if slow:
        from werkzeug.wrappers import Response

        httpserver.expect_request("/slow.js").respond_with_data(
            'console.log("slow");\n//# sourceMappingURL=slow.map',
            content_type="application/javascript",
        )

        def slow_map(request):
            time.sleep(6)
            return Response(external, content_type="application/json")

        httpserver.expect_request("/slow.map").respond_with_handler(slow_map)
    url = httpserver.url_for("/")
    script_urls = [
        httpserver.url_for(path)
        for path in ("/bundle.js", "/inline.js", "/header.js", "/legacy.js")
    ]
    if slow:
        script_urls.append(httpserver.url_for("/slow.js"))
    with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        destination = chrome_dir.parent
        response_dir = destination / "responses"
        response_dir.mkdir()
        index = response_dir / "index.jsonl"
        process = start_process_and_wait_for_file(
            [str(RESPONSES_HOOK), f"--url={url}"],
            index,
            cwd=response_dir,
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
                ready=lambda path: all(url in path.read_text() for url in script_urls),
            )
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
        output = destination / "sourcemaps"
        output.mkdir(exist_ok=True)
        result = subprocess.run(
            [str(HOOK), f"--url={url}", "--snapshot-id=test", "--depth=0"],
            cwd=output,
            env={**env, **({"SOURCEMAPS_TIMEOUT": "5"} if slow else {})},
            capture_output=True,
            text=True,
            timeout=15,
        )
    if slow:
        assert result.returncode != 0
        record = parse_jsonl_output(result.stdout)
        assert record is not None
        assert record["status"] == "failed"
        assert "time budget" in record["output_str"]
        return
    assert result.returncode == 0, result.stdout + result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record is not None
    assert record["status"] == "succeeded"
    records = json.loads((destination / "sourcemaps" / "index.json").read_text())
    assert len(records) == 4
    maps = {
        record["script_url"]: (destination / "sourcemaps" / record["path"]).read_bytes()
        for record in records
    }
    assert maps[httpserver.url_for("/bundle.js")] == external
    assert maps[httpserver.url_for("/inline.js")] == inline
    assert maps[httpserver.url_for("/header.js")] == external
    assert maps[httpserver.url_for("/legacy.js")] == inline
