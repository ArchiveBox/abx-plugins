"""Navigation must wait out Cloudflare Turnstile interstitials before capture."""

import json
import subprocess

import pytest
from werkzeug.wrappers import Response

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

# Cloudflare's managed challenge answers the real URL with 403 + cf-mitigated
# (isolated with COOP/COEP), runs its checks in the page, then either reloads
# with a clearance cookie or submits the solution as a form POST to the URL.
CHALLENGE_PAGE = """<!doctype html><title>Just a moment...</title>
<form method="post"></form>
<script>window._cf_chl_opt = {};
setTimeout(() => { %s }, 2000);</script>"""
CLEARANCE = {
    "reload": 'document.cookie = "cf_clearance=passed; path=/"; location.reload();',
    "post": "document.forms[0].submit();",
    "unsolved": "location.reload();",
}


def challenge_site(clearance: str):
    def handler(request):
        if request.cookies.get("cf_clearance") == "passed" or request.method == "POST":
            # Protected pages also run Cloudflare's bot-detection script,
            # which defines the same global as the challenge page.
            return Response(
                "<!doctype html><title>Real article</title><p>Article body</p>"
                "<script>window._cf_chl_opt = {};</script>",
                content_type="text/html",
            )
        return Response(
            CHALLENGE_PAGE % CLEARANCE[clearance],
            status=403,
            headers={
                "cf-mitigated": "challenge",
                "Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Embedder-Policy": "require-corp",
            },
            content_type="text/html",
        )

    return handler


def navigate(url, chrome_dir, env):
    result = subprocess.run(
        [str(CHROME_NAVIGATE_HOOK), f"--url={url}", "--snapshot-id=test-snapshot"],
        cwd=str(chrome_dir),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    return result, records[-1], json.loads((chrome_dir / "navigation.json").read_text())


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
@pytest.mark.parametrize("clearance", ["reload", "post"])
def test_navigation_waits_for_cleared_challenge(tmp_path, httpserver, clearance):
    httpserver.expect_request("/article").respond_with_handler(
        challenge_site(clearance),
    )
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        result, record, navigation = navigate(url, chrome_dir, env)
        assert result.returncode == 0, result.stderr
        assert record["status"] == "succeeded"
        assert record["output_str"] == "page loaded http=200"
        assert navigation["status"] == 200
        assert navigation["challenge"] == "cleared"
        assert navigation["finalUrl"] == url
        # Navigation returns only once the site itself is loaded.
        assert 2000 <= navigation["elapsed"] < 30000, navigation


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_unsolved_challenge_is_kept_without_failing(tmp_path, httpserver):
    httpserver.expect_request("/article").respond_with_handler(
        challenge_site("unsolved"),
    )
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        # The fixed challenge window is bounded by the hook's own timeout.
        result, record, navigation = navigate(
            url,
            chrome_dir,
            {**env, "CHROME_TIMEOUT": "20"},
        )
        assert result.returncode == 0, result.stderr
        assert record["status"] == "succeeded"
        assert (
            record["output_str"]
            == "page loaded http=403 (Cloudflare challenge unsolved)"
        )
        assert navigation["status"] == 403
        assert navigation["challenge"] == "unsolved"
        assert 10000 <= navigation["elapsed"] < 20000, navigation
