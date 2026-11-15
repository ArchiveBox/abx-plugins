"""Real Chrome navigation respects temporary server refusal and its cooldown."""

import json
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from werkzeug.wrappers import Response

from abx_plugins.plugins.base.utils import is_non_html_document
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def run_navigation(url, chrome_dir, env, timeout=15):
    result = subprocess.run(
        [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
        cwd=chrome_dir,
        env={**env, "CHROME_TIMEOUT": str(timeout)},
        capture_output=True,
        text=True,
        timeout=timeout + 15,
    )
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    navigation = json.loads((chrome_dir / "navigation.json").read_text())
    return result, records[-1], navigation


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry_after", ["1", "http-date", "rfc850", "asctime"])
def test_transient_response_waits_then_retries_same_identity(
    tmp_path,
    httpserver,
    status,
    retry_after,
):
    requests = []

    def article(request):
        requests.append((time.monotonic(), dict(request.headers), request.method))
        if len(requests) == 1:
            hint = retry_after
            until = datetime.now(UTC) + timedelta(seconds=3)
            if hint == "http-date":
                hint = format_datetime(until, usegmt=True)
            elif hint == "rfc850":
                hint = until.strftime("%A, %d-%b-%y %H:%M:%S GMT")
            elif hint == "asctime":
                hint = until.strftime("%a %b %e %H:%M:%S %Y")
            return Response(
                "<!doctype html><title>Temporarily unavailable</title>",
                status=status,
                headers={"Retry-After": hint},
                content_type="text/html",
            )
        return Response(
            "<!doctype html><title>Article</title>",
            content_type="text/html",
        )

    httpserver.expect_request("/article").respond_with_handler(article)
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(url, chrome_dir, env)
        assert result.returncode == 0, result.stderr
        assert record["status"] == "succeeded"
        assert navigation["status"] == 200
        assert navigation["attempts"] == 2
        assert navigation["finalUrl"] == url
        assert len(requests) == 2
        assert requests[1][0] - requests[0][0] >= (1 if retry_after == "1" else 2)
        for header in ("User-Agent", "Accept-Language", "Cookie", "Authorization"):
            assert requests[0][1].get(header) == requests[1][1].get(header)
        assert [request[2] for request in requests] == ["GET", "GET"]


@pytest.mark.parametrize("status", [429, 503])
def test_persistent_transient_response_stops_after_one_retry(
    tmp_path,
    httpserver,
    status,
):
    httpserver.expect_request("/article").respond_with_data(
        "<!doctype html><title>Still unavailable</title>",
        status=status,
        headers={"Retry-After": "1"},
        content_type="text/html",
    )
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(url, chrome_dir, env)
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed"
        assert navigation["status"] == status
        assert navigation["attempts"] == 2
        assert f"HTTP {status}" in navigation["error"]
        assert (
            len(
                [
                    request
                    for request, _ in httpserver.log
                    if request.path == "/article"
                ],
            )
            == 2
        )


@pytest.mark.parametrize(
    "retry_after",
    [None, "nonsense", "-1", "1.5", "Jan 1", "120", "999999999999999999999"],
)
def test_cooldown_is_not_shortened_to_fit_hook_budget(
    tmp_path,
    httpserver,
    retry_after,
):
    httpserver.expect_request("/article").respond_with_data(
        "<!doctype html><title>Unavailable</title>",
        status=503,
        headers={"Retry-After": retry_after} if retry_after is not None else {},
        content_type="text/html",
    )
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(url, chrome_dir, env, timeout=5)
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed"
        assert navigation["status"] == 503
        assert navigation["attempts"] == 1
        assert "budget" in navigation["error"].lower()
        if retry_after in (None, "nonsense", "-1", "1.5", "Jan 1"):
            assert navigation["retry_after_ms"] == 60000
        elif retry_after == "120":
            assert navigation["retry_after_ms"] == 120000
        assert (
            len(
                [
                    request
                    for request, _ in httpserver.log
                    if request.path == "/article"
                ],
            )
            == 1
        )


@pytest.mark.parametrize("status", [200, 403, 404, 500])
def test_other_http_responses_are_not_retried(tmp_path, httpserver, status):
    httpserver.expect_request("/article").respond_with_data(
        "<!doctype html><title>Response</title>",
        status=status,
        headers={"Retry-After": "1"},
        content_type="text/html",
    )
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(url, chrome_dir, env)
        assert result.returncode == (0 if status == 200 else 1), result.stdout
        assert record["status"] == ("succeeded" if status == 200 else "failed")
        assert navigation["status"] == status
        assert navigation["attempts"] == 1
        assert (
            len(
                [
                    request
                    for request, _ in httpserver.log
                    if request.path == "/article"
                ],
            )
            == 1
        )


def test_retry_after_zero_still_has_a_bounded_cooldown(tmp_path, httpserver):
    requests = []

    def article(_request):
        requests.append(time.monotonic())
        return Response(
            "<!doctype html><title>Busy</title>",
            status=429,
            headers={"Retry-After": "0"},
        )

    httpserver.expect_request("/article").respond_with_handler(article)
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(url, chrome_dir, env)
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed"
        assert navigation["attempts"] == len(requests) == 2
        assert requests[1] - requests[0] >= 1


def test_interrupted_cooldown_does_not_retry(tmp_path, httpserver):
    cooling = threading.Event()

    def article(_request):
        return Response(
            "<!doctype html><title>Busy</title>",
            status=503,
            headers={"Retry-After": "10"},
        )

    httpserver.expect_request("/article").respond_with_handler(article)
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        process = subprocess.Popen(
            [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
            cwd=chrome_dir,
            env={**env, "CHROME_TIMEOUT": "30"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None

        def read_stdout():
            assert process.stdout is not None
            for line in process.stdout:
                if "waiting 10000ms before one retry" in line:
                    cooling.set()

        reader = threading.Thread(target=read_stdout)
        reader.start()
        try:
            assert cooling.wait(timeout=10), "Hook never entered the cooldown"
            process.terminate()
            process.wait(timeout=5)
            reader.join(timeout=5)
            assert process.returncode != 0
            assert (
                len(
                    [
                        request
                        for request, _ in httpserver.log
                        if request.path == "/article"
                    ],
                )
                == 1
            )
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            reader.join(timeout=5)
            process.communicate(timeout=5)


def test_transient_post_response_is_not_resubmitted(tmp_path, httpserver):
    httpserver.expect_request("/form").respond_with_data(
        '<!doctype html><form method="post" action="/submit"></form>'
        "<script>document.forms[0].submit()</script>",
        content_type="text/html",
    )
    httpserver.expect_request("/submit", method="POST").respond_with_data(
        "<!doctype html><title>Busy</title>",
        status=503,
        headers={"Retry-After": "1"},
        content_type="text/html",
    )
    url = httpserver.url_for("/form")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(
            url,
            chrome_dir,
            {**env, "CHROME_WAIT_FOR": "networkidle0"},
        )
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed"
        assert navigation["status"] == 503
        assert navigation["attempts"] == 1
        assert (
            len([request for request, _ in httpserver.log if request.path == "/submit"])
            == 1
        )


def test_missing_retry_after_uses_conservative_default(tmp_path, httpserver):
    requests = []

    def article(_request):
        requests.append(time.monotonic())
        return Response(
            "<!doctype html><title>Response</title>",
            status=503 if len(requests) == 1 else 200,
            content_type="text/html",
        )

    httpserver.expect_request("/article").respond_with_handler(article)
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(url, chrome_dir, env, timeout=75)
        assert result.returncode == 0, result.stderr
        assert record["status"] == "succeeded"
        assert navigation["status"] == 200
        assert navigation["retry_after_ms"] == 60000
        assert navigation["attempts"] == len(requests) == 2
        assert requests[1] - requests[0] >= 60


def test_document_change_during_cooldown_is_not_requested_again(tmp_path, httpserver):
    httpserver.expect_request("/article").respond_with_data(
        '<!doctype html><form method="post" action="/submit"></form>'
        "<script>setTimeout(() => document.forms[0].submit(), 250)</script>",
        status=503,
        headers={"Retry-After": "1"},
        content_type="text/html",
    )
    httpserver.expect_request("/submit", method="POST").respond_with_data(
        "<!doctype html><title>Busy</title>",
        status=503,
        headers={"Retry-After": "1"},
        content_type="text/html",
    )
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(url, chrome_dir, env)
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed"
        assert navigation["status"] == 503
        assert navigation["attempts"] == 1
        assert "changed during server cooldown" in navigation["error"]
        assert [
            (request.method, request.path)
            for request, _ in httpserver.log
            if request.path != "/favicon.ico"
        ] == [("GET", "/article"), ("POST", "/submit")]


@pytest.mark.parametrize("wait_until", ["domcontentloaded", "load"])
def test_fragment_url_retry_reloads_the_document(tmp_path, httpserver, wait_until):
    requests = []

    def article(_request):
        requests.append(time.monotonic())
        return Response(
            "<!doctype html><title>Article</title><h1 id=story>Story</h1>",
            status=503 if len(requests) == 1 else 200,
            headers={"Retry-After": "1"},
            content_type="text/html",
        )

    httpserver.expect_request("/article").respond_with_handler(article)
    url = httpserver.url_for("/article") + "#story"
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(
            url,
            chrome_dir,
            {**env, "CHROME_WAIT_FOR": wait_until},
        )
        assert result.returncode == 0, result.stderr
        assert record["status"] == "succeeded"
        assert navigation["status"] == 200
        assert navigation["finalUrl"] == url
        assert navigation["attempts"] == len(requests) == 2
        assert requests[1] - requests[0] >= 1


@pytest.mark.parametrize(
    "initial_status,final_status,delay",
    [(503, 200, "0"), (200, 404, "1")],
)
def test_status_follows_the_settled_document(
    tmp_path,
    httpserver,
    initial_status,
    final_status,
    delay,
):
    httpserver.expect_request("/article").respond_with_data(
        "<!doctype html><title>Initial page</title>"
        '<script>setTimeout(() => location.replace("/final"), 250)</script>',
        status=initial_status,
        headers={"Retry-After": "1"},
        content_type="text/html",
    )
    httpserver.expect_request("/final").respond_with_data(
        "<!doctype html><title>Final page</title>",
        status=final_status,
        content_type="text/html",
    )
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(
            url,
            chrome_dir,
            {**env, "CHROME_DELAY_AFTER_LOAD": delay},
        )
        assert result.returncode == (0 if final_status == 200 else 1), result.stdout
        assert record["status"] == ("succeeded" if final_status == 200 else "failed")
        assert navigation["status"] == final_status
        assert navigation["finalUrl"] == httpserver.url_for("/final")
        assert navigation["attempts"] == 1
        assert [
            (request.method, request.path)
            for request, _ in httpserver.log
            if request.path != "/favicon.ico"
        ] == [("GET", "/article"), ("GET", "/final")]


@pytest.mark.parametrize("wait_until", ["domcontentloaded", "load"])
def test_retry_to_attachment_preserves_download_metadata(
    tmp_path,
    httpserver,
    wait_until,
):
    requests = []

    def download(_request):
        requests.append(time.monotonic())
        if len(requests) == 1:
            return Response(
                "<!doctype html><title>Busy</title>",
                status=503,
                headers={"Retry-After": "1"},
                content_type="text/html",
            )
        return Response(
            b"downloaded content",
            headers={"Content-Disposition": 'attachment; filename="download.bin"'},
            content_type="application/octet-stream",
        )

    httpserver.expect_request("/download").respond_with_handler(download)
    url = httpserver.url_for("/download")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(
            url,
            chrome_dir,
            {**env, "CHROME_WAIT_FOR": wait_until},
        )
        assert result.returncode == 1, result.stdout
        assert record["status"] == "failed"
        assert navigation["status"] == 200
        assert navigation["content_type"] == "application/octet-stream"
        assert "ERR_ABORTED" in navigation["error"]
        assert navigation["attempts"] == len(requests) == 2
        assert is_non_html_document(str(chrome_dir / "navigation.json"))


@pytest.mark.parametrize("wait_until", ["domcontentloaded", "load"])
def test_incidental_download_does_not_abort_recovered_html(
    tmp_path,
    httpserver,
    wait_until,
):
    requests = []

    def article(_request):
        requests.append(time.monotonic())
        if len(requests) == 1:
            return Response(
                "<!doctype html><title>Busy</title>",
                status=503,
                headers={"Retry-After": "1"},
                content_type="text/html",
            )
        return Response(
            "<!doctype html><title>Article</title><script>"
            'const link = document.createElement("a");'
            'link.href = URL.createObjectURL(new Blob(["incidental download"]));'
            'link.download = "incidental.txt"; link.click();'
            '</script><script src="/blocking.js"></script><h1>Article</h1>',
            content_type="text/html",
        )

    def blocking_script(_request):
        time.sleep(0.5)
        return Response(
            "document.body.dataset.ready = 'yes'",
            content_type="text/javascript",
        )

    httpserver.expect_request("/article").respond_with_handler(article)
    httpserver.expect_request("/blocking.js").respond_with_handler(blocking_script)
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = run_navigation(
            url,
            chrome_dir,
            {**env, "CHROME_WAIT_FOR": wait_until},
        )
        assert result.returncode == 0, result.stderr
        assert record["status"] == "succeeded"
        assert navigation["status"] == 200
        assert navigation["content_type"] == "text/html"
        assert navigation["attempts"] == len(requests) == 2
        assert not is_non_html_document(str(chrome_dir / "navigation.json"))
