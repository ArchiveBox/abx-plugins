"""A completed static download must not depend on an orchestrator's log layout."""

import json
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.base.utils import has_staticfile_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session
from abx_plugins.plugins.staticfile.tests.test_staticfile import run_staticfile_capture
from tests.test_staticfile_skip_helpers import _js_has_staticfile_output

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


@pytest.mark.parametrize("html", [False, True])
def test_staticfile_completion_without_legacy_log(tmp_path, httpserver, html):
    content = "<html><body>Real HTML page</body></html>" if html else '{"saved":true}'
    httpserver.expect_request("/document").respond_with_data(
        content,
        content_type="text/html" if html else "application/json",
    )
    url = httpserver.url_for("/document")
    with chrome_session(
        tmp_path,
        crawl_id="completion-crawl",
        snapshot_id="completion-snapshot",
        test_url=url,
        navigate=False,
        timeout=45,
    ) as (_process, _pid, chrome_dir, env):
        output = chrome_dir.parent / "staticfile"
        output.mkdir()
        code, stdout, stderr, navigate, record = run_staticfile_capture(
            output,
            chrome_dir,
            env,
            url,
            "completion-snapshot",
        )[:5]
        assert code == 0, stderr
        assert navigate.returncode == 0, navigate.stderr
        assert record is not None, stdout
        assert record["status"] == ("noresults" if html else "succeeded"), stdout
        assert not (output / "stdout.log").exists()
        assert has_staticfile_output(str(output)) is not html
        assert _js_has_staticfile_output(output) is not html
        if not html:
            completion = json.loads((output / "result.json").read_text())
            assert completion["status"] == record["status"]
            assert completion["output_str"] == record["output_str"]
            saved = chrome_dir.parent / record["output_str"]
            assert saved.read_text() == content
            screenshot = (
                ROOT / "abx_plugins/plugins/screenshot/on_Snapshot__51_screenshot.js"
            )
            result = subprocess.run(
                [str(screenshot), f"--url={url}"],
                env=env,
                cwd=chrome_dir.parent,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            assert parse_jsonl_output(result.stdout) == {
                "type": "ArchiveResult",
                "status": "noresults",
                "output_str": "staticfile already handled",
            }
            assert not (chrome_dir.parent / "screenshot/screenshot.png").exists()
            assert saved.read_text() == content
            # A stale record cannot hide a lost payload or authorize an escape.
            body = saved.resolve()
            outside = tmp_path / "outside.json"
            body.rename(outside)
            body.symlink_to(outside)
            assert has_staticfile_output(str(output)) is False
            assert _js_has_staticfile_output(output) is False
            body.unlink()
            outside.rename(body)
            body.write_bytes(b"")
            assert has_staticfile_output(str(output)) is False
            assert _js_has_staticfile_output(output) is False
            body.write_text(content)
            assert has_staticfile_output(str(output)) is True

            httpserver.expect_request("/html").respond_with_data(
                "<html><body>Next capture is HTML</body></html>",
                content_type="text/html",
            )

            def before_navigation(_chrome, _env):
                assert not (output / "result.json").exists()
                assert has_staticfile_output(str(output)) is False
                assert _js_has_staticfile_output(output) is False

            again = run_staticfile_capture(
                output,
                chrome_dir,
                env,
                httpserver.url_for("/html"),
                "completion-snapshot",
                before_navigation=before_navigation,
            )
            assert again[0] == 0, again[2]
            assert again[4] is not None, again[1]
            assert again[4]["status"] == "noresults", again[1]
            assert not (output / "result.json").exists()
            assert has_staticfile_output(str(output)) is False
            assert _js_has_staticfile_output(output) is False
        else:
            assert not (output / "result.json").exists()
