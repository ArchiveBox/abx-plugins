"""Unrelated acronym links must not become calendar requests blocked by CSP."""

import functools
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    fetch_devtools_targets,
)

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_calendar.js"


@pytest.mark.parametrize("explicit", [False, True])
def test_ics_advisory_is_not_calendar_but_explicit_feed_keeps_csp_failure(
    tmp_path,
    ensure_chrome_test_prereqs,
    explicit,
):
    # Actual non-calendar link from Wikipedia's List of software bugs capture
    # 06ac8d8ce9b073a98000d56581098396. The real browser enforces this CSP.
    origin = tmp_path / "origin"
    origin.mkdir()
    attribute = ' type="text/calendar"' if explicit else ""
    (origin / "index.html").write_text(
        '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'self'; connect-src 'self'\"></head><body>"
        f'<a href="https://www.us-cert.gov/ics/advisories/ICSMA-19-164-01"{attribute}>'
        "ICS Advisory (ICSMA-19-164-01)</a></body></html>",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(SimpleHTTPRequestHandler, directory=str(origin)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        with chrome_session(
            tmp_path,
            test_url=url,
            timeout=60,
            env_overrides={"AUTH_STORAGE_FILE": ""},
        ) as (_, _, chrome, env):
            endpoint = (chrome / "cdp_url.txt").read_text().strip()
            before = {
                t["id"]: t["url"]
                for t in fetch_devtools_targets(endpoint)
                if t["type"] == "page"
            }
            result = subprocess.run(
                [str(HOOK), f"--url={url}"],
                cwd=chrome.parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == (1 if explicit else 0), (
                result.stdout + result.stderr
            )
            record = parse_jsonl_output(result.stdout)
            assert record and record["status"] == (
                "failed" if explicit else "noresults"
            )
            if explicit:
                assert "CSP violation" in result.stderr
            else:
                assert "No calendar feed or ICS invitation" in result.stderr
                assert "CSP" not in result.stderr
            assert not (chrome.parent / "calendar").exists()
            assert {
                t["id"]: t["url"]
                for t in fetch_devtools_targets(endpoint)
                if t["type"] == "page"
            } == before
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
