"""Source parsers consume one capture attempt instead of retrying its origin."""

import json
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)
from abx_plugins.plugins.parse_rss_urls.parse_rss_test_helpers import parse_rss_urls_env


PLUGINS = Path(__file__).resolve().parents[1] / "abx_plugins/plugins"


@pytest.mark.parametrize(
    ("plugin", "body", "content_type"),
    [
        (
            "parse_html_urls",
            '<html><body><a href="https://archivebox.io/tests/">Saved</a></body></html>',
            "text/html",
        ),
        (
            "parse_jsonl_urls",
            '{"url":"https://archivebox.io/tests/"}\n',
            "application/json",
        ),
        (
            "parse_netscape_urls",
            '<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><DT><A HREF="https://archivebox.io/tests/">Saved</A></DL>',
            "text/html",
        ),
        (
            "parse_rss_urls",
            '<rss version="2.0"><channel><title>Saved</title><item><link>https://archivebox.io/tests/</link></item></channel></rss>',
            "application/rss+xml",
        ),
    ],
)
def test_parser_does_not_retry_failed_capture(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    plugin,
    body,
    content_type,
):
    url = httpserver.url_for("/source")
    with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
        _,
        _,
        chrome,
        env,
    ):
        httpserver.stop()
        try:
            navigation = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                cwd=chrome,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
        finally:
            httpserver.start()
        assert navigation.returncode != 0, navigation.stdout
        recorded = json.loads((chrome / "navigation.json").read_text())
        assert "ERR_CONNECTION_REFUSED" in recorded["error"], recorded
        assert not recorded["status"], recorded
        assert recorded["url"] == url
        assert httpserver.url_for("/source") == url
        httpserver.expect_request("/source").respond_with_data(
            body,
            content_type=content_type,
        )
        httpserver.expect_request("/other").respond_with_data(
            body,
            content_type=content_type,
        )
        hook = next((PLUGINS / plugin).glob("on_Snapshot__*"))
        if plugin == "parse_rss_urls":
            env = parse_rss_urls_env(env)
        result = subprocess.run(
            [str(hook), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert not httpserver.log, (
            "Parser retried an origin after the capture had failed"
        )
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        assert not [row for row in records if row.get("type") == "Snapshot"]
        assert (
            next(row for row in records if row.get("type") == "ArchiveResult")["status"]
            == "noresults"
        )

        # A real imported source remains usable even when browser navigation failed.
        imported = chrome.parent / "staticfile/source.txt"
        imported.parent.mkdir(exist_ok=True)
        imported.write_text(body)
        saved = subprocess.run(
            [str(hook), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert saved.returncode == 0, saved.stderr
        records = [
            json.loads(line)
            for line in saved.stdout.splitlines()
            if line.startswith("{")
        ]
        assert [row["url"] for row in records if row.get("type") == "Snapshot"] == [
            "https://archivebox.io/tests/",
        ]
        assert not httpserver.log
        imported.unlink()

        # A previous capture of a different URL must not suppress standalone imports.
        other = subprocess.run(
            [str(hook), f"--url={httpserver.url_for('/other')}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert other.returncode == 0, other.stderr
        records = [
            json.loads(line)
            for line in other.stdout.splitlines()
            if line.startswith("{")
        ]
        assert [row["url"] for row in records if row.get("type") == "Snapshot"] == [
            "https://archivebox.io/tests/",
        ]
        assert [request.path for request, _ in httpserver.log] == ["/other"]

        # A new successful capture replaces the failure and can import the same URL.
        recovered = subprocess.run(
            [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
            cwd=chrome,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert recovered.returncode == 0, recovered.stderr
        assert json.loads((chrome / "navigation.json").read_text())["status"] == 200
        fresh = subprocess.run(
            [str(hook), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert fresh.returncode == 0, fresh.stderr
        records = [
            json.loads(line)
            for line in fresh.stdout.splitlines()
            if line.startswith("{")
        ]
        assert [row["url"] for row in records if row.get("type") == "Snapshot"] == [
            "https://archivebox.io/tests/",
        ]
