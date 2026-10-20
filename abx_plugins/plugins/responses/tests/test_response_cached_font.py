"""Fonts reused from Chrome's memory cache still need original response bytes."""

import json
import subprocess
from pathlib import Path

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
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "scenario",
    ["duplicate", "changed-variant", "language-vary", "late-observer"],
)
def test_cached_font_preserves_exact_identity_without_refetching(
    tmp_path,
    httpserver,
    scenario,
):
    original = (FIXTURES / "source-sans-pro.woff").read_bytes()
    changed = (FIXTURES / "forkawesome.woff2").read_bytes()
    first = httpserver.url_for("/first")
    second = httpserver.url_for("/second")
    vary = "Accept-Language" if scenario == "language-vary" else "Referer"
    for url, body in [(first, original), (second, changed)]:
        httpserver.expect_request(
            "/font.woff",
            headers={"Referer": url},
        ).respond_with_data(
            body,
            content_type="font/woff",
            headers={
                "Cache-Control": "public, max-age=3600",
                "Vary": vary,
                "ETag": str(len(body)),
            },
        )
    for path in ["/first", "/second"]:
        httpserver.expect_request(path).respond_with_data(
            "<!doctype html><title>Cached font</title><style>@font-face{font-family:Saved;src:url(/font.woff)}body{font-family:Saved}</style><body>Original font bytes</body>",
            content_type="text/html",
        )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    urls = [first, second, first] if scenario == "changed-variant" else [first, first]
    with chrome_session(
        tmp_path,
        test_url=first,
        navigate=scenario == "late-observer",
        timeout=30,
        env_overrides={"CHROME_WAIT_FOR": "load"},
    ) as (_, _, chrome_dir, env):
        output = chrome_dir.parent / "responses"
        output.mkdir()
        index = output / "index.jsonl"
        process = start_process_and_wait_for_file(
            [str(RESPONSES_HOOK), f"--url={first}"],
            index,
            cwd=output,
            env=env,
        )
        try:
            for count, url in enumerate(
                urls if scenario != "late-observer" else [first],
                1,
            ):
                nav = subprocess.run(
                    [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                    cwd=chrome_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert nav.returncode == 0, nav.stderr
                wait_for_file(
                    index,
                    process=process,
                    ready=lambda p: (
                        sum(
                            json.loads(line)["url"] in (first, second)
                            for line in p.read_text().splitlines()
                        )
                        == count
                    ),
                )
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        rows = [json.loads(line) for line in index.read_text().splitlines()]
        fonts = [r for r in rows if r["url"] == httpserver.url_for("/font.woff")]
        # Chrome's font memory cache can reuse the first variant even for a
        # different Referer. Never attribute its old bytes to that new Vary key.
        if scenario in ("changed-variant", "late-observer"):
            assert process.returncode == 1, (stdout, stderr)
            assert "No data found for resource" in stderr
            expected = [] if scenario == "late-observer" else [original, original]
        else:
            assert process.returncode == 0, (stdout, stderr)
            expected = [original, original]
        assert [(output / font["path"]).read_bytes() for font in fonts] == expected
        assert sum(r.path == "/font.woff" for r, _ in httpserver.log) == 1
