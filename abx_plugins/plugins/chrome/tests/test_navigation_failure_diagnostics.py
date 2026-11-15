"""Failed browser setup retains safe navigation evidence before cleanup."""

import json

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session


def test_http_error_setup_reports_navigation_metadata(
    tmp_path,
    ensure_chrome_test_prereqs,
    chrome_test_urls,
):
    url = chrome_test_urls["not_found_url"] + "?token=private-query-value"
    with pytest.raises(RuntimeError) as failed:
        with chrome_session(tmp_path, test_url=url):
            pytest.fail("HTTP 404 navigation must fail browser setup")
    message = str(failed.value)
    assert "HTTP 404" in message
    assert "private-query-value" not in message
    metadata = json.loads(message.split("Navigation metadata: ", 1)[1])
    assert metadata["status"] == 404
    assert metadata["attempts"] == 1
    assert metadata["elapsed"] >= 0
    assert metadata["challenge_detected"] is False
    assert metadata["challenge_cleared"] is False
    assert all(isinstance(value, (int, float, bool)) for value in metadata.values())
