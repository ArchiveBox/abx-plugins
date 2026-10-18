"""Wget should reuse the saved browser identity without requiring live Chrome."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Response

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session


WGET_HOOK = next(Path(__file__).parent.parent.glob("on_Snapshot__*_wget.*"))


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_wget_reuses_saved_browser_identity_and_preserves_overrides(
    tmp_path: Path,
    httpserver: HTTPServer,
):
    httpserver.expect_request("/").respond_with_handler(
        lambda request: Response(
            request.headers["User-Agent"],
            content_type="text/html",
        ),
    )
    with chrome_session(tmp_path, test_url=httpserver.url_for("/")) as (
        _process,
        _pid,
        chrome_dir,
        chrome_env,
    ):
        metadata = (chrome_dir / "browser.json").read_bytes()
        snapshot_dir = chrome_dir.parent
        browser_agent = json.loads(metadata)["userAgentOverride"]["userAgent"]
        assert isinstance(browser_agent, str)
        assert "Chrome/" in browser_agent and "ArchiveBox/" not in browser_agent
        env = chrome_env.copy()

    # Chrome is closed: this is optional saved-artifact integration, not a new
    # browser dependency or a live CDP connection in the wget hook.
    cases: tuple[tuple[dict[str, str], str], ...] = (
        ({}, browser_agent),
        ({"USER_AGENT": "ExplicitGlobal/1.0"}, "ExplicitGlobal/1.0"),
        (
            {"USER_AGENT": "ExplicitGlobal/1.0", "WGET_USER_AGENT": "ExplicitWget/1.0"},
            "ExplicitWget/1.0",
        ),
    )
    for overrides, expected in cases:
        run_env: dict[str, str] = {
            **env,
            "SNAP_DIR": str(snapshot_dir),
            "WGET_WARC_ENABLED": "false",
        }
        run_env.pop("USER_AGENT", None)
        run_env.pop("WGET_USER_AGENT", None)
        run_env.update(overrides)
        result = subprocess.run(
            [str(WGET_HOOK), "--url", httpserver.url_for("/")],
            cwd=snapshot_dir,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", result.stdout
        assert (snapshot_dir / record["output_str"]).read_text() == expected
        assert (chrome_dir / "browser.json").read_bytes() == metadata


@pytest.mark.parametrize(
    "metadata",
    [None, "not JSON", "[]", '{"userAgentOverride": []}'],
)
def test_wget_without_valid_browser_metadata_stays_standalone(
    tmp_path: Path,
    httpserver: HTTPServer,
    metadata: str | None,
):
    httpserver.expect_request("/").respond_with_handler(
        lambda request: Response(
            request.headers["User-Agent"],
            content_type="text/html",
        ),
    )
    if metadata is not None:
        (tmp_path / "chrome").mkdir()
        (tmp_path / "chrome" / "browser.json").write_text(metadata)
    env: dict[str, str] = {
        **os.environ,
        "SNAP_DIR": str(tmp_path),
        "WGET_WARC_ENABLED": "false",
        "ABXPKG_LIB_DIR": str(tmp_path / "no-browser-cache"),
        "CRAWL_DIR": str(tmp_path / "no-crawl"),
    }
    env.pop("USER_AGENT", None)
    env.pop("WGET_USER_AGENT", None)
    result = subprocess.run(
        [str(WGET_HOOK), "--url", httpserver.url_for("/")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", result.stdout
    assert (tmp_path / record["output_str"]).read_text().startswith("Wget/")
