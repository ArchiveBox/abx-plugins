"""Integration tests for the chrome_mhtml plugin."""

import os
import subprocess
import tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pytest_httpserver import HTTPServer

from abx_plugins.plugins.base.testing import (
    get_hook_script,
    get_plugin_dir,
    parse_jsonl_output,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


PLUGIN_DIR = get_plugin_dir(__file__)
_MHTML_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_chrome_mhtml.*")
if _MHTML_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
MHTML_HOOK = _MHTML_HOOK
CHROME_STARTUP_TIMEOUT_SECONDS = 45
MHTML_PARENT_TOKEN = "ABX_MHTML_PARENT_TOKEN_7391"
MHTML_OOPIF_CHILD_TOKEN = "ABX_MHTML_OOPIF_CHILD_TOKEN_7391"
MHTML_OOPIF_CHILD_HOST = "oopif-child.test"


@pytest.fixture
def mhtml_oopif_test_url():
    httpserver = HTTPServer(threaded=True)
    httpserver.start()
    child_url = httpserver.url_for("/child").replace(
        "localhost",
        MHTML_OOPIF_CHILD_HOST,
        1,
    )
    httpserver.expect_request("/child").respond_with_data(
        f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>MHTML OOPIF Child</title></head>
<body><main><h1>{MHTML_OOPIF_CHILD_TOKEN}</h1></main></body>
</html>""",
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/parent").respond_with_data(
        f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>MHTML OOPIF Parent</title></head>
<body>
  <main><h1>{MHTML_PARENT_TOKEN}</h1></main>
  <iframe id="cross-site-frame" src="{child_url}"></iframe>
</body>
</html>""",
        content_type="text/html; charset=utf-8",
    )
    try:
        yield httpserver.url_for("/parent").replace("localhost", "127.0.0.1", 1)
    finally:
        httpserver.stop()


def test_hook_script_exists():
    assert MHTML_HOOK.exists(), f"Hook not found: {MHTML_HOOK}"


@pytest.mark.parametrize(
    "plugin_name",
    [
        "chrome_mhtml",
    ],
)
def test_mhtml_preview_templates_live_with_mhtml_plugins(plugin_name):
    plugin_dir = PLUGIN_DIR.parent / plugin_name

    card_template = plugin_dir / "templates" / "card.html"
    full_template = plugin_dir / "templates" / "full.html"

    assert card_template.exists()
    assert full_template.exists()
    assert "chrome-mhtml-thumbnail" in card_template.read_text()
    assert "?preview=1" in card_template.read_text()
    assert "full-page-iframe" in full_template.read_text()
    assert (
        "renderMhtmlToHtml" in full_template.read_text()
        or "?preview=1" in full_template.read_text()
    )


@pytest.mark.parametrize("directory", ["chrome_extension_mhtml", "chrome_mhtml"])
@pytest.mark.parametrize("blank_frame", [False, True])
def test_mhtml_preview_selects_main_document(
    httpserver,
    tmp_path,
    ensure_chrome_test_prereqs,
    directory,
    blank_frame,
):
    """Replay real Chrome captures through the card and full viewer templates."""
    child = '<iframe srcdoc=""></iframe>' if blank_frame else ""
    httpserver.expect_request("/source").respond_with_data(
        f"<!doctype html><html><body><h1>{MHTML_PARENT_TOKEN}</h1>{child}</body></html>",
        content_type="text/html",
    )
    output_path = f"/{directory}/snapshot.mhtml"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=ensure_chrome_test_prereqs,
        )
        page = browser.new_page()
        page.goto(httpserver.url_for("/source"))
        mhtml = (
            page.context.new_cdp_session(page)
            .send(
                "Page.captureSnapshot",
                {"format": "mhtml"},
            )["data"]
            .encode()
        )
        if blank_frame:
            parts = BytesParser(policy=policy.default).parsebytes(mhtml).walk()
            assert any(
                part.get_content_type() == "text/html" and not part["Content-Location"]
                for part in parts
            )
        saved = tmp_path / "snapshot.mhtml"
        saved.write_bytes(mhtml)
        httpserver.expect_request(output_path, query_string="").respond_with_data(
            saved.read_bytes(),
            content_type="multipart/related",
        )
        viewer = (PLUGIN_DIR / "templates/full.html").read_text()
        viewer = viewer.replace("{{ output_path }}", output_path).replace(
            '{{ output_path_raw|default:"snapshot.mhtml" }}',
            "snapshot.mhtml",
        )
        httpserver.expect_request(
            output_path,
            query_string="preview=1",
        ).respond_with_data(viewer, content_type="text/html")
        card = (
            (PLUGIN_DIR / "templates/card.html")
            .read_text()
            .replace(
                "{{ output_path }}",
                output_path,
            )
        )
        httpserver.expect_request("/card.html").respond_with_data(
            card,
            content_type="text/html",
        )
        page.goto(httpserver.url_for("/card.html"))
        rendered = page.locator("iframe").content_frame.locator("iframe").content_frame
        assert rendered.locator("h1").inner_text() == MHTML_PARENT_TOKEN
        page.goto(httpserver.url_for(output_path) + "?preview=1")
        assert (
            page.locator("iframe").content_frame.locator("h1").inner_text()
            == MHTML_PARENT_TOKEN
        )
        assert saved.read_bytes() == mhtml
        browser.close()


def test_extracts_mhtml_from_cross_site_iframe(
    require_chrome_runtime,
    mhtml_oopif_test_url,
):
    """MHTML capture should include the cross-site iframe frame tree."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        test_url = mhtml_oopif_test_url

        with chrome_session(
            tmpdir,
            test_url=test_url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            env_overrides={
                "CHROME_ARGS_EXTRA": f'["--site-per-process","--host-resolver-rules=MAP {MHTML_OOPIF_CHILD_HOST} 127.0.0.1"]',
            },
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            output_dir = snapshot_chrome_dir.parent / "chrome_mhtml"
            output_dir.mkdir(exist_ok=True)

            result = subprocess.run(
                [
                    str(MHTML_HOOK),
                    f"--url={test_url}",
                    "--snapshot-id=test-oopif",
                ],
                cwd=output_dir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"
        assert result_json["output_str"] == "chrome_mhtml/snapshot.mhtml"

        mhtml_file = output_dir / "snapshot.mhtml"
        assert mhtml_file.exists(), (
            f"snapshot.mhtml not created. Files: {list(output_dir.iterdir())}"
        )
        mhtml_content = mhtml_file.read_text(errors="ignore")
        mhtml_lower = mhtml_content.lower()
        assert "content-type: multipart/related" in mhtml_lower
        assert MHTML_PARENT_TOKEN in mhtml_content
        assert MHTML_OOPIF_CHILD_TOKEN in mhtml_content


def test_config_chrome_mhtml_false_skips():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["CHROME_MHTML_ENABLED"] = "False"

        result = subprocess.run(
            [str(MHTML_HOOK), "--url=https://example.com", "--snapshot-id=testskip"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0
        result_json = parse_jsonl_output(result.stdout)
        assert result_json
        assert result_json["type"] == "ArchiveResult"
        assert result_json["status"] == "skipped"
        assert result_json["output_str"] == "CHROME_MHTML_ENABLED=False"
