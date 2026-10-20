"""Capture genuine public subscriptions through the real Chrome/hook lifecycle."""

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
GOVUK = "https://www.gov.uk/bank-holidays/england-and-wales.ics"
W3C = "https://www.w3.org/events/meetings/f7fc20fd-5d9e-4427-94cd-34268db72ca8/"


@pytest.mark.parametrize(
    ("url", "files"),
    [
        (GOVUK, 1),
        ("https://www.gov.uk/bank-holidays", 3),
        (W3C + "export/", 1),
        (W3C + "20260624T120000/export", 1),
        ("https://calendify.com/session/K7NgQ1qGgL8", 1),
        (
            "https://calendar.google.com/calendar/embed?src=ctk8f1jgic2ui1mkcmhfcud1mo%40group.calendar.google.com",
            1,
        ),
        ("https://archive.fosdem.org/2026/schedule/ical", 1),
    ],
)
def test_public_calendar(tmp_path, ensure_chrome_test_prereqs, url, files):
    # Chrome treats attachment responses as an aborted page navigation. Run the
    # actual navigation hook and retain its evidence; the calendar hook still
    # downloads the original through the attached browser session.
    with chrome_session(
        tmp_path,
        test_url=url,
        navigate=False,
        timeout=60,
        env_overrides={"AUTH_STORAGE_FILE": ""},
    ) as (
        _,
        _,
        chrome,
        env,
    ):
        navigation = subprocess.run(
            [str(CHROME_NAVIGATE_HOOK), f"--url={url}", "--snapshot-id=test-snapshot"],
            cwd=chrome,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if navigation.returncode:
            assert "net::ERR_ABORTED" in navigation.stderr, (
                navigation.stdout + navigation.stderr
            )
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
            timeout=90,
        )
        (tmp_path / "calendar.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", result.stdout
        assert {
            t["id"]: t["url"]
            for t in fetch_devtools_targets(endpoint)
            if t["type"] == "page"
        } == before
        output = chrome.parent / "calendar"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == files
        calendars = []
        for item in manifest["files"]:
            body = (output / item["path"]).read_bytes()
            assert body.startswith(b"BEGIN:VCALENDAR") and body.rstrip().endswith(
                b"END:VCALENDAR",
            )
            assert len(body) == item["size"] > 0
            assert hashlib.sha256(body).hexdigest() == item["sha256"]
            assert item["format"] == "ics"
            calendars.append(body)
        assert (output / "ical.js").read_bytes() == (
            HOOK.parent / "vendor/ical.cjs"
        ).read_bytes()
        assert (output / "ICAL-LICENSE.txt").read_bytes() == (
            HOOK.parent / "vendor/LICENSE"
        ).read_bytes()
        assert (
            manifest["event_count"]
            == len(manifest["events"])
            == sum(body.count(b"BEGIN:VEVENT") for body in calendars)
        )
        if "gov.uk" in url:
            christmas = [
                e
                for e in manifest["events"]
                if e["summary"] == "Christmas Day" and e["start"] == "2026-12-25"
            ]
            assert len(christmas) == files
            assert all(
                e["end"] == "2026-12-26" and e["all_day"] is True for e in christmas
            )
            assert all(b"DTSTART;VALUE=DATE:20261225" in body for body in calendars)
        elif "calendar.google.com" in url:
            assert b"X-WR-CALNAME:TTP seminar" in calendars[0]
            assert b"SUMMARY:TTP Seminar - Ivan Novikov" in calendars[0]
            assert b"DTSTART:20260422T093000Z" in calendars[0]
        elif "calendify.com" in url:
            assert len(manifest["events"]) == 1
            assert (
                manifest["events"][0]["summary"]
                == "Intro to the Decentralized Internet & Privacy devroom"
            )
        elif "fosdem.org" in url:
            assert (
                b"SUMMARY:Introduction to Local First & Welcome to our devroom"
                in calendars[0]
            )
            assert b"DTSTART:20260131" in calendars[0]
            assert manifest["event_count"] > 500
        else:
            assert b"TZID:America/New_York" in calendars[0]
            assert b"SUMMARY:CSS weekly meeting" in calendars[0]
            assert b"LOCATION:Zoom" in calendars[0]
            if url.endswith("/export/"):
                assert (
                    b"RRULE:FREQ=WEEKLY;UNTIL=20290101T170000Z;INTERVAL=1;BYDAY=WE"
                    in calendars[0]
                )
                assert (
                    b"RECURRENCE-ID;TZID=America/New_York:20241225T120000"
                    in calendars[0]
                )
                assert any(
                    e["status"] == "CANCELLED" and e["start"] == "2024-12-25T12:00:00"
                    for e in manifest["events"]
                )
            else:
                assert len(manifest["events"]) == 1
                event = manifest["events"][0]
                assert event["start"] == "2026-06-24T12:00:00"
                assert event["end"] == "2026-06-24T13:00:00"
                assert event["all_day"] is False


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
    for endpoint in ("/industrial", "/events.ics"):
        httpserver.expect_request(endpoint).respond_with_data(
            "<html><h1>Industrial Control Systems</h1></html>",
            content_type="text/html",
        )
    httpserver.expect_request("/subscription").respond_with_data(
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
            request.path in {"/industrial", "/events.ics", "/subscription"}
            for request, _ in httpserver.log
        )
