"""Exercise calendar parsing and target isolation through the real Chrome hooks."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
    fetch_devtools_targets,
)

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_calendar.js"


def test_failed_navigation_remains_failure(tmp_path, ensure_chrome_test_prereqs):
    # Stop a dedicated real server, leaving the shared HTTP fixture untouched.
    with HTTPServer(host="127.0.0.1") as unavailable:
        url = unavailable.url_for("/unavailable")
    with chrome_session(
        tmp_path,
        test_url=url,
        navigate=False,
        timeout=60,
        env_overrides={"AUTH_STORAGE_FILE": ""},
    ) as (_, _, chrome, env):
        navigation = subprocess.run(
            [str(CHROME_NAVIGATE_HOOK), f"--url={url}", "--snapshot-id=test-snapshot"],
            cwd=chrome,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert navigation.returncode != 0, navigation.stdout + navigation.stderr
        state = json.loads((chrome / "navigation.json").read_text())
        assert "ERR_CONNECTION_REFUSED" in state["error"], state
        endpoint = (chrome / "cdp_url.txt").read_text().strip()
        # goto rejects before Chrome commits its error document. Establish
        # that real navigation's final URL before measuring hook isolation.
        deadline = time.monotonic() + 5
        while True:
            before = {
                t["id"]: t["url"]
                for t in fetch_devtools_targets(endpoint)
                if t["type"] == "page"
            }
            if url in before.values():
                break
            assert time.monotonic() < deadline, before
            time.sleep(0.01)
        started = time.monotonic()
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert time.monotonic() - started < 5
        record = parse_jsonl_output(result.stdout)
        assert result.returncode == 1, result.stdout + result.stderr
        assert record and record["status"] == "failed", result.stdout
        assert state["error"] in result.stderr
        assert not (chrome.parent / "calendar").exists()
        assert {
            t["id"]: t["url"]
            for t in fetch_devtools_targets(endpoint)
            if t["type"] == "page"
        } == before


def test_unrelated_page_preserves_target(tmp_path, ensure_chrome_test_prereqs):
    url = "https://archivebox.io/tests/"
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
        started = time.monotonic()
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert time.monotonic() - started < 5
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults", result.stdout
        assert (
            record["output_str"]
            == result.stderr.strip()
            == "No calendar feed or ICS invitation"
        )
        assert not (chrome.parent / "calendar").exists()
        assert {
            t["id"]: t["url"]
            for t in fetch_devtools_targets(endpoint)
            if t["type"] == "page"
        } == before


@pytest.mark.parametrize(
    ("links", "feed", "expected"),
    [
        ('<a href="/industrial">ICS</a>', False, "noresults"),
        ('<a href="/tags/icalendar/">iCalendar articles</a>', False, "noresults"),
        ('<a href="/topics/ical">Calendar articles</a>', False, "noresults"),
        ('<a href="/subscription/ical">Subscribe</a>', True, "succeeded"),
        ('<a href="/tags/icalendar/" type="text/calendar">Feed</a>', False, "failed"),
        (
            '<a href="/tags/icalendar/" type="text/calendar">Feed</a>'
            '<a href="/tags/icalendar/">Articles</a>',
            False,
            "failed",
        ),
        ('<a href="/subscription">ICS</a>', True, "succeeded"),
        ('<a href="/subscription">Download ICS</a>', True, "succeeded"),
        ('<a href="/subscription">Export as ICS file</a>', True, "succeeded"),
        ('<a href="/subscription">Subscribe to ICS calendar</a>', True, "succeeded"),
        (
            '<a href="/industrial">ICS</a><a href="/subscription">iCalendar</a>',
            True,
            "succeeded",
        ),
        ('<a href="/industrial" type="text/calendar">ICS</a>', False, "failed"),
        ('<a href="/industrial" download="events.ics">ICS</a>', False, "failed"),
        ('<a href="/events.ics">ICS</a>', False, "failed"),
        (
            '<a href="/industrial">ICS</a><a href="/industrial" type="text/calendar">Feed</a>',
            False,
            "failed",
        ),
    ],
)
def test_calendar_link_evidence(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    links,
    feed,
    expected,
):
    calendar = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ArchiveBox//Calendar test//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:owned-test@archivebox.io\r\nDTSTAMP:20261015T000000Z\r\n"
        b"DTSTART:20261016T120000Z\r\nDTEND:20261016T130000Z\r\n"
        b"SUMMARY:ArchiveBox calendar capture\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    httpserver.expect_request("/").respond_with_data(links, content_type="text/html")
    for endpoint in ("/industrial", "/events.ics", "/tags/icalendar/", "/topics/ical"):
        httpserver.expect_request(endpoint).respond_with_data(
            "<html><h1>Industrial Control Systems</h1></html>",
            content_type="text/html",
        )
    for endpoint in ("/subscription", "/subscription/ical"):
        httpserver.expect_request(endpoint).respond_with_data(
            calendar,
            content_type="text/calendar",
        )
    url = httpserver.url_for("/")
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
        assert result.returncode == (1 if expected == "failed" else 0), result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == expected, result.stdout + result.stderr
        output = chrome.parent / "calendar"
        if feed:
            manifest = json.loads((output / "downloads.json").read_text())
            assert len(manifest["files"]) == 1
            item = manifest["files"][0]
            assert (output / item["path"]).read_bytes() == calendar
            assert item["sha256"] == hashlib.sha256(calendar).hexdigest()
            assert item["size"] == len(calendar)
            assert manifest["event_count"] == 1
            assert manifest["events"][0]["summary"] == "ArchiveBox calendar capture"
        else:
            assert not output.exists()
        if expected == "failed":
            assert "Calendar URL did not return an ICS calendar" in result.stderr
        assert {
            t["id"]: t["url"]
            for t in fetch_devtools_targets(endpoint)
            if t["type"] == "page"
        } == before
        assert any(
            request.path
            in {
                "/industrial",
                "/events.ics",
                "/subscription",
                "/tags/icalendar/",
                "/topics/ical",
                "/subscription/ical",
            }
            for request, _ in httpserver.log
        )
