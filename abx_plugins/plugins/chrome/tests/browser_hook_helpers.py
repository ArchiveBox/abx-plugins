"""Real snapshot-hook checks shared by the browser driver plugins."""

import json
import os
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_records, run_hook
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGINS = Path(__file__).parents[2]


def assert_unconfigured_hook(hook, tmp_path):
    assert hook.is_file(), hook
    prefix = hook.parent.name.upper()
    env = {
        **os.environ,
        "SNAP_DIR": str(tmp_path),
        f"{prefix}_SCRIPT": "",
        f"{prefix}_PROMPT": "",
    }
    # Exercise the early exit with no browser session or installed driver libraries.
    command = [
        sys.executable if hook.suffix == ".py" else "node",
        str(hook),
        "--url=https://example.com",
    ]
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 10, result.stderr
    records = parse_jsonl_records(result.stdout)
    assert len(records) == 1 and records[0]["status"] == "skipped", records
    assert not list(tmp_path.iterdir())


def assert_script_capture(
    hook,
    script,
    tmp_path,
    httpserver,
    extra_env=None,
    *,
    expected_status="succeeded",
    expected_title="Prepared",
    saved_file=False,
):
    assert hook.is_file(), hook
    httpserver.expect_request("/").respond_with_data(
        '<title>Unprepared</title><script>window.driverToken="page-owned";</script>'
        "<button id=\"prepare\" onclick=\"document.title='Prepared';this.textContent='Complete'\">Prepare page</button>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    with chrome_session(tmp_path, test_url=httpserver.url_for("/")) as (
        _,
        _,
        chrome_dir,
        env,
    ):
        snapshot = chrome_dir.parent
        target = (chrome_dir / "target_id.txt").read_text()
        prefix = hook.parent.name.upper()
        env = {
            **env,
            "SNAP_DIR": str(snapshot),
            f"{prefix}_SCRIPT": script,
            **(extra_env or {}),
        }
        # Another tab with the same URL must never be selected by URL or page order.
        driver = PLUGINS / "chrome/chrome_utils.js"
        setup = subprocess.run(
            [
                env.get("NODE_BINARY") or "node",
                "-e",
                """
const {connectToPage} = require(process.argv[1]);
(async () => {
  const c = await connectToPage({chromeSessionDir:process.argv[2], waitForNavigationComplete:true});
  try {
    const decoy = await c.browser.newPage();
    await decoy.goto(process.argv[3]);
    await decoy.evaluate(() => { window.driverToken = 'other-tab'; });
    console.log(decoy.target()._targetId);
  } finally { await c.browser.disconnect(); }
})().catch(e => { console.error(e); process.exitCode=1; });
""",
                str(driver),
                str(chrome_dir),
                httpserver.url_for("/"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert setup.returncode == 0, setup.stderr
        decoy_id = setup.stdout.strip()
        code, stdout, stderr = run_hook(
            hook,
            httpserver.url_for("/"),
            "driver-test",
            cwd=snapshot,
            env=env,
            timeout=180,
        )
        assert code == (0 if expected_status == "succeeded" else 1), stderr + stdout
        records = parse_jsonl_records(stdout)
        assert records[-1]["status"] == expected_status, records
        assert (chrome_dir / "target_id.txt").read_text() == target
        result_path = snapshot / hook.parent.name / "result.log"
        if expected_status == "succeeded":
            result = json.loads(result_path.read_text())
            assert result["target_id"] == target.strip()
            assert result["title"] == expected_title, stderr + stdout
            if saved_file:
                assert (
                    (result_path.parent / "page.html")
                    .read_text()
                    .startswith("<!DOCTYPE html>")
                )
                assert (
                    'href="page.html"'
                    in (result_path.parent / "index.html").read_text()
                )
            else:
                assert not (result_path.parent / "index.html").exists()
        else:
            assert not result_path.exists()
        verify = subprocess.run(
            [
                env.get("NODE_BINARY") or "node",
                "-e",
                """
const {connectToPage} = require(process.argv[1]);
(async () => {
  const c = await connectToPage({chromeSessionDir:process.argv[2], waitForNavigationComplete:true});
  try {
    const target = c.browser.targets().find(t => t._targetId === process.argv[3]);
    if (!target) throw new Error('Other tab was closed');
    const page = await target.page();
    if (await page.title() !== 'Unprepared') throw new Error('Other tab was modified');
    await page.close();
  } finally { await c.browser.disconnect(); }
})().catch(e => { console.error(e); process.exitCode=1; });
""",
                str(driver),
                str(chrome_dir),
                decoy_id,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert verify.returncode == 0, verify.stderr
        title_hook = next((PLUGINS / "title").glob("on_Snapshot__*.js"))
        code, stdout, stderr = run_hook(
            title_hook,
            httpserver.url_for("/"),
            "driver-test",
            cwd=snapshot,
            env=env,
        )
        assert code == 0, stderr + stdout
        assert (snapshot / "title/title.txt").read_text().strip() == expected_title


def assert_challenge_capture(hook, tmp_path):
    """Verify real challenge interactions through a later DOM capture."""
    from html.parser import HTMLParser

    recipe = json.loads((hook.parent / "screenshot.json").read_text())
    configured = recipe["config_overrides"]
    prefix = hook.parent.name.upper()
    script = configured.get(f"{prefix}_SCRIPT", "")
    if not script:
        assert os.environ.get("OPENAI_API_KEY"), (
            "OPENAI_API_KEY is required for the real challenge test"
        )
    url = recipe["url"]
    with chrome_session(
        tmp_path,
        test_url=url,
        env_overrides={"CHROME_TIMEOUT": "60"},
    ) as (_, _, chrome_dir, env):
        snapshot = chrome_dir.parent
        env.update(
            {
                "SNAP_DIR": str(snapshot),
                f"{prefix}_SCRIPT": script,
                f"{prefix}_PROMPT": "",
                **{key: str(value) for key, value in configured.items()},
            },
        )
        code, stdout, stderr = run_hook(
            hook,
            url,
            "challenge",
            cwd=snapshot,
            env=env,
            timeout=270,
        )
        assert code == 0, stderr + stdout
        assert parse_jsonl_records(stdout)[-1]["status"] == "succeeded", stdout
        result = json.loads((snapshot / hook.parent.name / "result.log").read_text())
        if not script:
            assert result["done"] is True
        dom_hook = PLUGINS / "dom/on_Snapshot__53_dom.js"
        code, stdout, stderr = run_hook(
            dom_hook,
            url,
            "challenge",
            cwd=snapshot,
            env=env,
            timeout=90,
        )
        assert code == 0, stderr + stdout
        assert parse_jsonl_records(stdout)[-1]["status"] == "succeeded", stdout

        class ScoreParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.scores = []
                self.in_score = False
                self.completed = 0

            def handle_starttag(self, tag, attrs):
                classes = (dict(attrs).get("class") or "").split()
                if "score" in classes:
                    self.in_score = True
                if "task" in classes and "completed" in classes:
                    self.completed += 1

            def handle_data(self, data):
                if self.in_score and data.strip():
                    self.scores.append(int(data.strip()))

            def handle_endtag(self, tag):
                self.in_score = False

        captured = ScoreParser()
        captured.feed((snapshot / "dom/output.html").read_text())
        assert len(captured.scores) == 1, captured.scores
        assert captured.scores[0] in ({3} if script else {10, 11}), {
            "scores": captured.scores,
            "agent": result,
        }
        assert captured.completed == captured.scores[0]
        assert not list(snapshot.rglob("*.png"))
