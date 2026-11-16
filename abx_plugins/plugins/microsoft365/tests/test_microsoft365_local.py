"""Reject non-document Microsoft365 URLs using a real loopback Chrome session."""

import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_microsoft365.js"


@pytest.mark.parametrize(
    "url",
    [
        "https://pennstateoffice365.sharepoint.com/",
        "https://www.office.com/",
        "https://view.officeapps.live.com/op/view.aspx",
        "https://view.officeapps.live.com/op/embed.aspx?src=",
        "https://view.officeapps.live.com/op/view.aspx?src=not-a-document-url",
        "https://view.officeapps.live.com/op/view.aspx?src=ftp%3A%2F%2Fexample.com%2Ffile.docx",
    ],
)
def test_non_document_urls_with_local_page(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
    url,
):
    httpserver.expect_request("/").respond_with_data(
        "<title>Local non-document</title>",
        content_type="text/html",
    )
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/"),
        timeout=60,
        env_overrides={"AUTH_STORAGE_FILE": "", "CHROME_HEADLESS": "true"},
    ) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults", result.stdout
        assert not (chrome.parent / "microsoft365").exists()
