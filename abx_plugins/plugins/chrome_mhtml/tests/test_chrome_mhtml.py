"""Integration tests for the chrome_mhtml plugin."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import (
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
CHROME_UTILS = PLUGIN_DIR.parent / "chrome" / "chrome_utils.js"
CHROME_STARTUP_TIMEOUT_SECONDS = 45
MHTML_PARENT_TOKEN = "ABX_MHTML_PARENT_TOKEN_7391"
MHTML_OOPIF_CHILD_TOKEN = "ABX_MHTML_OOPIF_CHILD_TOKEN_7391"


@pytest.fixture
def mhtml_oopif_test_url(httpserver):
    child_url = httpserver.url_for("/child").replace(
        "localhost",
        "oopif-child.test",
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
  <iframe id="cross-site-frame"></iframe>
  <script>
    window.addEventListener("load", () => {{
      setTimeout(() => {{
        document.getElementById("cross-site-frame").src = "{child_url}";
      }}, 0);
    }});
  </script>
</body>
</html>""",
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for("/parent").replace("localhost", "127.0.0.1", 1)


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


def wait_for_oopif_child_frame(snapshot_chrome_dir: Path, env: dict[str, str]) -> None:
    script = r"""
const chromeUtils = require(process.argv[1]);
const chromeSessionDir = process.argv[2];
const childToken = process.argv[3];

(async () => {
    const puppeteer = chromeUtils.resolvePuppeteerModule();
    const connection = await chromeUtils.connectToPage({
        chromeSessionDir,
        timeoutMs: 30000,
        requireTargetId: true,
        puppeteer,
    });
    const deadline = Date.now() + 30000;
    try {
        while (Date.now() < deadline) {
            for (const frame of connection.page.frames()) {
                if (!frame.url().includes('/child')) continue;
                try {
                    const text = await frame.evaluate(() => document.body?.innerText || '');
                    if (text.includes(childToken)) {
                        process.stdout.write(JSON.stringify({url: frame.url(), text}));
                        return;
                    }
                } catch (error) {}
            }
            await new Promise(resolve => setTimeout(resolve, 250));
        }
        throw new Error(`Timed out waiting for OOPIF child frame token: ${childToken}`);
    } finally {
        connection.browser.disconnect();
    }
})().catch(error => {
    console.error(error.stack || error.message);
    process.exit(1);
});
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            script,
            str(CHROME_UTILS),
            str(snapshot_chrome_dir),
            MHTML_OOPIF_CHILD_TOKEN,
        ],
        capture_output=True,
        text=True,
        timeout=40,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert MHTML_OOPIF_CHILD_TOKEN in payload["text"]


def test_extracts_mhtml_from_cross_site_iframe(
    require_chrome_runtime,
    mhtml_oopif_test_url,
):
    """MHTML capture should include the cross-site iframe frame tree."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        test_url = mhtml_oopif_test_url
        chrome_args_extra = json.dumps(
            [
                "--site-per-process",
                "--host-resolver-rules=MAP oopif-child.test 127.0.0.1",
                "--proxy-server=direct://",
                "--proxy-bypass-list=*",
            ],
        )
        env_overrides = {
            "CHROME_ARGS_EXTRA": chrome_args_extra,
        }

        with chrome_session(
            tmpdir,
            test_url=test_url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
            env_overrides=env_overrides,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            wait_for_oopif_child_frame(snapshot_chrome_dir, env)
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
